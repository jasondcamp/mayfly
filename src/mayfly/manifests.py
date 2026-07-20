"""App Kubernetes manifests, built as plain dicts.

enableServiceLinks is disabled everywhere: the ``aws`` Service otherwise
injects AWS_PORT=tcp://... style env vars into pods, and Quarkus-based
emulators (floci) fatally misparse the analogous *_PORT variable as an
integer config property.
"""

from .emulators import AWS_ENDPOINT
from .spec import AppSpec

AWS_ENV = {
    "AWS_ENDPOINT_URL": AWS_ENDPOINT,
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def _env_list(env: dict[str, str]) -> list[dict]:
    return [{"name": k, "value": v} for k, v in env.items()]


def merge_patch(base, patch):
    """Deep-merge an app ``patch:`` onto a generated manifest.

    Maps merge recursively. Lists whose elements are all objects with a
    ``name`` key (containers, volumes, env, ports, ...) merge by name —
    matching entries deep-merge, new entries append. Any other list is
    replaced wholesale. Scalars replace.
    """
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = merge_patch(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(patch, list):
        named = all(isinstance(x, dict) and "name" in x for x in base + patch)
        if named:
            merged = {x["name"]: x for x in base}
            for x in patch:
                merged[x["name"]] = merge_patch(merged.get(x["name"], {}), x)
            return list(merged.values())
        return patch
    return patch


def app_ingress_host(name: str, app: AppSpec, namespace: str) -> str:
    return app.ingress.host or f"{name}.{namespace}.localtest.me"


def app_manifests(name: str, app: AppSpec, namespace: str) -> list[dict]:
    env = {**AWS_ENV, **app.env}
    limits = {"memory": app.resources.memory_limit}
    if app.resources.cpu_limit:
        limits["cpu"] = app.resources.cpu_limit
    container: dict = {
        "name": name,
        "image": app.image,
        "ports": [{"containerPort": app.port}],
        "env": _env_list(env),
        "resources": {
            "requests": {"cpu": app.resources.cpu, "memory": app.resources.memory},
            "limits": limits,
        },
    }
    if app.command:
        container["command"] = app.command
    if app.args:
        container["args"] = app.args
    if app.readiness:
        probe_port = app.readiness.port or app.port
        probe = (
            {"tcpSocket": {"port": probe_port}}
            if app.readiness.tcp
            else {"httpGet": {"path": app.readiness.path, "port": probe_port}}
        )
        container["readinessProbe"] = {
            **probe,
            "initialDelaySeconds": app.readiness.initial_delay_seconds,
            "periodSeconds": app.readiness.period_seconds,
        }
        if app.readiness.timeout_seconds:
            container["readinessProbe"]["timeoutSeconds"] = app.readiness.timeout_seconds
    if app.secrets:
        env_from = []
        for ref in app.secrets:
            if isinstance(ref, str):
                env_from.append({"secretRef": {"name": ref}})
            else:
                entry: dict = {"secretRef": {"name": ref.name}}
                if ref.prefix:
                    entry["prefix"] = ref.prefix
                env_from.append(entry)
        container["envFrom"] = env_from
    pod_spec: dict = {"enableServiceLinks": False, "containers": [container]}
    if app.image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": app.image_pull_secret}]
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "replicas": app.replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": pod_spec,
            },
        },
    }
    if app.patch:
        deployment = merge_patch(deployment, app.patch)
        # re-assert what mayfly depends on, whatever the patch said:
        # selector/app-label wire the Service and ingress; service links
        # crash Quarkus-based emulators (FLOCI_PORT parsing).
        deployment["metadata"]["name"] = name
        deployment["spec"]["selector"] = {"matchLabels": {"app": name}}
        deployment["spec"]["template"].setdefault("metadata", {}).setdefault(
            "labels", {}
        )["app"] = name
        deployment["spec"]["template"]["spec"]["enableServiceLinks"] = False
    manifests = [
        deployment,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": app.service_port, "targetPort": app.port}],
            },
        },
    ]
    if app.ingress:
        host = app_ingress_host(name, app, namespace)
        rule: dict = {
            "http": {
                "paths": [
                    {
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {"service": {"name": name, "port": {"number": 8080}}},
                    }
                ]
            }
        }
        if host != "*":  # "*" = match any host (e.g. an ALB's raw DNS name)
            rule["host"] = host
        spec: dict = {"rules": [rule]}
        if app.ingress.class_name:
            spec["ingressClassName"] = app.ingress.class_name
        metadata: dict = {"name": name}
        if app.ingress.annotations:
            metadata["annotations"] = app.ingress.annotations
        ingress = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": metadata,
            "spec": spec,
        }
        if app.ingress.patch:
            ingress = merge_patch(ingress, app.ingress.patch)
            ingress["metadata"]["name"] = name  # ownership stays stable
        manifests.append(ingress)
    return manifests
