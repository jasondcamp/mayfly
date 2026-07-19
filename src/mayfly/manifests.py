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
    container: dict = {
        "name": name,
        "image": app.image,
        "ports": [{"containerPort": app.port}],
        "env": _env_list(env),
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "256Mi"},
        },
    }
    if app.secrets:
        container["envFrom"] = [{"secretRef": {"name": s}} for s in app.secrets]
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"enableServiceLinks": False, "containers": [container]},
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
