from mayfly.provisioners import resolve_backend
from mayfly.spec import EnvSpec


def _spec(kind: str) -> EnvSpec:
    return EnvSpec.model_validate({"seed": "x", "emulator": {"kind": kind}})


def test_auto_on_ministack():
    spec = _spec("ministack")
    assert resolve_backend("auto", "rds", spec) == "emulator"
    assert resolve_backend("auto", "s3", spec) == "emulator"
    assert resolve_backend("auto", "elasticache", spec) == "emulator"
    # msk "emulator" backend is the hybrid: native broker + control-plane registration
    assert resolve_backend("auto", "msk", spec) == "emulator"


def test_auto_on_floci_is_native_for_containers():
    spec = _spec("floci")
    assert resolve_backend("auto", "rds", spec) == "native"
    assert resolve_backend("auto", "s3", spec) == "emulator"


def test_explicit_override_wins():
    spec = _spec("ministack")
    assert resolve_backend("native", "rds", spec) == "native"
    assert resolve_backend("emulator", "elasticache", spec) == "emulator"


def test_latest_version_rejected():
    import pytest
    with pytest.raises(ValueError):
        EnvSpec.model_validate({"seed": "x", "emulator": {"version": "latest"}})
