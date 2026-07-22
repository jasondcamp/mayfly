"""mayfly CLI: up / down / status / list / render / reap."""

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from urllib3.exceptions import MaxRetryError
from pathlib import Path

import boto3
import typer
import yaml

from . import (
    CREATED_AT_ANNOTATION,
    EXPIRES_AT_ANNOTATION,
    MANAGED_LABEL,
    SEED_LABEL,
    SERVICE_LABEL,
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
from .k8s import ClusterUnreachable, K8s, summarize_pods
from .manifests import (
    app_checks,
    app_ingress_host,
    app_manifests,
    init_app_config_hash,
    init_app_manifest,
)
from .naming import env_name, namespace_for
from .provisioners import ProvisionContext, provision_all, resolve_backend
from .spec import EnvSpec, load_spec, parse_ttl

class _Mayfly(typer.Typer):
    """Typer app that turns connectivity failures into one-line errors."""

    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except (ClusterUnreachable, MaxRetryError) as e:
            msg = str(e) if isinstance(e, ClusterUnreachable) else f"cluster unreachable: {e}"
            typer.echo(f"error: {msg}", err=True)
            raise SystemExit(1) from None


app = _Mayfly(
    help="Short lived ephemeral environments on Kubernetes.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def _global_options():
    # Library log noise (kubeconfig exec plugins, urllib3 retry warnings)
    # otherwise drowns the actual error; mayfly speaks via stdout only.
    logging.getLogger().setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    logging.getLogger("kubernetes").setLevel(logging.CRITICAL)

CTX_OPT = typer.Option(None, "--context", help="kubeconfig context (default: current)")
KCFG_OPT = typer.Option(None, "--kubeconfig", help="kubeconfig file (default: standard lookup)")
SEED_OPT = typer.Option(
    None, "--seed", help="override the spec's seed (selects/creates a different environment)"
)
SET_OPT = typer.Option(
    [],
    "--set",
    help="override a spec field before validation, path.to.field=value "
    "(repeatable; e.g. --set apps.backend.image=ghcr.io/acme/backend:pr-42)",
)


def _say(msg: str) -> None:
    typer.echo(typer.style("==> ", fg="green", bold=True) + msg)


def _detail(msg: str) -> None:
    typer.secho(f"    {msg}", dim=True)


def _ok(label: str, started: Optional[float] = None) -> None:
    """A completed step, website-terminal style: green check, dim timing."""
    elapsed = "" if started is None else f"{time.monotonic() - started:.1f}s"
    typer.echo(
        "  "
        + typer.style("\u2713", fg="green", bold=True)
        + f" {label:<50}"
        + typer.style(f"{elapsed:>7}", dim=True)
    )


def _rule() -> None:
    typer.secho("  " + "\u254c" * 59, dim=True)


def _banner() -> None:
    typer.secho(f"mayfly {__version__} \u00b7 ephemeral by design", dim=True)


def _kv(label: str, value: str) -> None:
    typer.echo("  " + typer.style(f"{label:<10}", dim=True) + value)


def _load(
    spec_file: Path,
    seed: Optional[str] = None,
    overrides: Optional[list[str]] = None,
) -> EnvSpec:
    try:
        spec = load_spec(spec_file, overrides)
    except Exception as e:
        typer.echo(f"spec error: {e}", err=True)
        raise typer.Exit(1)
    if seed:
        spec = spec.model_copy(update={"seed": seed})
    return spec


def _seed_label(seed: str) -> str:
    return seed.replace(" ", "_")[:63]


def _guard_seed_collision(k8s: K8s, ns: str, seed: str) -> None:
    """Without a hash suffix, two seeds can map to the same words; refuse to
    adopt a namespace that records a different seed."""
    obj = k8s.get_namespace(ns)
    if obj is None:
        return
    recorded = (obj.metadata.labels or {}).get(SEED_LABEL)
    if recorded and recorded != _seed_label(seed):
        typer.echo(
            f"error: namespace {ns} already belongs to seed {recorded!r} "
            f"(yours: {seed!r}) — name collision; pick a different seed",
            err=True,
        )
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
    seed: Optional[str] = SEED_OPT,
    overrides: list[str] = SET_OPT,
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
    pull_secret_namespace: str = typer.Option(
        "default",
        "--pull-secret-namespace",
        help="namespace to copy imagePullSecret secrets from",
    ),
):
    """Create or update the environment described by the spec."""
    spec = _load(spec_file, seed, overrides)
    needs_patched = []
    if spec.services.alb:
        needs_patched.append("services.alb (ALB HTTP data plane)")
    if any(
        c.engine == "valkey" and resolve_backend(c.backend, "elasticache", spec) == "emulator"
        for c in spec.services.elasticache
    ):
        needs_patched.append("elasticache engine: valkey")
    if needs_patched and spec.emulator.image is None:
        typer.echo(
            "error: the stock ministack image does not support: "
            + "; ".join(needs_patched)
            + "\nSelect mayfly's patched image in the spec:\n"
            '  emulator: {image: ghcr.io/jasondcamp/mayfly-ministack, version: "0.1.3"}',
            err=True,
        )
        raise typer.Exit(1)
    name = env_name(spec.seed)
    ns = namespace_for(spec.seed, spec.namespace_prefix)
    _banner()
    _say(f"environment {name} (namespace {ns}, seed {spec.seed!r}, ttl {spec.ttl})")
    t_up = time.monotonic()

    k8s = K8s(context, kubeconfig)
    _guard_seed_collision(k8s, ns, spec.seed)
    now = datetime.now(timezone.utc)
    expires = now + spec.ttl_delta
    t0 = time.monotonic()
    k8s.ensure_namespace(
        ns,
        labels={
            MANAGED_LABEL: "true",
            SEED_LABEL: _seed_label(spec.seed),
            SPEC_HASH_LABEL: spec.spec_hash(),
        },
        annotations={
            CREATED_AT_ANNOTATION: now.isoformat(),
            EXPIRES_AT_ANNOTATION: expires.isoformat(),
        },
    )

    _ok(f"namespace {ns}", t0)

    t0 = time.monotonic()
    _say(f"deploying emulator {spec.emulator.kind} ({resolve_image(spec.emulator)})")
    k8s.apply_all(emulator_manifests(spec.emulator, ns, msk_bootstrap(spec)), ns)
    k8s.wait_deployment(ns, AWS_SERVICE)
    _ok("emulator ready", t0)

    t0 = time.monotonic()
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
        k8s.write_secret(ns, secret_name, data, labels={SERVICE_LABEL: "true"})
        _detail(f"secret {secret_name} written")
    _ok(f"{len(secrets)} service secret(s) written", t0)

    enabled_inits = {n: a for n, a in spec.init_apps.items() if a.enabled}
    enabled_apps = {n: a for n, a in spec.apps.items() if a.enabled}
    if enabled_inits or enabled_apps:
        pull_secrets = {
            a.image_pull_secret
            for a in [*enabled_inits.values(), *enabled_apps.values()]
            if a.image_pull_secret
        }
        for ps in sorted(pull_secrets):
            k8s.copy_secret(ps, pull_secret_namespace, ns)
            _detail(f"pull secret {ps} copied from {pull_secret_namespace}")

    for init_name, init_spec in enabled_inits.items():
        job_name = f"init-{init_name}"
        if init_spec.run_policy != "always":
            prior = k8s.get_job(ns, job_name)
            prior_ok = prior is not None and (prior.status.succeeded or 0) >= 1
            if prior_ok and init_spec.run_policy == "once":
                _detail(f"init {init_name}: skipped (once; already ran)")
                continue
            if prior_ok and init_spec.run_policy == "on-change":
                prior_hash = (prior.metadata.annotations or {}).get("mayfly.dev/config-hash")
                if prior_hash == init_app_config_hash(init_name, init_spec):
                    _detail(f"init {init_name}: skipped (on-change; config unchanged)")
                    continue
        _say(f"init: {init_name}")
        t0 = time.monotonic()
        k8s.delete_job(ns, job_name)
        k8s.apply(init_app_manifest(init_name, init_spec), namespace=ns)
        try:
            k8s.wait_job(ns, job_name, init_spec.timeout_seconds)
            _ok(f"init {init_name} completed", t0)
        except RuntimeError as e:
            typer.echo(f"error: {e}", err=True)
            logs = k8s.job_logs(ns, job_name)
            if logs:
                typer.echo("--- init job logs ---", err=True)
                typer.echo(logs, err=True)
            raise typer.Exit(1)

    if enabled_apps:
        _say(f"deploying apps: {', '.join(enabled_apps)}")
        checks_json = json.dumps(app_checks(spec.apps))
        for app_name, app_spec in enabled_apps.items():
            k8s.apply_all(app_manifests(app_name, app_spec, ns, checks_json), ns)
        for app_name in enabled_apps:
            t0 = time.monotonic()
            k8s.wait_deployment(ns, app_name)
            _ok(f"{app_name} ready", t0)

    _rule()
    typer.secho(
        f"Environment ready in {time.monotonic() - t_up:.1f}s"
        f" \u00b7 ttl {spec.ttl}"
        f" \u00b7 expires {expires.isoformat(timespec='minutes')}",
        fg="green",
        bold=True,
    )
    typer.echo()
    _kv("Namespace", ns)
    for app_name, app_spec in enabled_apps.items():
        if app_spec.ingress:
            host = app_ingress_host(app_name, app_spec, ns)
            _kv(app_name.capitalize(), f"http://{host}/  (cluster ingress, port 80)")
    for alb in spec.services.alb:
        _kv(
            "ALB " + alb.name,
            f"http://{alb.name}.{ns}.localtest.me/  (load-balanced -> {alb.target_app})",
        )
    _kv("Secrets", f"kubectl -n {ns} get secrets")
    if spec.emulator.expose:
        _kv("AWS API", f"http://aws.{ns}.localtest.me  (AWS_ENDPOINT_URL; creds test/test)")
    else:
        _kv("AWS API", f"kubectl -n {ns} port-forward svc/{AWS_SERVICE} 4566:4566")
    _kv("Teardown", f"mayfly down {spec_file}")


@app.command()
def down(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    seed: Optional[str] = SEED_OPT,
    name: Optional[str] = typer.Option(None, "--name", help="environment name instead of spec"),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Destroy the environment (deletes its namespace)."""
    spec = _load(spec_file, seed)
    ns = (
        namespace_for(spec.seed, spec.namespace_prefix)
        if not name
        else (f"{spec.namespace_prefix}-{name}" if spec.namespace_prefix else name)
    )
    k8s = K8s(context, kubeconfig)
    _managed_namespace(k8s, ns)
    _say(f"deleting {ns}")
    t0 = time.monotonic()
    k8s.delete_namespace(ns)
    _ok(f"namespace {ns} deleted", t0)
    typer.secho("destroyed \u00b7 gone by sunset", fg="green", bold=True)


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
        if n.metadata.deletion_timestamp:
            expires_in = "TERMINATING"
        elif expires:
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
    seed: Optional[str] = SEED_OPT,
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Show pods and provisioned endpoints for the environment."""
    spec = _load(spec_file, seed)
    ns = namespace_for(spec.seed, spec.namespace_prefix)
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
    seed: Optional[str] = SEED_OPT,
    overrides: list[str] = SET_OPT,
):
    """Print the resolved plan (name, namespace, manifests) without touching the cluster."""
    spec = _load(spec_file, seed, overrides)
    ns = namespace_for(spec.seed, spec.namespace_prefix)
    docs = [
        {"mayfly": {"name": env_name(spec.seed), "namespace": ns, "ttl": spec.ttl,
                    "emulator": resolve_image(spec.emulator), "specHash": spec.spec_hash()}},
        *emulator_manifests(spec.emulator, ns, msk_bootstrap(spec)),
    ]
    for init_name, init_spec in spec.init_apps.items():
        if init_spec.enabled:
            docs.append(init_app_manifest(init_name, init_spec))
    checks_json = json.dumps(app_checks(spec.apps))
    for app_name, app_spec in spec.apps.items():
        if app_spec.enabled:
            docs.extend(app_manifests(app_name, app_spec, ns, checks_json))
    typer.echo(yaml.safe_dump_all(docs, sort_keys=False))


@app.command()
def restart(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    seed: Optional[str] = SEED_OPT,
    apps_filter: list[str] = typer.Option(
        [], "--app", help="restart only these apps (repeatable); default: all apps"
    ),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Rolling-restart the environment's apps (e.g. after pushing new images
    under the same tag). Services and the emulator are left untouched."""
    spec = _load(spec_file, seed)
    ns = namespace_for(spec.seed, spec.namespace_prefix)
    k8s = K8s(context, kubeconfig)
    _managed_namespace(k8s, ns)

    targets = {n: a for n, a in spec.apps.items() if a.enabled}
    if apps_filter:
        unknown = set(apps_filter) - set(targets)
        if unknown:
            typer.echo(f"error: unknown app(s): {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(1)
        targets = {n: a for n, a in targets.items() if n in apps_filter}

    _say(f"restarting: {', '.join(targets)}")
    for name in targets:
        k8s.restart_deployment(ns, name)
    for name in targets:
        t0 = time.monotonic()
        k8s.wait_deployment(ns, name, timeout=300)
        _ok(f"{name} rolled", t0)
    typer.secho(f"{len(targets)} app(s) restarted", fg="green", bold=True)


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
        if n.metadata.deletion_timestamp:
            continue  # already terminating
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
    if dry_run:
        _say(f"{reaped} environment(s) eligible")
    else:
        _say(
            f"{reaped} environment(s) reaped"
            + (" (namespaces terminate in the background; "
               "they show TERMINATING in `mayfly list` until gone)" if reaped else "")
        )


@app.command()
def extend(
    spec_file: Path = typer.Argument(Path("env.yaml"), exists=True, dir_okay=False),
    seed: Optional[str] = SEED_OPT,
    ttl: str = typer.Option(..., "--ttl", help="new TTL from now, e.g. 4h"),
    context: Optional[str] = CTX_OPT,
    kubeconfig: Optional[str] = KCFG_OPT,
):
    """Push the environment's expiry out to now + TTL."""
    delta = parse_ttl(ttl)
    spec = _load(spec_file, seed)
    ns = namespace_for(spec.seed, spec.namespace_prefix)
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
