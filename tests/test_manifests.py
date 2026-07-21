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
        + app_manifests("echo", AppSpec(image="e:1"), "env-x")
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
    (dep, svc) = app_manifests("myapi", app, "ns1")
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
    (dep, _svc) = app_manifests("echo", AppSpec(image="e:1"), "env-x")
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
    (dep, _svc) = app_manifests("web", AppSpec(image="i:1", secrets=["rds-appdb"]), "ns1")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "rds-appdb"}} in container["envFrom"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AWS_ENDPOINT_URL"] == f"http://{AWS_SERVICE}:{AWS_PORT}"


def test_app_ingress_default_host_and_optout():
    with_ing = app_manifests(
        "dragonfly", AppSpec(image="d:1", port=8080, ingress={}), "merry-blonde-stoat"
    )
    ing = next(m for m in with_ing if m["kind"] == "Ingress")
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == "dragonfly.merry-blonde-stoat.localtest.me"
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "dragonfly", "port": {"number": 8080}}
    assert "ingressClassName" not in ing["spec"]

    custom = app_manifests(
        "dragonfly",
        AppSpec(image="d:1", ingress={"host": "status.example.com", "className": "alb"}),
        "ns1",
    )
    ing = next(m for m in custom if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "status.example.com"
    assert ing["spec"]["ingressClassName"] == "alb"

    without = app_manifests("echo", AppSpec(image="e:1"), "ns1")
    assert not any(m["kind"] == "Ingress" for m in without)


def test_app_secret_prefixes():
    (dep, _svc) = app_manifests(
        "watcher",
        AppSpec(
            image="d:1",
            secrets=["rds-appdb", {"name": "elasticache-cache-a", "prefix": "CACHE_A_"}],
        ),
        "ns1",
    )
    env_from = dep["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert env_from == [
        {"secretRef": {"name": "rds-appdb"}},
        {"secretRef": {"name": "elasticache-cache-a"}, "prefix": "CACHE_A_"},
    ]


def test_app_ingress_alb_shape():
    manifests = app_manifests(
        "hello",
        AppSpec(
            image="h:1",
            ingress={
                "host": "*",
                "className": "alb",
                "annotations": {"alb.ingress.kubernetes.io/scheme": "internet-facing"},
            },
        ),
        "ns1",
    )
    ing = next(m for m in manifests if m["kind"] == "Ingress")
    assert "host" not in ing["spec"]["rules"][0]  # "*" -> match any host
    assert ing["spec"]["ingressClassName"] == "alb"
    assert (
        ing["metadata"]["annotations"]["alb.ingress.kubernetes.io/scheme"]
        == "internet-facing"
    )


def test_app_patch_merges_and_reasserts_invariants():
    from mayfly.manifests import merge_patch

    app = AppSpec(
        image="i:1",
        port=3000,
        patch={
            "spec": {
                "template": {
                    "metadata": {"labels": {"team": "core", "app": "hijacked"}},
                    "spec": {
                        "enableServiceLinks": True,  # must be re-asserted off
                        "tolerations": [{"key": "gpu", "operator": "Exists"}],
                        "containers": [
                            {
                                "name": "myapi",  # merges into existing by name
                                "volumeMounts": [{"name": "cfg", "mountPath": "/etc/app"}],
                            },
                            {"name": "sidecar", "image": "envoy:1"},  # appends
                        ],
                        "volumes": [{"name": "cfg", "configMap": {"name": "myapi-cfg"}}],
                    },
                },
                "selector": {"matchLabels": {"app": "hijacked"}},
            }
        },
    )
    (dep, _svc) = app_manifests("myapi", app, "ns1")
    pod = dep["spec"]["template"]["spec"]
    # invariants survive hostile patches
    assert dep["spec"]["selector"] == {"matchLabels": {"app": "myapi"}}
    assert dep["spec"]["template"]["metadata"]["labels"]["app"] == "myapi"
    assert dep["spec"]["template"]["metadata"]["labels"]["team"] == "core"
    assert pod["enableServiceLinks"] is False
    # additions land
    assert pod["tolerations"] == [{"key": "gpu", "operator": "Exists"}]
    assert pod["volumes"] == [{"name": "cfg", "configMap": {"name": "myapi-cfg"}}]
    names = [c["name"] for c in pod["containers"]]
    assert names == ["myapi", "sidecar"]
    main = pod["containers"][0]
    assert main["image"] == "i:1"  # curated field survives named-merge
    assert main["volumeMounts"] == [{"name": "cfg", "mountPath": "/etc/app"}]

    # list-merge semantics directly
    assert merge_patch([1, 2], [3]) == [3]  # unnamed lists replace
    assert merge_patch({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}


def test_app_ingress_patch():
    app = AppSpec(
        image="i:1",
        ingress={
            "host": "x.example.com",
            "patch": {
                "metadata": {
                    "name": "hijacked",
                    "annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "true"},
                },
                "spec": {"tls": [{"hosts": ["x.example.com"], "secretName": "x-tls"}]},
            },
        },
    )
    manifests = app_manifests("web", app, "ns1")
    ing = next(m for m in manifests if m["kind"] == "Ingress")
    assert ing["metadata"]["name"] == "web"  # re-asserted
    assert ing["metadata"]["annotations"]["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
    assert ing["spec"]["tls"] == [{"hosts": ["x.example.com"], "secretName": "x-tls"}]
    assert ing["spec"]["rules"][0]["host"] == "x.example.com"  # generated rule intact


def test_app_tcp_app_shape():
    app = AppSpec(
        image="edoburu/pgbouncer:v1.24.1-p1",
        port=5432,
        servicePort=5432,
        readiness={"tcp": True},
    )
    (dep, svc) = app_manifests("pgbouncer", app, "ns1")
    probe = dep["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert probe["tcpSocket"] == {"port": 5432}
    assert "httpGet" not in probe
    assert svc["spec"]["ports"] == [{"port": 5432, "targetPort": 5432}]


def test_app_service_port_default_unchanged():
    (_dep, svc) = app_manifests("web", AppSpec(image="i:1", port=3000), "ns1")
    assert svc["spec"]["ports"] == [{"port": 8080, "targetPort": 3000}]


def test_app_checks_derived_from_readiness():
    from mayfly.manifests import app_checks

    apps = {
        "hello": AppSpec(image="h:1", port=8080, readiness={"path": "/healthz"}),
        "pgbouncer": AppSpec(image="p:1", port=5432, servicePort=5432, readiness={"tcp": True}),
        "noprobe": AppSpec(image="n:1"),
        "off": AppSpec(image="o:1", enabled=False, readiness={"path": "/"}),
    }
    checks = app_checks(apps)
    assert {"name": "hello", "kind": "http", "target": "http://hello:8080/healthz"} in checks
    assert {"name": "pgbouncer", "kind": "tcp", "target": "pgbouncer:5432"} in checks
    assert len(checks) == 2  # no readiness / disabled -> no check


def test_app_env_carries_identity_and_checks():
    (dep, _svc) = app_manifests(
        "web", AppSpec(image="i:1"), "ns1", checks_json='[{"name":"web"}]'
    )
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MAYFLY_APP_NAME"] == "web"
    assert env["MAYFLY_APP_CHECKS"] == '[{"name":"web"}]'


def test_aws_api_ingress_opt_in():
    for kind in ("ministack", "floci"):
        closed = emulator_manifests(EmulatorSpec(kind=kind), "env-q")
        assert not any(m["kind"] == "Ingress" for m in closed)  # default OFF
        opened = emulator_manifests(EmulatorSpec(kind=kind, expose=True), "env-q")
        ing = next(m for m in opened if m["kind"] == "Ingress")
        assert ing["spec"]["rules"][0]["host"] == "aws.env-q.localtest.me"
