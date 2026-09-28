"""Unit tests for the pure parts of helm-chart apps: placeholder
interpolation, rendered-doc invariant enforcement, wait/check derivation.
The `helm template` subprocess itself is covered by e2e only."""

import pytest

from mayfly.helm import (
    HELM_APP_LABEL,
    HelmError,
    helm_checks,
    helm_deployments,
    prepare_rendered_docs,
    resolve_placeholders,
)
from mayfly.spec import HelmAppSpec

SECRETS = {
    "rds-appdb": {"DATABASE_URL": "postgresql://app:pw@appdb:5432/app"},
    "sm-app-api-key": {"SECRET_VALUE": "s3kr1t", "SECRET_NAME": "app/api-key"},
}


def test_resolve_placeholders_nested():
    values = {
        "db": {"url": "${secret:rds-appdb:DATABASE_URL}"},
        "env": [{"name": "API_KEY", "value": "key=${secret:sm-app-api-key:SECRET_VALUE}!"}],
        "replicas": 2,
        "debug": False,
        "nothing": None,
    }
    out = resolve_placeholders(values, SECRETS)
    assert out["db"]["url"] == "postgresql://app:pw@appdb:5432/app"
    assert out["env"][0]["value"] == "key=s3kr1t!"
    assert out["replicas"] == 2 and out["debug"] is False and out["nothing"] is None
    # input untouched
    assert values["db"]["url"] == "${secret:rds-appdb:DATABASE_URL}"


def test_resolve_placeholders_multiple_in_one_string():
    out = resolve_placeholders(
        {"combo": "${secret:sm-app-api-key:SECRET_NAME}=${secret:sm-app-api-key:SECRET_VALUE}"},
        SECRETS,
    )
    assert out["combo"] == "app/api-key=s3kr1t"


def test_resolve_placeholders_unknown_secret():
    with pytest.raises(HelmError, match="unknown secret 'nope'"):
        resolve_placeholders({"x": "${secret:nope:KEY}"}, SECRETS)


def test_resolve_placeholders_unknown_key():
    with pytest.raises(HelmError, match="unknown key 'NOPE'"):
        resolve_placeholders({"x": "${secret:rds-appdb:NOPE}"}, SECRETS)


def test_resolve_placeholders_empty():
    assert resolve_placeholders({}, {}) == {}


def _rendered_docs():
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "podinfo", "labels": {"app.kubernetes.io/name": "podinfo"}},
            "spec": {"template": {"spec": {"containers": [{"name": "podinfo"}]}}},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "podinfo", "namespace": "chart-default"},
            "spec": {"ports": [{"port": 9898}]},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "sweeper"},
            "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": []}}}}},
        },
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "one-off"}, "spec": {}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": "wide"}},
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "rogue"}},
    ]


def test_prepare_rendered_docs():
    kept, warnings = prepare_rendered_docs("podinfo", _rendered_docs(), "mf-env")
    kinds = [d["kind"] for d in kept]
    assert kinds == ["Deployment", "Service", "CronJob", "Pod"]
    assert len(warnings) == 2
    assert any("ClusterRole/wide" in w for w in warnings)
    assert any("Namespace/rogue" in w for w in warnings)
    for doc in kept:
        assert doc["metadata"]["namespace"] == "mf-env"  # foreign ns overridden too
        assert doc["metadata"]["labels"][HELM_APP_LABEL] == "podinfo"
    deployment = kept[0]
    assert deployment["metadata"]["labels"]["app.kubernetes.io/name"] == "podinfo"  # preserved
    assert deployment["spec"]["template"]["spec"]["enableServiceLinks"] is False
    cronjob = kept[2]
    assert (
        cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["enableServiceLinks"] is False
    )
    pod = kept[3]
    assert pod["spec"]["enableServiceLinks"] is False


def test_helm_deployments():
    kept, _ = prepare_rendered_docs("podinfo", _rendered_docs(), "mf-env")
    assert helm_deployments(kept) == ["podinfo"]


def test_helm_checks():
    apps = {
        "podinfo": HelmAppSpec.model_validate(
            {
                "chart": "podinfo",
                "repo": "https://stefanprodan.github.io/podinfo",
                "version": "6.9.1",
                "check": {"kind": "http", "target": "http://podinfo:9898/healthz"},
            }
        ),
        "nocheck": HelmAppSpec.model_validate(
            {"chart": "c", "repo": "https://r.example.com", "version": "1.0.0"}
        ),
        "disabled": HelmAppSpec.model_validate(
            {
                "chart": "c",
                "repo": "https://r.example.com",
                "version": "1.0.0",
                "enabled": False,
                "check": {"kind": "tcp", "target": "c:80"},
            }
        ),
    }
    assert helm_checks(apps) == [
        {"name": "podinfo", "kind": "http", "target": "http://podinfo:9898/healthz"}
    ]
