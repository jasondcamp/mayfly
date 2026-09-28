"""AZURE_ENV injection + CLI-level wiring (render, SDK guard)."""

import yaml
from typer.testing import CliRunner

from mayfly.manifests import AZURE_ENV, app_manifests, init_app_manifest
from mayfly.spec import AppSpec, InitAppSpec

# every service class needs its cloud emulator declared
EMULATORS = {"azure": {"kind": "floci-az"}}


def _container_env(manifests_or_job):
    if isinstance(manifests_or_job, list):  # app_manifests
        deploy = next(m for m in manifests_or_job if m["kind"] == "Deployment")
    else:  # init job
        deploy = manifests_or_job
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"]}


def test_azure_env_contents():
    assert AZURE_ENV["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"
    cs = AZURE_ENV["AZURE_STORAGE_CONNECTION_STRING"]
    assert "AccountName=devstoreaccount1" in cs
    assert "BlobEndpoint=http://azure:4577/devstoreaccount1" in cs
    assert "localhost" not in cs


def test_app_env_without_azure():
    env = _container_env(app_manifests("api", AppSpec(image="a:1"), "env-x"))
    assert "AWS_ENDPOINT_URL" in env
    assert "AZURE_EMULATOR_ENDPOINT" not in env
    assert "AZURE_STORAGE_CONNECTION_STRING" not in env


def test_app_env_with_azure():
    env = _container_env(
        app_manifests("api", AppSpec(image="a:1"), "env-x", azure_env=True)
    )
    assert env["AWS_ENDPOINT_URL"] == "http://aws:4566"  # AWS env still present
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"
    assert "AZURE_STORAGE_CONNECTION_STRING" in env


def test_app_env_overrides_beat_azure_env():
    app = AppSpec(image="a:1", env={"AZURE_EMULATOR_ENDPOINT": "http://other:1"})
    env = _container_env(app_manifests("api", app, "env-x", azure_env=True))
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://other:1"


def test_init_job_env_with_azure():
    job = init_app_manifest("mig", InitAppSpec(image="i:1"), azure_env=True)
    env = _container_env(job)
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"
    job = init_app_manifest("mig", InitAppSpec(image="i:1"))
    assert "AZURE_EMULATOR_ENDPOINT" not in _container_env(job)


def test_init_config_hash_unchanged_by_azure_env():
    # the run ledger hashes the spec entry, not the injected env — flipping
    # azure on must not retrigger runPolicy: on-change jobs
    from mayfly.manifests import init_app_config_hash

    init = InitAppSpec(image="i:1")
    a = init_app_manifest("mig", init, azure_env=False)
    b = init_app_manifest("mig", init, azure_env=True)
    assert (
        a["metadata"]["annotations"]["mayfly.dev/config-hash"]
        == b["metadata"]["annotations"]["mayfly.dev/config-hash"]
        == init_app_config_hash("mig", init)
    )


# ----------------------------------------------------------- azure_checks


def test_azure_checks_shapes():
    from mayfly.manifests import azure_checks
    from mayfly.spec import EnvSpec

    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "emulators": EMULATORS, "services": {
                "keyvault": [
                    {"name": "vault", "secrets": [{"name": "api-key", "value": "v"}]}
                ],
                "servicebus": [
                    {
                        "name": "main",
                        "queues": ["jobs"],
                        "topics": [{"name": "events", "subscriptions": ["worker"]}],
                    }
                ],
                "postgresflexible": [{"name": "appdb", "dbName": "orders"}],
                "azuresql": [{"name": "coredb"}],
            },
        }
    )
    checks = {c["name"]: c for c in azure_checks(spec.services)}
    assert checks["vault"]["kind"] == "keyvault"
    assert checks["vault"]["url"] == "http://azure:4577/vault-keyvault"
    assert checks["vault"]["secrets"] == ["api-key"]
    assert checks["main"]["queues"] == ["jobs"]
    assert checks["main"]["topics"] == [{"name": "events", "subscriptions": ["worker"]}]
    assert "sb://azure:5673/" in checks["main"]["connection"]
    assert checks["appdb"] == {
        "name": "appdb", "kind": "pgflex", "host": "pgflex-appdb",
        "port": 5432, "db": "orders", "user": "app",
    }
    assert checks["coredb"]["host"] == "azuresql-coredb"
    assert checks["coredb"]["port"] == 1433
    assert checks["coredb"]["user"] == "sa"


def test_azure_checks_empty_without_azure_services():
    from mayfly.manifests import azure_checks
    from mayfly.spec import EnvSpec

    assert azure_checks(EnvSpec(seed="x").services) == []


def test_azure_checks_env_injection():
    env = _container_env(
        app_manifests(
            "api", AppSpec(image="a:1"), "env-x",
            azure_env=True, azure_checks_json='[{"kind":"keyvault"}]',
        )
    )
    assert env["MAYFLY_AZURE_CHECKS"] == '[{"kind":"keyvault"}]'
    env = _container_env(app_manifests("api", AppSpec(image="a:1"), "env-x"))
    assert "MAYFLY_AZURE_CHECKS" not in env


# ------------------------------------------------------------------ render


def _render(tmp_path, spec_yaml: str) -> list[dict]:
    from mayfly.cli import app

    p = tmp_path / "env.yaml"
    p.write_text(spec_yaml)
    result = CliRunner().invoke(app, ["render", str(p)])
    assert result.exit_code == 0, result.output
    return [d for d in yaml.safe_load_all(result.output) if d]


AZ_SPEC = """
seed: az-render
emulators:
  azure: {kind: floci-az}
services:
  keyvault:
    - name: vault
      secrets: [{name: k, value: v}]
  postgresflexible:
    - name: appdb
apps:
  api: {image: a:1}
"""


def test_render_includes_azure_emulator(tmp_path):
    docs = _render(tmp_path, AZ_SPEC)
    header = docs[0]["mayfly"]
    assert header["emulators"]["azure"].startswith("floci/floci-az:")
    assert "aws" not in header["emulators"]
    deploy_names = [
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    ]
    # explicit emulators: Azure-only spec deploys no AWS emulator
    assert "azure" in deploy_names and "aws" not in deploy_names
    api = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "api"
    )
    env = {
        e["name"]: e["value"]
        for e in api["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"


def test_render_no_azure_without_azure_services(tmp_path):
    docs = _render(
        tmp_path,
        "seed: aws-only\nemulators:\n  aws: {kind: ministack}\napps:\n  api: {image: a:1}\n",
    )
    assert set(docs[0]["mayfly"]["emulators"]) == {"aws"}
    deploy_names = [
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    ]
    assert "azure" not in deploy_names


def test_render_native_only_azure_still_deploys_declared_emulator(tmp_path):
    # strict per cloud: postgresflexible runs natively, but its cloud's
    # emulator must be declared, and a declared emulator always deploys
    docs = _render(
        tmp_path,
        "seed: pg-only\nemulators:\n  azure: {kind: floci-az}\n"
        "services:\n  postgresflexible: [{name: appdb}]\n",
    )
    assert set(docs[0]["mayfly"]["emulators"]) == {"azure"}
    deploy_names = [
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    ]
    assert "azure" in deploy_names and "aws" not in deploy_names


def _app_env(docs, name="api"):
    app = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == name
    )
    return {e["name"]: e["value"] for e in app["spec"]["template"]["spec"]["containers"][0]["env"]}


MIXED_SPEC = """
seed: mixed
emulators:
  aws: {kind: ministack}
  azure: {kind: floci-az}
services:
  rds: [{name: appdb}]
  keyvault: [{name: vault}]
apps:
  api: {image: a:1}
"""


def test_render_mixed_clouds_deploys_both(tmp_path):
    docs = _render(tmp_path, MIXED_SPEC)
    assert set(docs[0]["mayfly"]["emulators"]) == {"aws", "azure"}
    deploy_names = {
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    }
    assert {"aws", "azure"} <= deploy_names
    env = _app_env(docs)
    assert env["AWS_ENDPOINT_URL"] == "http://aws:4566"
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"


def test_render_azure_only_injects_no_aws_env(tmp_path):
    env = _app_env(_render(tmp_path, AZ_SPEC))
    assert not [k for k in env if k.startswith("AWS_")]
    assert env["AZURE_EMULATOR_ENDPOINT"] == "http://azure:4577"


def test_render_no_emulators(tmp_path):
    docs = _render(tmp_path, "seed: bare\napps:\n  api: {image: a:1}\n")
    assert docs[0]["mayfly"]["emulators"] == {}
    deploy_names = {
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    }
    assert deploy_names == {"api"}
    env = _app_env(docs)
    assert not [k for k in env if k.startswith(("AWS_", "AZURE_"))]


def test_render_undeclared_emulator_fails(tmp_path):
    from mayfly.cli import app

    p = tmp_path / "env.yaml"
    p.write_text("seed: x\nservices:\n  rds: [{name: appdb}]\n")
    result = CliRunner().invoke(app, ["render", str(p)])
    assert result.exit_code != 0
    assert "declare emulators.aws" in result.output
