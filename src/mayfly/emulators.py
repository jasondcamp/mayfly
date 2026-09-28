"""Emulator registry: pinned images and per-namespace manifests.

Every environment runs its emulators inside its namespace behind fixed
Services: ``aws`` on 4566 for the AWS emulator, ``azure`` on 4577 (plus
Service Bus AMQP on 5673) for the Azure one — apps and provisioners use
``http://aws:4566`` / ``http://azure:4577`` regardless of which image backs
them. The AWS emulator always deploys; the Azure emulator deploys only when
an Azure service class resolves to the emulator backend.

Topologies (AWS side validated 2026-07-18 on k3d + kubedock):

- ``ministack``: ministack + kubedock colocated in ONE pod. MiniStack's
  container-service readiness checks and port bindings assume the Docker
  daemon is on its own localhost; sharing the pod makes that true, which is
  what lets RDS reach ``available`` and advertise a working ``aws:<port>``
  endpoint. Container-backed AWS services (RDS) work through the real AWS
  API via kubedock.
- ``floci``: floci alone. Its container-backed services need the Docker
  volumes API, which kubedock doesn't implement (501) — so with floci every
  container service uses the native backend and floci serves only the
  in-process AWS APIs (S3 etc.).
- ``floci-az``: floci-az + kubedock in one pod, mirroring ministack:
  Key Vault is in-process, but Service Bus is Docker-backed (Artemis), so
  the emulator needs a Docker daemon on its own localhost.
"""

import os
from dataclasses import dataclass

from .spec import AwsEmulatorSpec, AzureEmulatorSpec, EnvSpec

AWS_SERVICE = "aws"
AWS_PORT = 4566
AWS_ENDPOINT = f"http://{AWS_SERVICE}:{AWS_PORT}"

AZURE_SERVICE = "azure"
AZURE_PORT = 4577
AZURE_ENDPOINT = f"http://{AZURE_SERVICE}:{AZURE_PORT}"
SERVICEBUS_AMQP_PORT = 5673

RDS_BASE_PORT = 15432
CACHE_BASE_PORT = 16379
KAFKA_PORT = 9092
PORT_RANGE = 8  # per service class, pre-exposed on the aws Service


def msk_bootstrap(spec: EnvSpec) -> str | None:
    """Bootstrap-broker string for the natively-deployed MSK brokers."""
    brokers = [f"msk-{m.name}:{KAFKA_PORT}" for m in spec.services.msk]
    return ",".join(brokers) or None


KUBEDOCK_IMAGE = (
    "joyrex2001/kubedock:0.22.0"
    "@sha256:6d7afc5e2c3bfbd686b3dae10a293a110e17ff7156d978f4198af77c8392ad7c"
)


@dataclass(frozen=True)
class EmulatorInfo:
    image: str
    version: str
    digest: str | None
    # service classes provisioned through the emulator's AWS API; everything
    # else falls back to the native backend
    api_backed: frozenset[str]


EMULATORS: dict[str, EmulatorInfo] = {
    "ministack": EmulatorInfo(
        # 1.4.4 includes mayfly's upstreamed ALB data plane (instance/ip
        # targets, ministack#1113) and ElastiCache valkey engine (#1115) —
        # the emulator/ overlay image is retired.
        image="ministackorg/ministack",
        version="1.4.4",
        digest="sha256:7485cab02cd88fe8bfabb3d187fabd71e5f30bd3efe802e1f2d6619a35824f31",
        # msk is hybrid: control plane in ministack (MINISTACK_MSK_BOOTSTRAP
        # routes GetBootstrapBrokers to the broker mayfly deploys natively);
        # the Kafka wire protocol itself is served by that broker.
        api_backed=frozenset({"s3", "rds", "elasticache", "msk", "dynamodb", "alb", "secretsmanager"}),
    ),
    "floci": EmulatorInfo(
        image="floci/floci",
        version="1.5.33",
        digest="sha256:d2ecc8035822b23b8587a56eab15edd825f41d3fb80d93e8e66680410beddc08",
        api_backed=frozenset({"s3", "dynamodb", "secretsmanager"}),  # in-process in floci too
    ),
}

AZURE_EMULATORS: dict[str, EmulatorInfo] = {
    "floci-az": EmulatorInfo(
        image="floci/floci-az",
        version="0.13.0",
        digest="sha256:3a71953fbc0940aa33bbc1c5e88211a320b66812c0840831a9f8558d3d3521c5",
        # keyvault is in-process; servicebus is Docker-backed via kubedock.
        # postgresflexible/azuresql deliberately absent: the faithful backend
        # is a real engine pod (native), not floci-az's Docker-backed engine.
        api_backed=frozenset({"keyvault", "servicebus"}),
    ),
}

_ALL_EMULATORS = {**EMULATORS, **AZURE_EMULATORS}


# Dev overrides: locally built images (see overrides/README.md and
# scripts/overrides.sh) that replace a component's pinned image, for testing
# unreleased emulator/kubedock changes. They win over the spec's image/version
# and the pinned default, and mayfly announces them on every up/render.
OVERRIDE_ENV = {
    "kubedock": "MAYFLY_KUBEDOCK_IMAGE",
    "ministack": "MAYFLY_MINISTACK_IMAGE",
    "floci": "MAYFLY_FLOCI_IMAGE",
    "floci-az": "MAYFLY_FLOCI_AZ_IMAGE",
}


def override_image(component: str) -> str | None:
    """The dev-override image ref for a component, or None when unset."""
    var = OVERRIDE_ENV[component]
    ref = os.environ.get(var, "").strip()
    if not ref:
        return None
    last = ref.rsplit("/", 1)[-1]
    if "@" not in last and ":" not in last:
        raise ValueError(f"{var}={ref!r} needs an explicit tag or digest")
    if last.split("@", 1)[0].endswith(":latest"):
        raise ValueError(f"{var}={ref!r}: 'latest' tags are not allowed")
    return ref


def active_overrides() -> dict[str, str]:
    """Component -> image for every dev override currently set."""
    return {c: ref for c in OVERRIDE_ENV if (ref := override_image(c))}


def pinned_image(component: str) -> str:
    """The digest-pinned default image for a component, ignoring overrides —
    the base that scripts/overrides.sh builds overlays on."""
    if component == "kubedock":
        return KUBEDOCK_IMAGE
    info = _ALL_EMULATORS[component]
    return f"{info.image}:{info.version}@{info.digest}"


def kubedock_image() -> str:
    return override_image("kubedock") or KUBEDOCK_IMAGE


def resolve_image(em: AwsEmulatorSpec | AzureEmulatorSpec) -> str:
    """Full image ref for the emulator, digest-pinned when using defaults.
    A dev override (MAYFLY_<KIND>_IMAGE) wins over everything."""
    override = override_image(em.kind)
    if override:
        return override
    info = _ALL_EMULATORS[em.kind]
    image = em.image or info.image
    version = em.version or info.version
    ref = f"{image}:{version}"
    if em.image is None and em.version is None and info.digest:
        ref += f"@{info.digest}"
    return ref


def api_backed_services(em: AwsEmulatorSpec | AzureEmulatorSpec) -> frozenset[str]:
    return _ALL_EMULATORS[em.kind].api_backed


def _env(env: dict[str, str]) -> list[dict]:
    return [{"name": k, "value": v} for k, v in env.items()]


def _kubedock_rbac(namespace: str) -> list[dict]:
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "kubedock"}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "kubedock"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "pods/log", "pods/exec", "services", "configmaps"],
                    "verbs": ["create", "get", "list", "watch", "delete"],
                },
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "verbs": ["create", "get", "list", "watch", "delete"],
                },
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "kubedock"},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "kubedock",
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": "kubedock", "namespace": namespace}
            ],
        },
    ]


def _kubedock_container(pre_archive: bool = False) -> dict:
    args = [
        "server",
        "--listen-addr=:2475",
        "--namespace=$(POD_NAMESPACE)",
        "--service-account=kubedock",
        "--reverse-proxy",
        # kubedock reaps spawned pods after 1h by default — far shorter
        # than environment TTLs — and released versions have no way to
        # disable the reaper (upstream: joyrex2001/kubedock#290 makes
        # reapmax=0 = disabled;
        # DO NOT pass 0 to current releases, they'd reap immediately).
        # Namespace deletion is mayfly's cleanup, so push the ceiling out
        # a century; switch to --reapmax=0 once the pinned kubedock has
        # the patch.
        "--reapmax=876000h",
        # init containers (volume/pre-archive setup) default to
        # joyrex2001/kubedock:<own version>, which doesn't exist for a
        # locally built kubedock — always run the image kubedock itself uses
        f"--initimage={kubedock_image()}",
    ]
    if pre_archive:
        # Unlike real Docker, kubedock starts a container on the first file
        # copied into it. floci-az does create -> copy broker.xml + patched
        # Artemis jars -> start, so without this the broker boots unpatched.
        # KNOWN GAP (verified 2026-09-26): --pre-archive bundles ALL of a
        # container's pre-start files into ONE ConfigMap, and floci-az
        # 0.13.0's Service Bus payload is ~1.6 MB (two patched jars of
        # 733 KB + 859 KB) — over the 1 MiB object cap, so the broker never
        # starts. Kept anyway: it fails loudly at provisioning instead of
        # running an unpatched broker. See docs/docs/architecture.md.
        args.append("--pre-archive")
    return {
        "name": "kubedock",
        "image": kubedock_image(),
        "args": args,
        "env": [
            {
                "name": "POD_NAMESPACE",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
            }
        ],
        "ports": [{"containerPort": 2475}],
        "resources": {
            "requests": {"cpu": "25m", "memory": "64Mi"},
            # kubedock holds every reverse-proxy listener + container
            # bookkeeping in memory; 256Mi got OOMKilled after ~1 day,
            # which silently severs all aws:<port> data planes
            "limits": {"memory": "768Mi"},
        },
    }


def _emulator_ingress(name: str, host: str, port: int) -> dict:
    """Opt-in (expose:) ingress for an emulator API: laptop CLI/SDK access
    with no port-forward."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name},
        "spec": {
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {"name": name, "port": {"number": port}}
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }


def _emulator_service(name: str, ports: list[dict]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {"selector": {"app": name}, "ports": ports},
    }


def _aws_service_ports(extra_ports: bool) -> list[dict]:
    ports = [{"name": "awsapi", "port": AWS_PORT, "targetPort": AWS_PORT}]
    if extra_ports:
        for i in range(PORT_RANGE):
            ports.append(
                {"name": f"rds-{i}", "port": RDS_BASE_PORT + i, "targetPort": RDS_BASE_PORT + i}
            )
            ports.append(
                {
                    "name": f"cache-{i}",
                    "port": CACHE_BASE_PORT + i,
                    "targetPort": CACHE_BASE_PORT + i,
                }
            )
    return ports


def _deployment(
    name: str, containers: list[dict], service_account: str | None = None
) -> dict:
    pod_spec: dict = {"enableServiceLinks": False, "containers": containers}
    if service_account:
        pod_spec["serviceAccountName"] = service_account
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": pod_spec,
            },
        },
    }


def _aws_readiness() -> dict:
    # both AWS emulators answer the localstack-compatible health path on 4566
    return {
        "httpGet": {"path": "/_localstack/health", "port": AWS_PORT},
        "initialDelaySeconds": 2,
        "periodSeconds": 2,
    }


def _ministack_manifests(
    em: AwsEmulatorSpec, namespace: str, msk_bootstrap: str | None
) -> list[dict]:
    # DOCKER_NETWORK is deliberately NOT set: RDS public-endpoint mode
    # ignores it, and setting it forces ElastiCache down the
    # network-attach path that kubedock rejects. Without it, both
    # services take the published-port branch and advertise
    # MINISTACK_HOST:<base_port+n> — reachable via the aws Service.
    env = {
        "DOCKER_HOST": "tcp://localhost:2475",
        "MINISTACK_HOST": AWS_SERVICE,
        "MINISTACK_RDS_PUBLIC_ENDPOINT": "1",
        "RDS_BASE_PORT": str(RDS_BASE_PORT),
        "ELASTICACHE_BASE_PORT": str(CACHE_BASE_PORT),
    }
    if msk_bootstrap:
        # GetBootstrapBrokers answers with the broker mayfly brings
        env["MINISTACK_MSK_BOOTSTRAP"] = msk_bootstrap
    ministack = {
        "name": "ministack",
        "image": resolve_image(em),
        "ports": [{"containerPort": AWS_PORT}],
        "env": _env(env),
        "readinessProbe": _aws_readiness(),
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"memory": "512Mi"},
        },
    }
    return [
        *_kubedock_rbac(namespace),
        _deployment(AWS_SERVICE, [_kubedock_container(), ministack], "kubedock"),
        _emulator_service(AWS_SERVICE, _aws_service_ports(extra_ports=True)),
    ]


def _floci_manifests(
    em: AwsEmulatorSpec, namespace: str, msk_bootstrap: str | None
) -> list[dict]:
    floci = {
        "name": "floci",
        "image": resolve_image(em),
        "ports": [{"containerPort": AWS_PORT}],
        "env": _env({"FLOCI_HOSTNAME": AWS_SERVICE, "FLOCI_STORAGE_MODE": "memory"}),
        "readinessProbe": _aws_readiness(),
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"memory": "512Mi"},
        },
    }
    return [
        _deployment(AWS_SERVICE, [floci]),
        _emulator_service(AWS_SERVICE, _aws_service_ports(extra_ports=False)),
    ]


_AWS_BUILDERS = {
    "ministack": _ministack_manifests,
    "floci": _floci_manifests,
}


def emulator_manifests(
    em: AwsEmulatorSpec,
    namespace: str,
    msk_bootstrap: str | None = None,
    ingress_domain: str = "localtest.me",
) -> list[dict]:
    manifests = _AWS_BUILDERS[em.kind](em, namespace, msk_bootstrap)
    if em.expose:
        manifests.append(
            _emulator_ingress(AWS_SERVICE, f"aws.{namespace}.{ingress_domain}", AWS_PORT)
        )
    return manifests


def azure_emulator_manifests(
    em: AzureEmulatorSpec,
    namespace: str,
    ingress_domain: str = "localtest.me",
    servicebus: bool = False,
) -> list[dict]:
    """floci-az + kubedock in one pod (the ministack topology: Service Bus is
    Docker-backed, so the emulator needs a Docker daemon on its localhost)."""
    env = {
        "DOCKER_HOST": "tcp://localhost:2475",
        "FLOCI_HOSTNAME": AZURE_SERVICE,
        "FLOCI_STORAGE_MODE": "memory",
        # Reach sidecars at the daemon's host + published port. kubedock reports
        # every container IP as 127.0.0.1 and only reverse-proxies published
        # ports in this pod, so floci-az's default (container IP + internal
        # port) can't reach the Service Bus broker (floci-io/floci-az#331).
        # Releases without the setting ignore it (verified on 0.13.0).
        "FLOCI_AZ_DOCKER_ENDPOINT_MODE": "published",
    }
    if servicebus:
        # Service Bus is management-only ("mocked") by default; disabling that
        # spawns a real Artemis broker via kubedock. start-on-boot brings the
        # default namespace up so the AMQP data plane is ready at up time.
        env["FLOCI_AZ_SERVICES_SERVICE_BUS_MOCKED"] = "false"
        env["FLOCI_AZ_SERVICES_SERVICE_BUS_START_ON_BOOT"] = "true"
    floci_az = {
        "name": "floci-az",
        "image": resolve_image(em),
        "ports": [
            {"containerPort": AZURE_PORT},
            {"containerPort": SERVICEBUS_AMQP_PORT},
        ],
        "env": _env(env),
        # /_floci/health is what floci-az's own image HEALTHCHECK uses
        # (verified 200 on 0.13.0, as is /health)
        "readinessProbe": {
            "httpGet": {"path": "/_floci/health", "port": AZURE_PORT},
            "initialDelaySeconds": 2,
            "periodSeconds": 2,
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"memory": "512Mi"},
        },
    }
    ports = [
        {"name": "azapi", "port": AZURE_PORT, "targetPort": AZURE_PORT},
        {
            "name": "sb-amqp",
            "port": SERVICEBUS_AMQP_PORT,
            "targetPort": SERVICEBUS_AMQP_PORT,
        },
    ]
    manifests = [
        # kubedock RBAC is shared with the aws pod when both deploy: the
        # manifests are identical, and server-side apply converges them.
        *_kubedock_rbac(namespace),
        _deployment(
            AZURE_SERVICE, [_kubedock_container(pre_archive=True), floci_az], "kubedock"
        ),
        _emulator_service(AZURE_SERVICE, ports),
    ]
    if em.expose:
        manifests.append(
            _emulator_ingress(AZURE_SERVICE, f"az.{namespace}.{ingress_domain}", AZURE_PORT)
        )
    return manifests
