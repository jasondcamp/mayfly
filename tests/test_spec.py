from datetime import timedelta

import pytest

from mayfly.spec import EnvSpec, load_spec, parse_ttl

MINIMAL = {"seed": "test-1"}

FULL = {
    "apiVersion": "mayfly/v1alpha1",
    "seed": "test-full",
    "ttl": "2h",
    "services": {
        "s3": {"buckets": ["assets", "uploads"]},
        "rds": [{"name": "appdb", "engine": "postgres", "dbName": "app"}],
        "elasticache": [{"name": "cache-a"}],
        "msk": [{"name": "events", "topics": ["orders"]}],
    },
    "apps": {
        "echo": {"image": "ealen/echo-server:latest", "port": 80, "secrets": ["rds-appdb"]},
    },
}


def test_minimal_defaults():
    spec = EnvSpec.model_validate(MINIMAL)
    assert spec.ttl == "8h"
    assert spec.services.rds == []
    assert spec.apps == {}


def test_full_spec():
    spec = EnvSpec.model_validate(FULL)
    assert spec.services.rds[0].db_name == "app"
    assert spec.services.rds[0].scheme == "postgresql"
    assert spec.apps["echo"].secrets == ["rds-appdb"]
    assert spec.ttl_delta == timedelta(hours=2)


def test_unknown_keys_rejected():
    with pytest.raises(ValueError):
        EnvSpec.model_validate({**MINIMAL, "servcies": {}})


def test_bad_api_version_rejected():
    with pytest.raises(ValueError):
        EnvSpec.model_validate({**MINIMAL, "apiVersion": "mayfly/v9"})


def test_bad_engine_rejected():
    with pytest.raises(ValueError):
        EnvSpec.model_validate(
            {**MINIMAL, "services": {"rds": [{"name": "db", "engine": "oracle"}]}}
        )


def test_bad_resource_name_rejected():
    with pytest.raises(ValueError):
        EnvSpec.model_validate({**MINIMAL, "services": {"rds": [{"name": "Bad_Name"}]}})


@pytest.mark.parametrize(
    ("ttl", "delta"),
    [("30m", timedelta(minutes=30)), ("8h", timedelta(hours=8)), ("2d", timedelta(days=2))],
)
def test_ttl_parse(ttl, delta):
    assert parse_ttl(ttl) == delta


def test_ttl_invalid():
    with pytest.raises(ValueError):
        parse_ttl("soon")


def test_spec_hash_stable_and_sensitive():
    a = EnvSpec.model_validate(FULL)
    b = EnvSpec.model_validate(FULL)
    assert a.spec_hash() == b.spec_hash()
    c = EnvSpec.model_validate({**FULL, "ttl": "3h"})
    assert a.spec_hash() != c.spec_hash()


def test_load_spec(tmp_path):
    p = tmp_path / "env.yaml"
    p.write_text("seed: file-test\n")
    assert load_spec(p).seed == "file-test"


def test_namespace_prefix_validated():
    spec = EnvSpec.model_validate({"seed": "x", "namespacePrefix": "team-a"})
    assert spec.namespace_prefix == "team-a"
    with pytest.raises(ValueError):
        EnvSpec.model_validate({"seed": "x", "namespacePrefix": "Bad_Prefix"})


def test_dynamodb_spec():
    spec = EnvSpec.model_validate(
        {"seed": "x", "services": {"dynamodb": [{"name": "sessions"}, {"name": "carts", "hashKey": "cartId"}]}}
    )
    assert spec.services.dynamodb[0].hash_key == "id"
    assert spec.services.dynamodb[1].hash_key == "cartId"


def test_alb_spec_and_target_validation():
    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "services": {"alb": [{"name": "hello-alb", "targetApp": "hello"}]},
            "apps": {"hello": {"image": "h:1"}},
        }
    )
    assert spec.services.alb[0].target_app == "hello"
    with pytest.raises(ValueError, match="not in apps"):
        EnvSpec.model_validate(
            {"seed": "x", "services": {"alb": [{"name": "a", "targetApp": "ghost"}]}}
        )


def test_elasticache_engine_and_version():
    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "services": {
                "elasticache": [
                    {"name": "a"},
                    {"name": "b", "engine": "memcached", "version": "1.6"},
                    {"name": "c", "engine": "valkey"},
                ]
            },
        }
    )
    a, b, c = spec.services.elasticache
    assert (a.engine, a.resolved_version, a.port) == ("redis", "7.2", 6379)
    assert (b.engine, b.resolved_version, b.port) == ("memcached", "1.6", 11211)
    assert (c.engine, c.resolved_version, c.port) == ("valkey", "8", 6379)
    with pytest.raises(ValueError):
        EnvSpec.model_validate(
            {"seed": "x", "services": {"elasticache": [{"name": "a", "engine": "mongo"}]}}
        )


def test_secretsmanager_spec():
    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "services": {
                "secretsmanager": [
                    {"name": "app/api-key", "value": "v1"},
                    {"name": "app/signing-key", "generate": True},
                ]
            },
        }
    )
    assert spec.services.secretsmanager[0].value == "v1"
    assert spec.services.secretsmanager[1].generate is True
    for bad in (
        {"name": "app/x"},                             # neither
        {"name": "app/x", "value": "v", "generate": True},  # both
        {"name": "bad name!", "value": "v"},           # invalid chars
    ):
        with pytest.raises(ValueError):
            EnvSpec.model_validate({"seed": "x", "services": {"secretsmanager": [bad]}})


def test_sm_k8s_name_mangling():
    from mayfly.provisioners.aws import _sm_k8s_name

    assert _sm_k8s_name("app/api-key") == "sm-app-api-key"
    assert _sm_k8s_name("App_Signing.Key@2") == "sm-app-signing-key-2"


def test_init_apps_spec():
    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "initApps": {"migrate": {"image": "i:1", "command": ["rake"]}},
        }
    )
    assert spec.init_apps["migrate"].timeout_seconds == 600
    with pytest.raises(ValueError):
        EnvSpec.model_validate(
            {"seed": "x", "initApps": {"Bad_Name": {"image": "i:1"}}}
        )


# ---------------------------------------------------------------- overrides
def _raw_example():
    import yaml
    from pathlib import Path

    return yaml.safe_load(Path("examples/env.yaml").read_text())


def test_set_override_dict_path():
    from mayfly.spec import EnvSpec, apply_overrides

    raw = _raw_example()
    apply_overrides(raw, ["apps.hello.image=ghcr.io/jasondcamp/mayfly-hello:pr-9"])
    spec = EnvSpec.model_validate(raw)
    assert spec.apps["hello"].image == "ghcr.io/jasondcamp/mayfly-hello:pr-9"


def test_set_override_coerces_int_and_adds_env_key():
    from mayfly.spec import EnvSpec, apply_overrides

    raw = _raw_example()
    apply_overrides(
        raw, ["apps.hello.replicas=5", "apps.caddis-api.env.FLAG=on"]
    )
    spec = EnvSpec.model_validate(raw)
    assert spec.apps["hello"].replicas == 5
    assert spec.apps["caddis-api"].env["FLAG"] == "on"


def test_set_override_typo_in_app_name_errors():
    import pytest

    from mayfly.spec import apply_overrides

    with pytest.raises(ValueError, match="no key 'nope'"):
        apply_overrides(_raw_example(), ["apps.nope.image=x"])


def test_set_override_named_list_entry():
    from mayfly.spec import EnvSpec, apply_overrides

    raw = _raw_example()
    apply_overrides(raw, ["services.rds.appdb.dbName=other"])
    spec = EnvSpec.model_validate(raw)
    assert spec.services.rds[0].db_name == "other"


def test_set_override_list_index():
    from mayfly.spec import apply_overrides

    raw = _raw_example()
    apply_overrides(raw, ["services.s3.buckets.0=first"])
    assert raw["services"]["s3"]["buckets"][0] == "first"


def test_set_override_errors():
    import pytest

    from mayfly.spec import apply_overrides

    with pytest.raises(ValueError, match="expected path"):
        apply_overrides({}, ["no-equals-sign"])
    with pytest.raises(ValueError, match="no entry named"):
        apply_overrides(_raw_example(), ["services.rds.missing.dbName=x"])


def test_ingress_domain_default_and_custom():
    from mayfly.spec import EnvSpec

    assert EnvSpec(seed="x").ingress_domain == "localtest.me"
    spec = EnvSpec.model_validate({"seed": "x", "ingressDomain": "envs.corp.example.com"})
    assert spec.ingress_domain == "envs.corp.example.com"


def test_ingress_domain_invalid():
    import pytest

    from mayfly.spec import EnvSpec

    for bad in ("Envs.Example.Com", "envs..example.com", "-bad.example.com", ""):
        with pytest.raises(Exception):
            EnvSpec.model_validate({"seed": "x", "ingressDomain": bad})


def test_ingress_domain_threads_into_hosts():
    from mayfly.manifests import app_ingress_host
    from mayfly.spec import AppSpec

    app = AppSpec.model_validate({"image": "x:1", "port": 80, "ingress": {}})
    assert app_ingress_host("api", app, "pr-42", "envs.example.com") == "api.pr-42.envs.example.com"
    pinned = AppSpec.model_validate(
        {"image": "x:1", "port": 80, "ingress": {"host": "api.example.com"}}
    )
    assert app_ingress_host("api", pinned, "pr-42", "envs.example.com") == "api.example.com"
