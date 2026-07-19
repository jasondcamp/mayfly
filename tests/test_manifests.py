from mayfly.emulators import (
    AWS_PORT,
    AWS_SERVICE,
    EMULATORS,
    KUBEDOCK_IMAGE,
    RDS_BASE_PORT,
    emulator_manifests,
    resolve_image,
)
from mayfly.manifests import app_manifests
from mayfly.spec import AppSpec, EmulatorSpec


def _pod_specs(manifests):
    return [m["spec"]["template"]["spec"] for m in manifests if m["kind"] == "Deployment"]


def test_all_pods_disable_service_links():
    # The aws Service otherwise injects *_PORT=tcp://... into sibling pods,
    # which Quarkus-based emulators fatally misparse.
    manifests = (
        emulator_manifests(EmulatorSpec(kind="ministack"), "env-x")
        + emulator_manifests(EmulatorSpec(kind="floci"), "env-x")
        + app_manifests("echo", AppSpec(image="e:1"))
    )
    pods = _pod_specs(manifests)
    assert pods, "expected deployments"
    assert all(p["enableServiceLinks"] is False for p in pods)


def test_default_images_are_digest_pinned():
    for kind, info in EMULATORS.items():
        ref = resolve_image(EmulatorSpec(kind=kind))
        assert "@sha256:" in ref, f"{kind} default not digest-pinned: {ref}"
        assert ":latest" not in ref
    assert "@sha256:" in KUBEDOCK_IMAGE


def test_version_override_drops_digest():
    ref = resolve_image(EmulatorSpec(kind="ministack", version="9.9.9"))
    assert ref == "ministackorg/ministack:9.9.9"


def test_ministack_colocates_kubedock():
    manifests = emulator_manifests(EmulatorSpec(kind="ministack"), "env-y")
    (pod,) = _pod_specs(manifests)
    names = {c["name"] for c in pod["containers"]}
    assert names == {"kubedock", "ministack"}
    ministack = next(c for c in pod["containers"] if c["name"] == "ministack")
    env = {e["name"]: e["value"] for e in ministack["env"]}
    # kubedock shares the pod, so the Docker daemon really is on localhost
    assert env["DOCKER_HOST"] == "tcp://localhost:2475"
    assert env["MINISTACK_HOST"] == AWS_SERVICE
    assert env["MINISTACK_RDS_PUBLIC_ENDPOINT"] == "1"
    # DOCKER_NETWORK must stay unset: it forces ElastiCache down the
    # network-attach path kubedock rejects (and RDS public mode ignores it)
    assert "DOCKER_NETWORK" not in env


def test_ministack_msk_bootstrap_env():
    from mayfly.emulators import msk_bootstrap
    from mayfly.spec import EnvSpec

    spec = EnvSpec.model_validate(
        {"seed": "x", "services": {"msk": [{"name": "events"}, {"name": "logs"}]}}
    )
    bootstrap = msk_bootstrap(spec)
    assert bootstrap == "msk-events:9092,msk-logs:9092"
    manifests = emulator_manifests(EmulatorSpec(kind="ministack"), "env-y", bootstrap)
    (pod,) = _pod_specs(manifests)
    ministack = next(c for c in pod["containers"] if c["name"] == "ministack")
    env = {e["name"]: e["value"] for e in ministack["env"]}
    assert env["MINISTACK_MSK_BOOTSTRAP"] == bootstrap
    # no msk in spec -> no env var
    manifests = emulator_manifests(EmulatorSpec(kind="ministack"), "env-y", None)
    (pod,) = _pod_specs(manifests)
    ministack = next(c for c in pod["containers"] if c["name"] == "ministack")
    assert "MINISTACK_MSK_BOOTSTRAP" not in {e["name"] for e in ministack["env"]}


def test_ministack_service_exposes_rds_ports():
    manifests = emulator_manifests(EmulatorSpec(kind="ministack"), "env-y")
    svc = next(m for m in manifests if m["kind"] == "Service")
    ports = {p["port"] for p in svc["spec"]["ports"]}
    assert AWS_PORT in ports
    assert RDS_BASE_PORT in ports


def test_floci_has_no_kubedock():
    manifests = emulator_manifests(EmulatorSpec(kind="floci"), "env-z")
    (pod,) = _pod_specs(manifests)
    assert [c["name"] for c in pod["containers"]] == ["floci"]
    svc = next(m for m in manifests if m["kind"] == "Service")
    assert [p["port"] for p in svc["spec"]["ports"]] == [AWS_PORT]


def test_kubedock_rolebinding_namespaced_subject():
    manifests = emulator_manifests(EmulatorSpec(kind="ministack"), "env-y")
    rb = next(m for m in manifests if m["kind"] == "RoleBinding")
    assert rb["subjects"][0]["namespace"] == "env-y"


def test_app_full_spec_rendering():
    app = AppSpec(
        image="ghcr.io/x/api:1",
        port=3000,
        command=["/bin/server"],
        args=["--verbose"],
        replicas=3,
        resources={"cpu": "100m", "memory": "128Mi", "memoryLimit": "512Mi", "cpuLimit": "1"},
        readiness={"path": "/healthz", "initialDelaySeconds": 5},
        imagePullSecret="regcred",
    )
    (dep, svc) = app_manifests("myapi", app)
    assert dep["spec"]["replicas"] == 3
    pod = dep["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "regcred"}]
    c = pod["containers"][0]
    assert c["command"] == ["/bin/server"]
    assert c["args"] == ["--verbose"]
    assert c["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"memory": "512Mi", "cpu": "1"},
    }
    assert c["readinessProbe"] == {
        "httpGet": {"path": "/healthz", "port": 3000},
        "initialDelaySeconds": 5,
        "periodSeconds": 5,
    }
    assert svc["spec"]["ports"] == [{"port": 8080, "targetPort": 3000}]


def test_app_minimal_defaults_unchanged():
    (dep, _svc) = app_manifests("echo", AppSpec(image="e:1"))
    assert dep["spec"]["replicas"] == 1
    pod = dep["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod
    c = pod["containers"][0]
    assert "command" not in c and "args" not in c and "readinessProbe" not in c
    assert c["resources"] == {
        "requests": {"cpu": "10m", "memory": "32Mi"},
        "limits": {"memory": "256Mi"},
    }


def test_app_secrets_mounted_env_from():
    (dep, _svc) = app_manifests("web", AppSpec(image="i:1", secrets=["rds-appdb"]))
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "rds-appdb"}} in container["envFrom"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AWS_ENDPOINT_URL"] == f"http://{AWS_SERVICE}:{AWS_PORT}"
