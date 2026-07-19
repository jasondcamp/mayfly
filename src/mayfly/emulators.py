"""AWS emulator registry: pinned images and per-namespace manifests.

Every environment runs its own emulator stack inside its namespace behind a
single Service named ``aws`` on port 4566 — apps and provisioners use
``http://aws:4566`` regardless of which emulator backs it.

Topologies (validated 2026-07-18 on k3d + kubedock):

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
"""

from dataclasses import dataclass
from typing import Optional

from .spec import EmulatorSpec, EnvSpec

AWS_SERVICE = "aws"
AWS_PORT = 4566
AWS_ENDPOINT = f"http://{AWS_SERVICE}:{AWS_PORT}"

RDS_BASE_PORT = 15432
CACHE_BASE_PORT = 16379
KAFKA_PORT = 9092
PORT_RANGE = 8  # per service class, pre-exposed on the aws Service


def msk_bootstrap(spec: EnvSpec) -> Optional[str]:
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
    digest: Optional[str]
    # service classes provisioned through the emulator's AWS API; everything
    # else falls back to the native backend
    api_backed: frozenset[str]


EMULATORS: dict[str, EmulatorInfo] = {
    "ministack": EmulatorInfo(
        image="ministackorg/ministack",
        version="1.4.3",
        digest="sha256:22a278f078f5f88b3437abd1a4daea101bbb1b3d5d7e35353c39029a6ade09e0",
        # msk is hybrid: control plane in ministack (MINISTACK_MSK_BOOTSTRAP
        # routes GetBootstrapBrokers to the broker mayfly deploys natively);
        # the Kafka wire protocol itself is served by that broker.
        api_backed=frozenset({"s3", "rds", "elasticache", "msk"}),
    ),
    "floci": EmulatorInfo(
        image="floci/floci",
        version="1.5.33",
        digest="sha256:d2ecc8035822b23b8587a56eab15edd825f41d3fb80d93e8e66680410beddc08",
        api_backed=frozenset({"s3"}),
    ),
}


def resolve_image(em: EmulatorSpec) -> str:
    """Full image ref for the emulator, digest-pinned when using defaults."""
    info = EMULATORS[em.kind]
    image = em.image or info.image
    version = em.version or info.version
    ref = f"{image}:{version}"
    if em.image is None and em.version is None and info.digest:
        ref += f"@{info.digest}"
    return ref


def api_backed_services(em: EmulatorSpec) -> frozenset[str]:
    return EMULATORS[em.kind].api_backed


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


def _kubedock_container() -> dict:
    return {
        "name": "kubedock",
        "image": KUBEDOCK_IMAGE,
        "args": [
            "server",
            "--listen-addr=:2475",
            "--namespace=$(POD_NAMESPACE)",
            "--service-account=kubedock",
            "--reverse-proxy",
        ],
        "env": [
            {
                "name": "POD_NAMESPACE",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
            }
        ],
        "ports": [{"containerPort": 2475}],
        "resources": {
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"memory": "256Mi"},
        },
    }


def _aws_service(extra_ports: bool) -> dict:
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
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": AWS_SERVICE},
        "spec": {"selector": {"app": AWS_SERVICE}, "ports": ports},
    }


def _deployment(containers: list[dict], service_account: Optional[str] = None) -> dict:
    pod_spec: dict = {"enableServiceLinks": False, "containers": containers}
    if service_account:
        pod_spec["serviceAccountName"] = service_account
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": AWS_SERVICE, "labels": {"app": AWS_SERVICE}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": AWS_SERVICE}},
            "template": {
                "metadata": {"labels": {"app": AWS_SERVICE}},
                "spec": pod_spec,
            },
        },
    }


def _readiness() -> dict:
    # both emulators answer the localstack-compatible health path on 4566
    return {
        "httpGet": {"path": "/_localstack/health", "port": AWS_PORT},
        "initialDelaySeconds": 2,
        "periodSeconds": 2,
    }


def emulator_manifests(
    em: EmulatorSpec, namespace: str, msk_bootstrap: Optional[str] = None
) -> list[dict]:
    image = resolve_image(em)
    if em.kind == "ministack":
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
            "image": image,
            "ports": [{"containerPort": AWS_PORT}],
            "env": _env(env),
            "readinessProbe": _readiness(),
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"memory": "512Mi"},
            },
        }
        return [
            *_kubedock_rbac(namespace),
            _deployment([_kubedock_container(), ministack], service_account="kubedock"),
            _aws_service(extra_ports=True),
        ]

    floci = {
        "name": "floci",
        "image": image,
        "ports": [{"containerPort": AWS_PORT}],
        "env": _env({"FLOCI_HOSTNAME": AWS_SERVICE, "FLOCI_STORAGE_MODE": "memory"}),
        "readinessProbe": _readiness(),
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"memory": "512Mi"},
        },
    }
    return [_deployment([floci]), _aws_service(extra_ports=False)]
