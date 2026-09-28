"""App Kubernetes manifests, built as plain dicts.

enableServiceLinks is disabled everywhere: the ``aws`` Service otherwise
injects AWS_PORT=tcp://... style env vars into pods, and Quarkus-based
emulators (floci) fatally misparse the analogous *_PORT variable as an
integer config property.
"""

from .emulators import AWS_ENDPOINT, AZURE_ENDPOINT
from .provisioners.azure import (
    keyvault_url,
    servicebus_connection_string,
    storage_connection_string,
)
from .provisioners.native import DB_USER, MSSQL_PASSWORD, MSSQL_USER
from .spec import AppSpec, InitAppSpec, ServicesSpec

AWS_ENV = {
    "AWS_ENDPOINT_URL": AWS_ENDPOINT,
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_DEFAULT_REGION": "us-east-1",
}

# each bundle is injected only when the spec declares that cloud's emulator
AZURE_ENV = {
    "AZURE_EMULATOR_ENDPOINT": AZURE_ENDPOINT,
    "AZURE_STORAGE_CONNECTION_STRING": storage_connection_string(),
}


def _base_env(azure_env: bool, aws_env: bool) -> dict[str, str]:
    return {**(AWS_ENV if aws_env else {}), **(AZURE_ENV if azure_env else {})}


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


def _env_from(refs) -> list[dict]:
    out = []
    for ref in refs:
        if isinstance(ref, str):
            out.append({"secretRef": {"name": ref}})
        else:
            entry: dict = {"secretRef": {"name": ref.name}}
            if ref.prefix:
                entry["prefix"] = ref.prefix
            out.append(entry)
    return out


def init_app_config_hash(name: str, init: InitAppSpec) -> str:
    import hashlib
    import json as _json

    canonical = _json.dumps(
        {"name": name, **init.model_dump(by_alias=True)}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def init_app_manifest(
    name: str, init: InitAppSpec, azure_env: bool = False, aws_env: bool = True
) -> dict:
    """A one-shot Job for an initApps entry. The config-hash annotation is
    the run ledger: the completed Job records what configuration last
    succeeded, which runPolicy once/on-change consult before rerunning."""
    limits = {"memory": init.resources.memory_limit}
    if init.resources.cpu_limit:
        limits["cpu"] = init.resources.cpu_limit
    container: dict = {
        "name": name,
        "image": init.image,
        "env": _env_list({**_base_env(azure_env, aws_env), **init.env}),
        "resources": {
            "requests": {"cpu": init.resources.cpu, "memory": init.resources.memory},
            "limits": limits,
        },
    }
    if init.command:
        container["command"] = init.command
    if init.args:
        container["args"] = init.args
    if init.secrets:
        container["envFrom"] = _env_from(init.secrets)
    pod_spec: dict = {
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "containers": [container],
    }
    if init.image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": init.image_pull_secret}]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"init-{name}",
            "labels": {"mayfly.dev/init-app": name},
            "annotations": {"mayfly.dev/config-hash": init_app_config_hash(name, init)},
        },
        "spec": {
            "backoffLimit": 1,
            "activeDeadlineSeconds": init.timeout_seconds,
            "template": {
                "metadata": {"labels": {"mayfly.dev/init-app": name}},
                "spec": pod_spec,
            },
        },
    }
    if init.patch:
        job = merge_patch(job, init.patch)
        job["metadata"]["name"] = f"init-{name}"
        job["spec"]["template"]["spec"]["enableServiceLinks"] = False
    return job


def app_ingress_host(
    name: str, app: AppSpec, namespace: str, domain: str = "localtest.me"
) -> str:
    return app.ingress.host or f"{name}.{namespace}.{domain}"


def app_checks(apps: dict) -> list[dict]:
    """Health checks derived from each enabled app's readiness spec, reachable
    through its Service — consumed by observer apps (dragonfly) via the
    MAYFLY_APP_CHECKS env var."""
    checks = []
    for name, app in apps.items():
        if not app.enabled or not app.readiness:
            continue
        if app.readiness.tcp:
            checks.append({"name": name, "kind": "tcp",
                           "target": f"{name}:{app.service_port}"})
        else:
            checks.append({"name": name, "kind": "http",
                           "target": f"http://{name}:{app.service_port}{app.readiness.path}"})
    return checks


def azure_checks(services: ServicesSpec) -> list[dict]:
    """Azure-service checks derived from the spec — consumed by observer
    apps (dragonfly) via MAYFLY_AZURE_CHECKS. Spec-driven rather than
    control-plane-discovered: floci-az's ARM listing support is unverified,
    and the native database classes are invisible to any control plane."""
    return [
        {
            "name": vault.name,
            "kind": "keyvault",
            "url": keyvault_url(vault.name),
            "secrets": [s.name for s in vault.secrets],
        }
        for vault in services.keyvault
    ] + [
        {
            "name": sb.name,
            "kind": "servicebus",
            "connection": servicebus_connection_string(sb.name),
            "queues": sb.queues,
            "topics": [
                {"name": t.name, "subscriptions": t.subscriptions} for t in sb.topics
            ],
        }
        for sb in services.servicebus
    ] + [
        {
            "name": db.name,
            "kind": "pgflex",
            "host": f"pgflex-{db.name}",
            "port": 5432,
            "db": db.db_name,
            "user": DB_USER,
        }
        for db in services.postgresflexible
    ] + [
        {
            "name": db.name,
            "kind": "azuresql",
            "host": f"azuresql-{db.name}",
            "port": 1433,
            "db": db.db_name,
            "user": MSSQL_USER,
            # fixture constant, not a real credential (parity with the
            # DB_PASSWORD convention dragonfly already assumes)
            "password": MSSQL_PASSWORD,
        }
        for db in services.azuresql
    ]


def app_manifests(
    name: str,
    app: AppSpec,
    namespace: str,
    checks_json: str = "",
    ingress_domain: str = "localtest.me",
    azure_env: bool = False,
    azure_checks_json: str = "",
    aws_env: bool = True,
) -> list[dict]:
    env = {
        **_base_env(azure_env, aws_env),
        "MAYFLY_APP_NAME": name,
        **({"MAYFLY_APP_CHECKS": checks_json} if checks_json else {}),
        **({"MAYFLY_AZURE_CHECKS": azure_checks_json} if azure_checks_json else {}),
        **app.env,
    }
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
        container["envFrom"] = _env_from(app.secrets)
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
        host = app_ingress_host(name, app, namespace, ingress_domain)
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
