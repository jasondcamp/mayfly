from mayfly.emulators import (
    AZURE_EMULATORS,
    AZURE_PORT,
    AZURE_SERVICE,
    SERVICEBUS_AMQP_PORT,
    azure_emulator_manifests,
    emulator_manifests,
    resolve_image,
)
from mayfly.spec import AzureEmulatorSpec, AwsEmulatorSpec


def _pod_specs(manifests):
    return [m["spec"]["template"]["spec"] for m in manifests if m["kind"] == "Deployment"]


def test_azure_default_image_digest_pinned():
    for kind in AZURE_EMULATORS:
        ref = resolve_image(AzureEmulatorSpec(kind=kind))
        assert ref.startswith("floci/floci-az:")
        assert "@sha256:" in ref
        assert ":latest" not in ref


def test_azure_version_override_drops_digest():
    ref = resolve_image(AzureEmulatorSpec(kind="floci-az", version="9.9.9"))
    assert ref == "floci/floci-az:9.9.9"


def test_azure_image_override():
    ref = resolve_image(AzureEmulatorSpec(kind="floci-az", image="mirror.corp/floci-az", version="1.2.3"))
    assert ref == "mirror.corp/floci-az:1.2.3"


def test_azure_pod_colocates_kubedock():
    # Service Bus is Docker-backed: floci-az needs a Docker daemon on its
    # own localhost, same topology as ministack.
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    (pod,) = _pod_specs(manifests)
    names = {c["name"] for c in pod["containers"]}
    assert names == {"kubedock", "floci-az"}
    assert pod["serviceAccountName"] == "kubedock"
    az = next(c for c in pod["containers"] if c["name"] == "floci-az")
    env = {e["name"]: e["value"] for e in az["env"]}
    assert env["DOCKER_HOST"] == "tcp://localhost:2475"
    assert env["FLOCI_HOSTNAME"] == AZURE_SERVICE


def test_azure_pod_disables_service_links():
    # a Service named `azure` would inject AZURE_PORT=tcp://... into
    # siblings; Quarkus-based floci-az misparses *_PORT vars
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    for pod in _pod_specs(manifests):
        assert pod["enableServiceLinks"] is False


def test_azure_service_ports():
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    svc = next(m for m in manifests if m["kind"] == "Service")
    assert svc["metadata"]["name"] == AZURE_SERVICE
    ports = {p["port"] for p in svc["spec"]["ports"]}
    assert ports == {AZURE_PORT, SERVICEBUS_AMQP_PORT}
    assert svc["spec"]["selector"] == {"app": AZURE_SERVICE}


def test_azure_deployment_identity():
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    deploy = next(m for m in manifests if m["kind"] == "Deployment")
    assert deploy["metadata"]["name"] == AZURE_SERVICE
    assert deploy["spec"]["selector"]["matchLabels"] == {"app": AZURE_SERVICE}


def test_azure_kubedock_rbac_present_and_namespaced():
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    kinds = [m["kind"] for m in manifests]
    for kind in ("ServiceAccount", "Role", "RoleBinding"):
        assert kind in kinds
    rb = next(m for m in manifests if m["kind"] == "RoleBinding")
    assert rb["subjects"][0]["namespace"] == "env-a"


def test_azure_expose_ingress():
    manifests = azure_emulator_manifests(
        AzureEmulatorSpec(kind="floci-az", expose=True), "env-a", "envs.example.com"
    )
    ing = next(m for m in manifests if m["kind"] == "Ingress")
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == "az.env-a.envs.example.com"
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": AZURE_SERVICE, "port": {"number": AZURE_PORT}}


def test_azure_no_ingress_by_default():
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    assert not [m for m in manifests if m["kind"] == "Ingress"]


def test_aws_expose_ingress_survived_refactor():
    manifests = emulator_manifests(
        AwsEmulatorSpec(kind="ministack", expose=True), "env-y", None, "envs.example.com"
    )
    ing = next(m for m in manifests if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "aws.env-y.envs.example.com"
    manifests = emulator_manifests(AwsEmulatorSpec(kind="floci", expose=True), "env-y")
    ing = next(m for m in manifests if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "aws.env-y.localtest.me"


def test_readiness_probes():
    # aws emulators: localstack-compatible health on 4566; floci-az: the
    # /_floci/health path its own image HEALTHCHECK uses, on 4577
    manifests = emulator_manifests(AwsEmulatorSpec(kind="ministack"), "env-y")
    (pod,) = _pod_specs(manifests)
    ministack = next(c for c in pod["containers"] if c["name"] == "ministack")
    assert ministack["readinessProbe"]["httpGet"]["path"] == "/_localstack/health"
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    (pod,) = _pod_specs(manifests)
    az = next(c for c in pod["containers"] if c["name"] == "floci-az")
    assert az["readinessProbe"]["httpGet"] == {"path": "/_floci/health", "port": AZURE_PORT}


def test_azure_servicebus_broker_env():
    # without servicebus: no broker env (management-plane classes only)
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    (pod,) = _pod_specs(manifests)
    az = next(c for c in pod["containers"] if c["name"] == "floci-az")
    env = {e["name"]: e["value"] for e in az["env"]}
    assert "FLOCI_AZ_SERVICES_SERVICE_BUS_MOCKED" not in env
    # with servicebus: disable mocking + start the broker on boot
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a", servicebus=True)
    (pod,) = _pod_specs(manifests)
    az = next(c for c in pod["containers"] if c["name"] == "floci-az")
    env = {e["name"]: e["value"] for e in az["env"]}
    assert env["FLOCI_AZ_SERVICES_SERVICE_BUS_MOCKED"] == "false"
    assert env["FLOCI_AZ_SERVICES_SERVICE_BUS_START_ON_BOOT"] == "true"


def _kubedock_args(manifests):
    (pod,) = _pod_specs(manifests)
    return next(c for c in pod["containers"] if c["name"] == "kubedock")["args"]


def test_azure_kubedock_pre_archive():
    # floci-az copies broker.xml + patched Artemis jars before starting the
    # broker; default kubedock would start it on the first copy (unpatched)
    args = _kubedock_args(azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a"))
    assert "--pre-archive" in args
    assert "--reverse-proxy" in args  # azure:5673 reaches the broker through it


def test_aws_kubedock_has_no_pre_archive():
    # ministack's container path is validated without it; keep it unchanged
    args = _kubedock_args(emulator_manifests(AwsEmulatorSpec(kind="ministack"), "env-y"))
    assert "--pre-archive" not in args


def test_azure_endpoint_mode_published():
    # kubedock serves only published ports in the pod and reports every
    # container IP as 127.0.0.1: floci-az must dial daemon host + published port
    manifests = azure_emulator_manifests(AzureEmulatorSpec(kind="floci-az"), "env-a")
    (pod,) = _pod_specs(manifests)
    az = next(c for c in pod["containers"] if c["name"] == "floci-az")
    env = {e["name"]: e["value"] for e in az["env"]}
    assert env["FLOCI_AZ_DOCKER_ENDPOINT_MODE"] == "published"
    assert env["DOCKER_HOST"] == "tcp://localhost:2475"  # daemon host -> localhost
