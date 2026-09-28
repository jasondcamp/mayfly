"""The emulator conformance specs (tests/emulators/) stay valid and resolve
to the backends each conformance run assumes — so spec-model changes that
would break the emulators workflow fail here first, without a cluster."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mayfly.provisioners import resolve_backend
from mayfly.spec import load_spec

SPEC_DIR = Path(__file__).parent / "emulators"
KINDS = ("ministack", "floci", "floci-az")


def _spec(kind):
    return load_spec(SPEC_DIR / f"env-{kind}.yaml")


@pytest.mark.parametrize("kind", KINDS)
def test_spec_validates(kind):
    spec = _spec(kind)
    assert spec.seed == f"emu-{kind}"


def test_ministack_spec_is_emulator_backed():
    spec = _spec("ministack")
    for svc in ("s3", "rds", "elasticache", "msk", "dynamodb", "alb", "secretsmanager"):
        assert resolve_backend("auto", svc, spec) == "emulator", svc
    assert spec.emulators.azure is None


def test_floci_spec_splits_backends():
    spec = _spec("floci")
    for svc in ("s3", "dynamodb", "secretsmanager"):
        assert resolve_backend("auto", svc, spec) == "emulator", svc
    for svc in ("rds", "elasticache", "msk"):
        assert resolve_backend("auto", svc, spec) == "native", svc
    assert not spec.services.alb  # emulator-only class; no floci data plane


def test_floci_az_spec_backends():
    spec = _spec("floci-az")
    assert spec.emulators.azure is not None
    assert spec.emulators.aws is None  # Azure-only: no AWS emulator deploys
    assert resolve_backend("auto", "keyvault", spec) == "emulator"
    assert resolve_backend("auto", "servicebus", spec) == "emulator"
    assert resolve_backend("auto", "postgresflexible", spec) == "native"
    assert resolve_backend("auto", "azuresql", spec) == "native"


@pytest.mark.parametrize(
    ("kind", "expected_deployments"),
    [
        ("ministack", {"aws", "dragonfly", "hello"}),
        ("floci", {"aws", "dragonfly"}),
        ("floci-az", {"azure", "dragonfly"}),
    ],
)
def test_specs_render_expected_deployments(kind, expected_deployments):
    from mayfly.cli import app

    result = CliRunner().invoke(app, ["render", str(SPEC_DIR / f"env-{kind}.yaml")])
    assert result.exit_code == 0, result.output
    docs = [d for d in yaml.safe_load_all(result.output) if d]
    deployments = {
        d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"
    }
    assert deployments == expected_deployments


def test_runner_covers_every_spec():
    # scripts/emulator-test.sh derives paths from the kind name: every spec
    # needs its smoke script and vice versa
    specs = {p.stem.removeprefix("env-") for p in SPEC_DIR.glob("env-*.yaml")}
    smokes = {p.stem.removeprefix("smoke-") for p in SPEC_DIR.glob("smoke-*.sh")}
    assert specs == smokes == set(KINDS)


def test_workflow_matrix_matches_kinds():
    workflow = yaml.safe_load(
        (Path(__file__).parent.parent / ".github" / "workflows" / "emulators.yml").read_text()
    )
    options = workflow[True]["workflow_dispatch"]["inputs"]["emulator"]["options"]
    assert options == ["all", *KINDS]
