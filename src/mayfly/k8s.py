"""Kubernetes helpers: server-side apply, namespaces, secrets, port-forward."""

import contextlib
import json
import re
import subprocess
import time
from collections.abc import Iterator
from typing import Optional

from kubernetes import client, config, dynamic
from kubernetes.client import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

try:  # urllib3 >= 2
    from urllib3.exceptions import NameResolutionError
except ImportError:  # urllib3 1.x: DNS failures surface as NewConnectionError
    class NameResolutionError(Exception):
        pass

from . import FIELD_MANAGER


class ClusterUnreachable(RuntimeError):
    """The Kubernetes API server can't be reached; message is user-facing."""


def _unreachable(e: MaxRetryError, context: Optional[str]) -> ClusterUnreachable:
    host = f"{e.pool.host}:{e.pool.port}"
    dns_failure = isinstance(e.reason, NameResolutionError) or (
        "resolve" in str(e.reason).lower() or "name or service not known" in str(e.reason).lower()
    )
    if dns_failure:
        what = f"cannot resolve cluster host {e.pool.host!r}"
    elif isinstance(e.reason, ConnectTimeoutError):
        what = f"connection to {host} timed out"
    else:
        what = f"cannot connect to {host}"
    ctx = f"context {context!r}" if context else "the current kubeconfig context"
    return ClusterUnreachable(
        f"{what} ({ctx}). Is the cluster up? Use --context/--kubeconfig to target another."
    )


class K8s:
    def __init__(self, context: Optional[str] = None, kubeconfig: Optional[str] = None):
        try:
            config.load_kube_config(config_file=kubeconfig, context=context)
        except ConfigException as e:
            raise ClusterUnreachable(str(e)) from e
        self.context = context
        self.kubeconfig = kubeconfig
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.batch = client.BatchV1Api()
        try:
            # first API contact (/version + discovery) happens here
            self.dyn = dynamic.DynamicClient(client.ApiClient())
        except MaxRetryError as e:
            raise _unreachable(e, context) from e

    # ------------------------------------------------------------ apply
    def apply(self, manifest: dict, namespace: Optional[str] = None) -> None:
        """Server-side apply a single manifest dict."""
        resource = self.dyn.resources.get(
            api_version=manifest["apiVersion"], kind=manifest["kind"]
        )
        kwargs = dict(
            body=manifest,
            name=manifest["metadata"]["name"],
            content_type="application/apply-patch+yaml",
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )
        if resource.namespaced:
            kwargs["namespace"] = namespace
        resource.patch(**kwargs)

    def apply_all(self, manifests: list[dict], namespace: str) -> None:
        for m in manifests:
            self.apply(m, namespace=namespace)

    # -------------------------------------------------------- namespace
    def ensure_namespace(
        self, name: str, labels: dict[str, str], annotations: dict[str, str]
    ) -> None:
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": name, "labels": labels, "annotations": annotations},
            }
        )

    def get_namespace(self, name: str):
        try:
            return self.core.read_namespace(name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_namespaces(self, label_selector: str):
        return self.core.list_namespace(label_selector=label_selector).items

    def delete_namespace(self, name: str, wait: bool = True, timeout: int = 300) -> None:
        try:
            self.core.delete_namespace(name)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        if wait:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.get_namespace(name) is None:
                    return
                time.sleep(2)
            raise TimeoutError(f"namespace {name} still terminating after {timeout}s")

    # ---------------------------------------------------------- secrets
    def write_secret(
        self,
        namespace: str,
        name: str,
        data: dict[str, str],
        labels: Optional[dict] = None,
    ) -> None:
        metadata: dict = {"name": name}
        if labels:
            metadata["labels"] = labels
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": metadata,
                "stringData": data,
            },
            namespace=namespace,
        )

    def copy_secret(self, name: str, source_ns: str, target_ns: str) -> None:
        """Copy a secret (e.g. registry credentials) into an env namespace."""
        try:
            s = self.core.read_namespaced_secret(name, source_ns)
        except ApiException as e:
            if e.status == 404:
                raise RuntimeError(
                    f"pull secret {name!r} not found in namespace {source_ns!r}; "
                    f"create it there (kubectl create secret docker-registry ...) "
                    f"or pass --pull-secret-namespace"
                ) from e
            raise
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name},
                "type": s.type,
                "data": s.data or {},
            },
            namespace=target_ns,
        )

    def read_secret(self, namespace: str, name: str) -> Optional[dict]:
        import base64

        try:
            s = self.core.read_namespaced_secret(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return {k: base64.b64decode(v).decode() for k, v in (s.data or {}).items()}

    # ------------------------------------------------------ deployments
    def wait_deployment(self, namespace: str, name: str, timeout: int = 180) -> None:
        """Wait for rollout completion (kubectl rollout status semantics).

        Checking available_replicas alone is not enough: during a rolling
        update the OLD pod still counts as available, and provisioning
        against it is work an in-memory emulator forgets seconds later.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                d = self.apps.read_namespaced_deployment(name, namespace)
                want = d.spec.replicas or 1
                s = d.status
                if (
                    (s.observed_generation or 0) >= d.metadata.generation
                    and (s.updated_replicas or 0) >= want
                    and (s.available_replicas or 0) >= want
                    and not s.unavailable_replicas
                ):
                    return
            except ApiException as e:
                if e.status != 404:
                    raise
            time.sleep(2)
        raise TimeoutError(f"deployment {namespace}/{name} rollout not complete after {timeout}s")

    # ------------------------------------------------------------- jobs
    def get_job(self, namespace: str, name: str):
        try:
            return self.batch.read_namespaced_job(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def delete_job(self, namespace: str, name: str, timeout: int = 120) -> None:
        """Delete a Job and its pods, waiting until gone (Jobs are immutable,
        so re-running an init app means recreate)."""
        try:
            self.batch.delete_namespaced_job(
                name, namespace, propagation_policy="Foreground"
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.batch.read_namespaced_job(name, namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(1)
        raise TimeoutError(f"job {namespace}/{name} still deleting after {timeout}s")

    def wait_job(self, namespace: str, name: str, timeout: int) -> None:
        """Wait for Job completion; raise RuntimeError with reason on failure."""
        deadline = time.monotonic() + timeout + 30  # deadline enforcement margin
        while time.monotonic() < deadline:
            j = self.batch.read_namespaced_job(name, namespace)
            s = j.status
            if (s.succeeded or 0) >= 1:
                return
            for cond in s.conditions or []:
                if cond.type == "Failed" and cond.status == "True":
                    raise RuntimeError(
                        f"init job {name} failed: {cond.reason or ''} {cond.message or ''}".strip()
                    )
            time.sleep(2)
        raise RuntimeError(f"init job {name} did not complete within {timeout}s")

    def job_logs(self, namespace: str, name: str, tail: int = 40) -> str:
        pods = self.core.list_namespaced_pod(
            namespace, label_selector=f"job-name={name}"
        ).items
        chunks = []
        for p in pods[-2:]:
            try:
                resp = self.core.read_namespaced_pod_log(
                    p.metadata.name, namespace, tail_lines=tail,
                    _preload_content=False,
                )
                chunks.append(resp.data.decode(errors="replace"))
            except ApiException:
                pass
        return "\n".join(chunks).strip()

    def restart_deployment(self, namespace: str, name: str) -> None:
        """Trigger a rolling restart (same mechanism as kubectl rollout restart)."""
        from datetime import datetime, timezone

        self.apps.patch_namespaced_deployment(
            name,
            namespace,
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "mayfly.dev/restarted-at": datetime.now(
                                    timezone.utc
                                ).isoformat(timespec="seconds")
                            }
                        }
                    }
                }
            },
        )

    def exec_in_deployment(
        self, namespace: str, deployment: str, command: list[str], retries: int = 5
    ) -> str:
        """Run a command in the first ready pod of a deployment."""
        from kubernetes.stream import stream

        pods = self.core.list_namespaced_pod(
            namespace, label_selector=f"app={deployment}"
        ).items
        ready = [
            p for p in pods
            if any(c.ready for c in (p.status.container_statuses or []))
        ]
        if not ready:
            raise RuntimeError(f"no ready pod for {namespace}/{deployment}")
        last_err: Optional[Exception] = None
        for _ in range(retries):
            try:
                return stream(
                    self.core.connect_get_namespaced_pod_exec,
                    ready[0].metadata.name,
                    namespace,
                    command=command,
                    stderr=True, stdin=False, stdout=True, tty=False,
                )
            except Exception as e:  # exec can flake while the app finishes booting
                last_err = e
                time.sleep(3)
        raise RuntimeError(f"exec failed in {namespace}/{deployment}: {last_err}")

    # ----------------------------------------------------- port-forward
    @contextlib.contextmanager
    def port_forward(
        self, namespace: str, service: str, remote_port: int
    ) -> Iterator[int]:
        """Port-forward a Service to an ephemeral local port via kubectl.

        Yields the local port. kubectl picks the port (``:remote``) so
        parallel invocations never collide.
        """
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", namespace, "port-forward", f"svc/{service}", f":{remote_port}"]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            local_port = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        err = proc.stderr.read()
                        raise RuntimeError(f"port-forward failed: {err.strip()}")
                    continue
                m = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
                if m:
                    local_port = int(m.group(1))
                    break
            if local_port is None:
                raise TimeoutError("port-forward did not report a local port in 30s")
            yield local_port
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def summarize_pods(k8s: K8s, namespace: str) -> list[dict]:
    pods = k8s.core.list_namespaced_pod(namespace).items
    out = []
    for p in pods:
        ready = sum(1 for c in (p.status.container_statuses or []) if c.ready)
        total = len(p.spec.containers)
        out.append(
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "ready": f"{ready}/{total}",
            }
        )
    return out


def to_json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
