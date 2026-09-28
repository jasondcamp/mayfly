"""Dev overrides: MAYFLY_<COMPONENT>_IMAGE env vars and scripts/overrides.sh."""

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mayfly.emulators import (
    KUBEDOCK_IMAGE,
    OVERRIDE_ENV,
    active_overrides,
    azure_emulator_manifests,
    emulator_manifests,
    override_image,
    pinned_image,
    resolve_image,
)
from mayfly.spec import AzureEmulatorSpec, AwsEmulatorSpec

SCRIPT = Path(__file__).parent.parent / "scripts" / "overrides.sh"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in OVERRIDE_ENV.values():
        monkeypatch.delenv(var, raising=False)


def _kubedock(manifests):
    pod = next(m for m in manifests if m["kind"] == "Deployment")["spec"]["template"]["spec"]
    return next(c for c in pod["containers"] if c["name"] == "kubedock")


# --------------------------------------------------------------- env vars


def test_no_overrides_by_default():
    assert active_overrides() == {}
    assert resolve_image(AwsEmulatorSpec(kind="ministack")) == pinned_image("ministack")
    assert _kubedock(emulator_manifests(AwsEmulatorSpec(kind="ministack"), "ns"))["image"] == KUBEDOCK_IMAGE


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        ("ministack", AwsEmulatorSpec(kind="ministack")),
        ("floci", AwsEmulatorSpec(kind="floci")),
        ("floci-az", AzureEmulatorSpec(kind="floci-az")),
    ],
)
def test_emulator_override_wins(monkeypatch, kind, spec):
    monkeypatch.setenv(OVERRIDE_ENV[kind], f"mayfly-override/{kind}:dev-abc123")
    assert resolve_image(spec) == f"mayfly-override/{kind}:dev-abc123"


def test_override_beats_spec_image_and_version(monkeypatch):
    monkeypatch.setenv("MAYFLY_MINISTACK_IMAGE", "mayfly-override/ministack:dev-1")
    spec = AwsEmulatorSpec(kind="ministack", image="mirror/ministack", version="9.9.9")
    assert resolve_image(spec) == "mayfly-override/ministack:dev-1"


def test_override_is_per_kind(monkeypatch):
    monkeypatch.setenv("MAYFLY_FLOCI_IMAGE", "mayfly-override/floci:dev-1")
    assert resolve_image(AwsEmulatorSpec(kind="ministack")) == pinned_image("ministack")


def test_kubedock_override_in_both_emulator_pods(monkeypatch):
    ref = "mayfly-override/kubedock:dev-abc123"
    monkeypatch.setenv("MAYFLY_KUBEDOCK_IMAGE", ref)
    for manifests in (
        emulator_manifests(AwsEmulatorSpec(kind="ministack"), "ns"),
        azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "ns"),
    ):
        kd = _kubedock(manifests)
        assert kd["image"] == ref
        # setup init containers must use the same (locally built) image —
        # kubedock's default is joyrex2001/kubedock:<own version>
        assert f"--initimage={ref}" in kd["args"]


def test_initimage_matches_pinned_kubedock_by_default():
    kd = _kubedock(emulator_manifests(AwsEmulatorSpec(kind="ministack"), "ns"))
    assert f"--initimage={KUBEDOCK_IMAGE}" in kd["args"]


@pytest.mark.parametrize(
    "bad", ["mayfly-override/kubedock", "kubedock:latest", "reg.io:5000/kubedock", "x:latest@sha256:ab"]
)
def test_override_needs_explicit_non_latest_tag(monkeypatch, bad):
    monkeypatch.setenv("MAYFLY_KUBEDOCK_IMAGE", bad)
    with pytest.raises(ValueError):
        override_image("kubedock")


@pytest.mark.parametrize(
    "good", ["reg.io:5000/kubedock:dev-1", "kubedock@sha256:abc", "a/b:1.2.3"]
)
def test_override_accepts_tags_and_digests(monkeypatch, good):
    monkeypatch.setenv("MAYFLY_KUBEDOCK_IMAGE", good)
    assert override_image("kubedock") == good


def test_blank_override_is_ignored(monkeypatch):
    monkeypatch.setenv("MAYFLY_KUBEDOCK_IMAGE", "   ")
    assert override_image("kubedock") is None


def test_pinned_images_are_digest_pinned():
    for component in OVERRIDE_ENV:
        assert "@sha256:" in pinned_image(component), component


# -------------------------------------------------------------------- CLI


def _render(tmp_path, monkeypatch, env: dict[str, str]):
    from mayfly.cli import app

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = tmp_path / "env.yaml"
    spec.write_text("seed: override-test\n")
    return CliRunner().invoke(app, ["render", str(spec)])


def test_render_announces_and_records_overrides(tmp_path, monkeypatch):
    result = _render(tmp_path, monkeypatch, {"MAYFLY_KUBEDOCK_IMAGE": "mayfly-override/kubedock:dev-1"})
    assert result.exit_code == 0, result.output
    assert "dev override kubedock -> mayfly-override/kubedock:dev-1" in result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]  # stdout stays pure YAML
    assert docs[0]["mayfly"]["devOverrides"] == {"kubedock": "mayfly-override/kubedock:dev-1"}


def test_render_without_overrides_has_no_header_key(tmp_path, monkeypatch):
    result = _render(tmp_path, monkeypatch, {})
    assert result.exit_code == 0, result.output
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert "devOverrides" not in docs[0]["mayfly"]
    assert "dev override" not in result.stderr


def test_bad_override_is_a_clean_cli_error(tmp_path, monkeypatch):
    result = _render(tmp_path, monkeypatch, {"MAYFLY_KUBEDOCK_IMAGE": "kubedock:latest"})
    assert result.exit_code == 1
    assert "latest" in result.stderr
    assert "Traceback" not in result.output


# ------------------------------------------------------ scripts/overrides.sh


def _plan(tmp_path):
    return subprocess.run(
        [str(SCRIPT), "plan"],
        env={"MAYFLY_OVERRIDES_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_plan_empty(tmp_path):
    r = _plan(tmp_path)
    assert r.returncode == 0 and r.stdout == ""


def test_plan_detects_modes(tmp_path):
    (tmp_path / "ministack" / "rootfs" / "opt").mkdir(parents=True)  # overlay
    (tmp_path / "kubedock").mkdir()
    (tmp_path / "kubedock" / "go.mod").write_text("module x\n\ngo 1.26.3\n")  # source
    fa = tmp_path / "floci-az"
    (fa / "docker").mkdir(parents=True)
    (fa / "pom.xml").write_text("<project/>")
    (fa / "docker" / "Dockerfile.jvm-package").write_text("FROM x\n")
    r = _plan(tmp_path)
    assert r.returncode == 0, r.stderr
    rows = {line.split()[0]: line.split()[1:] for line in r.stdout.splitlines()}
    assert rows == {
        "kubedock": ["source", "MAYFLY_KUBEDOCK_IMAGE"],
        "ministack": ["overlay", "MAYFLY_MINISTACK_IMAGE"],
        "floci-az": ["source", "MAYFLY_FLOCI_AZ_IMAGE"],
    }


def test_plan_follows_symlinked_checkouts(tmp_path):
    checkout = tmp_path / "elsewhere" / "kubedock"
    checkout.mkdir(parents=True)
    (checkout / "go.mod").write_text("module x\n")
    overrides = tmp_path / "overrides"
    overrides.mkdir()
    (overrides / "kubedock").symlink_to(checkout)
    r = _plan(overrides)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["kubedock", "source", "MAYFLY_KUBEDOCK_IMAGE"]


def test_plan_rejects_unknown_component(tmp_path):
    (tmp_path / "localstack").mkdir()
    r = _plan(tmp_path)
    assert r.returncode != 0
    assert "unknown component" in r.stderr


def test_plan_rejects_unrecognized_contents(tmp_path):
    (tmp_path / "kubedock").mkdir()
    (tmp_path / "kubedock" / "notes.txt").write_text("x")
    r = _plan(tmp_path)
    assert r.returncode != 0
    assert "neither a rootfs/ overlay" in r.stderr


def test_plan_ignores_hidden_entries(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".env").write_text("export X=1\n")
    r = _plan(tmp_path)
    assert r.returncode == 0 and r.stdout == ""


def test_script_env_vars_match_mayfly(tmp_path):
    # the script derives var names on its own; they must agree with what
    # mayfly reads, for every component
    for component in OVERRIDE_ENV:
        (tmp_path / component / "rootfs").mkdir(parents=True)
    r = _plan(tmp_path)
    assert r.returncode == 0, r.stderr
    got = {line.split()[0]: line.split()[2] for line in r.stdout.splitlines()}
    assert got == OVERRIDE_ENV


def test_repo_overrides_dir_is_gitignored():
    gi = (Path(__file__).parent.parent / "overrides" / ".gitignore").read_text().splitlines()
    assert "*" in gi and "!.gitignore" in gi
