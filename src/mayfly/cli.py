"""mayfly CLI: up / down / status / list / render / reap."""

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

import boto3
import typer
import yaml

from . import (
    CREATED_AT_ANNOTATION,
    EXPIRES_AT_ANNOTATION,
    MANAGED_LABEL,
    SEED_LABEL,
    SPEC_HASH_LABEL,
    __version__,
)
from .emulators import (
    AWS_PORT,
    AWS_SERVICE,
    emulator_manifests,
    msk_bootstrap,
    resolve_image,
)
from .k8s import K8s, summarize_pods
from .manifests import app_manifests
from .naming import env_name, namespace_for
from .provisioners import ProvisionContext, provision_all
from .spec import EnvSpec, load_spec, parse_ttl

app = typer.Typer(help="Short lived ephemeral environments on Kubernetes.", no_args_is_help=True)

CTX_OPT = typer.Option(None, "--context", help="kubeconfig context (default: current)")
KCFG_OPT = typer.Option(None, "--kubeconfig", help="kubeconfig file (default: standard lookup)")


def _say(msg: str) -> None:
    typer.echo(f"==> {msg}")


def _detail(msg: str) -> None:
    typer.echo(f"    {msg}")


def _load(spec_file: Path) -> EnvSpec:
    try:
        return load_spec(spec_file)
    except Exception as e:
        typer.echo(f"spec error: {e}", err=True)
        raise typer.Exit(1)


def _managed_namespace(k8s: K8s, ns: str):
    """Return the namespace object, or exit if missing/unmanaged."""
    obj = k8s.get_namespace(ns)
    if obj is None:
        typer.echo(f"namespace {ns} not found", err=True)
        raise typer.Exit(1)
    labels = obj.metadata.labels or {}
    if labels.get(MANAGED_LABEL) != "true":
        typer.echo(
            f"refusing: namespace {ns} is not labeled {MANAGED_LABEL}=true "
            "(not created by mayfly)",
            err=True,
        )
        raise typer.Exit(1)
    return obj


@app.command()
def version():
    """Print the mayfly version."""
    typer.echo(__version__)


@app.command()
def up(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
    pull_secret_namespace: str = typer.Option(
        "default",
        "--pull-secret-namespace",
        help="namespace to copy imagePullSecret secrets from",
    ),
):
    """Create or update the environment described by the spec."""
    spec = _load(spec_file)
    name = env_name(spec.seed)
    ns = namespace_for(spec.seed)
    _say(f"environment {name} (namespace {ns}, seed {spec.seed!r}, ttl {spec.ttl})")

    k8s = K8s(context, kubeconfig)
    now = datetime.now(timezone.utc)
    expires = now + spec.ttl_delta
    k8s.ensure_namespace(
        ns,
        labels={
            MANAGED_LABEL: "true",
            SEED_LABEL: spec.seed.replace(" ", "_")[:63],
            SPEC_HASH_LABEL: spec.spec_hash(),
        },
        annotations={
            CREATED_AT_ANNOTATION: now.isoformat(),
            EXPIRES_AT_ANNOTATION: expires.isoformat(),
        },
    )

    _say(f"deploying emulator {spec.emulator.kind} ({resolve_image(spec.emulator)})")
    k8s.apply_all(emulator_manifests(spec.emulator, ns, msk_bootstrap(spec)), ns)
    k8s.wait_deployment(ns, AWS_SERVICE)

    _say("provisioning services")
    with k8s.port_forward(ns, AWS_SERVICE, AWS_PORT) as local_port:
        ctx = ProvisionContext(
            k8s=k8s,
            namespace=ns,
            session_factory=_aws_session_factory(f"http://127.0.0.1:{local_port}"),
            progress=_detail,
        )
        secrets = provision_all(spec, ctx)

    for secret_name, data in secrets.items():
        k8s.write_secret(ns, secret_name, data)
        _detail(f"secret {secret_name} written")

    enabled_apps = {n: a for n, a in spec.apps.items() if a.enabled}
    if enabled_apps:
        pull_secrets = {a.image_pull_secret for a in enabled_apps.values() if a.image_pull_secret}
        for ps in sorted(pull_secrets):
            k8s.copy_secret(ps, pull_secret_namespace, ns)
            _detail(f"pull secret {ps} copied from {pull_secret_namespace}")
        _say(f"deploying apps: {', '.join(enabled_apps)}")
        for app_name, app_spec in enabled_apps.items():
            k8s.apply_all(app_manifests(app_name, app_spec), ns)
        for app_name in enabled_apps:
            k8s.wait_deployment(ns, app_name)

    _say(f"environment {name} is up (expires {expires.isoformat(timespec='minutes')})")
    typer.echo()
    typer.echo(f"  Namespace: {ns}")
    typer.echo(f"  Secrets:   kubectl -n {ns} get secrets")
    typer.echo(f"  AWS API:   kubectl -n {ns} port-forward svc/{AWS_SERVICE} 4566:4566")
    typer.echo(f"  Teardown:  mayfly down {spec_file}")


@app.command()
def down(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    name: Optional[str] = typer.Option(None, "--name", help="environment name instead of spec"),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Destroy the environment (deletes its namespace)."""
    ns = f"env-{name}" if name else namespace_for(_load(spec_file).seed)
    k8s = K8s(context, kubeconfig)
    _managed_namespace(k8s, ns)
    _say(f"deleting {ns}")
    k8s.delete_namespace(ns)
    _say("destroyed")


@app.command("list")
def list_envs(context: Optional[str] = CTX_OPT, kubeconfig: Optional[str] = KCFG_OPT):
    """List mayfly environments."""
    k8s = K8s(context, kubeconfig)
    namespaces = k8s.list_namespaces(f"{MANAGED_LABEL}=true")
    if not namespaces:
        typer.echo("no environments")
        return
    now = datetime.now(timezone.utc)
    rows = [("NAMESPACE", "SEED", "AGE", "EXPIRES-IN")]
    for n in namespaces:
        ann = n.metadata.annotations or {}
        labels = n.metadata.labels or {}
        age = now - n.metadata.creation_timestamp
        expires = ann.get(EXPIRES_AT_ANNOTATION)
        if expires:
            left = datetime.fromisoformat(expires) - now
            expires_in = _fmt_delta(left) if left > timedelta(0) else "EXPIRED"
        else:
            expires_in = "-"
        rows.append((n.metadata.name, labels.get(SEED_LABEL, "-"), _fmt_delta(age), expires_in))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        typer.echo("  ".join(c.ljust(w) for c, w in zip(r, widths)))


@app.command()
def status(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Show pods and provisioned endpoints for the environment."""
    spec = _load(spec_file)
    ns = namespace_for(spec.seed)
    k8s = K8s(context, kubeconfig)
    obj = _managed_namespace(k8s, ns)
    ann = obj.metadata.annotations or {}
    _say(f"{ns} (expires {ann.get(EXPIRES_AT_ANNOTATION, '?')})")
    typer.echo()
    typer.echo("PODS")
    for p in summarize_pods(k8s, ns):
        typer.echo(f"  {p['name']:<50} {p['ready']:<6} {p['phase']}")
    typer.echo()
    typer.echo("SECRETS")
    secret_names = ["s3-buckets"] if spec.services.s3.buckets else []
    secret_names += [f"rds-{d.name}" for d in spec.services.rds]
    secret_names += [f"elasticache-{c.name}" for c in spec.services.elasticache]
    secret_names += [f"msk-{m.name}" for m in spec.services.msk]
    for sn in secret_names:
        data = k8s.read_secret(ns, sn)
        state = "ok" if data else "MISSING"
        typer.echo(f"  {sn:<30} {state}")


@app.command()
def render(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
):
    """Print the resolved plan (name, namespace, manifests) without touching the cluster."""
    spec = _load(spec_file)
    ns = namespace_for(spec.seed)
    docs = [
        {"mayfly": {"name": env_name(spec.seed), "namespace": ns, "ttl": spec.ttl,
                    "emulator": resolve_image(spec.emulator), "specHash": spec.spec_hash()}},
        *emulator_manifests(spec.emulator, ns, msk_bootstrap(spec)),
    ]
    for app_name, app_spec in spec.apps.items():
        if app_spec.enabled:
            docs.extend(app_manifests(app_name, app_spec))
    typer.echo(yaml.safe_dump_all(docs, sort_keys=False))


@app.command()
def reap(
    dry_run: bool = typer.Option(False, "--dry-run", help="report only, delete nothing"),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Delete environments whose TTL has expired."""
    k8s = K8s(context, kubeconfig)
    now = datetime.now(timezone.utc)
    reaped = 0
    for n in k8s.list_namespaces(f"{MANAGED_LABEL}=true"):
        ann = n.metadata.annotations or {}
        expires = ann.get(EXPIRES_AT_ANNOTATION)
        if not expires:
            continue
        if datetime.fromisoformat(expires) <= now:
            if dry_run:
                _say(f"would reap {n.metadata.name} (expired {expires})")
            else:
                _say(f"reaping {n.metadata.name} (expired {expires})")
                k8s.delete_namespace(n.metadata.name, wait=False)
            reaped += 1
    _say(f"{reaped} environment(s) {'eligible' if dry_run else 'reaped'}")


@app.command()
def extend(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    ttl: str = typer.Option(..., "--ttl", help="new TTL from now, e.g. 4h"),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Push the environment's expiry out to now + TTL."""
    delta = parse_ttl(ttl)
    ns = namespace_for(_load(spec_file).seed)
    k8s = K8s(context, kubeconfig)
    _managed_namespace(k8s, ns)
    expires = (datetime.now(timezone.utc) + delta).isoformat()
    k8s.core.patch_namespace(ns, {"metadata": {"annotations": {EXPIRES_AT_ANNOTATION: expires}}})
    _say(f"{ns} now expires {expires}")


def _aws_session_factory(endpoint_url: str):
    """Session factory whose clients all target the forwarded floci endpoint."""

    class _Session:
        def client(self, service: str):
            return boto3.Session(
                aws_access_key_id="test",
                aws_secret_access_key="test",
                region_name="us-east-1",
            ).client(service, endpoint_url=endpoint_url)

    return _Session


def _fmt_delta(d: timedelta) -> str:
    total = int(d.total_seconds())
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h{(total % 3600) // 60}m"
    return f"{total // 86400}d{(total % 86400) // 3600}h"


if __name__ == "__main__":
    sys.exit(app())
