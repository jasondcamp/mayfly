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


def app_manifests(name: str, app: AppSpec) -> list[dict]:
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
        container["readinessProbe"] = {
            "httpGet": {
                "path": app.readiness.path,
                "port": app.readiness.port or app.port,
            },
            "initialDelaySeconds": app.readiness.initial_delay_seconds,
            "periodSeconds": app.readiness.period_seconds,
        }
    if app.secrets:
        container["envFrom"] = [{"secretRef": {"name": s}} for s in app.secrets]
    pod_spec: dict = {"enableServiceLinks": False, "containers": [container]}
    if app.image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": app.image_pull_secret}]
    return [
        {
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
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": 8080, "targetPort": app.port}],
            },
        },
    ]
