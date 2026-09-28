"""Helm-chart apps: client-side render (``helm template``), placeholder
interpolation from provisioned secrets, invariant enforcement.

mayfly applies the rendered docs itself via server-side apply — no helm
release exists, so the namespace TTL remains the only lifecycle boundary.
"""

import copy
import re
import shutil
import subprocess
import tempfile

import yaml

from .spec import HelmAppSpec

HELM_APP_LABEL = "mayfly.dev/helm-app"

_PLACEHOLDER_RE = re.compile(r"\$\{secret:([^:{}]+):([^:{}]+)\}")

# kinds whose pod template gets enableServiceLinks: False — the ``aws``
# Service otherwise injects AWS_PORT=tcp://... style env vars into pods
# (same rationale as manifests.py; a standing invariant).
_POD_TEMPLATE_KINDS = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}
)

# cluster-scoped kinds are skipped: they would outlive the namespace TTL and
# collide across environments. Unknown (CRD) kinds are assumed namespaced;
# a truly cluster-scoped one fails server-side at apply time.
_CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Namespace",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "PersistentVolume",
        "StorageClass",
        "IngressClass",
        "PriorityClass",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "APIService",
        "RuntimeClass",
    }
)


class HelmError(RuntimeError):
    """User-facing helm failure (missing binary, template error, bad placeholder)."""


def require_helm() -> None:
    if shutil.which("helm") is None:
        raise HelmError(
            "helm not found on PATH — the helmApps section requires the helm "
            "CLI (https://helm.sh/docs/intro/install/)"
        )


def resolve_placeholders(values: dict, secrets: dict[str, dict[str, str]]) -> dict:
    """Replace ``${secret:<name>:<KEY>}`` in every string of the values tree
    with provisioned secret data. Unknown secret or key is a hard error —
    fail closed before anything renders."""

    def _sub(match: re.Match) -> str:
        name, key = match.group(1), match.group(2)
        if name not in secrets:
            raise HelmError(
                f"values reference unknown secret {name!r} "
                f"(available: {', '.join(sorted(secrets)) or 'none'})"
            )
        if key not in secrets[name]:
            raise HelmError(
                f"values reference unknown key {key!r} in secret {name!r} "
                f"(available: {', '.join(sorted(secrets[name]))})"
            )
        return secrets[name][key]

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return _PLACEHOLDER_RE.sub(_sub, node)
        return node

    return _walk(copy.deepcopy(values))


def render_chart(name: str, helm: HelmAppSpec, namespace: str, values: dict) -> list[dict]:
    """``helm template`` the pinned chart with the given values; return the
    parsed manifest docs."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(values, f)
        f.flush()
        cmd = [
            "helm",
            "template",
            name,
            helm.chart,
            "--repo",
            helm.repo,
            "--version",
            helm.version,
            "--namespace",
            namespace,
            "--values",
            f.name,
            # hook manifests (tests, pre/post-install jobs) need helm's
            # lifecycle to mean anything; rendered flat they'd apply as
            # plain resources — and test pods carry randomized name
            # suffixes that would accumulate on every re-up.
            "--no-hooks",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise HelmError(
                "helm not found on PATH — the helmApps section requires the "
                "helm CLI (https://helm.sh/docs/intro/install/)"
            ) from None
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise HelmError(f"helm template failed for {name!r}: {detail}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc]


def _pod_specs(doc: dict) -> list[dict]:
    """Pod spec dicts inside a rendered object (created on demand)."""
    kind = doc.get("kind")
    if kind in _POD_TEMPLATE_KINDS:
        return [doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})]
    if kind == "CronJob":
        return [
            doc.setdefault("spec", {})
            .setdefault("jobTemplate", {})
            .setdefault("spec", {})
            .setdefault("template", {})
            .setdefault("spec", {})
        ]
    if kind == "Pod":
        return [doc.setdefault("spec", {})]
    return []


def prepare_rendered_docs(
    name: str, docs: list[dict], namespace: str
) -> tuple[list[dict], list[str]]:
    """Filter and stamp rendered docs for apply. Returns (docs, warnings).

    Cluster-scoped kinds are dropped (with a warning); every kept doc is
    forced into the environment namespace, labeled for attribution, and has
    mayfly's pod invariants re-asserted."""
    kept: list[dict] = []
    warnings: list[str] = []
    for doc in docs:
        kind = doc.get("kind", "<unknown>")
        obj_name = doc.get("metadata", {}).get("name", "<unnamed>")
        if kind in _CLUSTER_SCOPED_KINDS:
            warnings.append(f"skipping cluster-scoped {kind}/{obj_name} from helm app {name!r}")
            continue
        meta = doc.setdefault("metadata", {})
        meta["namespace"] = namespace
        meta.setdefault("labels", {})[HELM_APP_LABEL] = name
        for pod_spec in _pod_specs(doc):
            pod_spec["enableServiceLinks"] = False
        kept.append(doc)
    return kept, warnings


def helm_deployments(docs: list[dict]) -> list[str]:
    """Names of rendered Deployments — the readiness wait set (v1 waits
    Deployments only)."""
    return [
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Deployment" and d.get("metadata", {}).get("name")
    ]


def helm_checks(helm_apps: dict[str, HelmAppSpec]) -> list[dict]:
    """Dragonfly check entries for helm apps — same shape as
    manifests.app_checks(), consumed unchanged by dragonfly's check_app."""
    return [
        {"name": name, "kind": h.check.kind, "target": h.check.target}
        for name, h in helm_apps.items()
        if h.enabled and h.check
    ]
