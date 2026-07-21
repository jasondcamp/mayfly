"""Service provisioners and backend resolution.

Two backends per container-service class:

- ``emulator``: provision through the emulator's real AWS API (boto3 against
  ``aws:4566``). Available where the chosen emulator's container path works
  through kubedock (see emulators.EMULATORS.api_backed).
- ``native``: mayfly deploys the backing container (postgres/valkey/redpanda)
  directly as a Deployment + Service.

``backend: auto`` (the default) picks ``emulator`` when the chosen emulator
supports that service class, else ``native``. The Secret contract is
identical either way. S3 always goes through the emulator API (in-process
in both emulators).

Endpoints written into Secrets are always cluster-internal (reachable from
pods in the namespace), never the CLI's forwarded localhost address.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ..emulators import api_backed_services
from ..k8s import K8s
from ..spec import Backend, EnvSpec
from .aws import (
    AlbProvisioner,
    SecretsManagerProvisioner,
    DynamoProvisioner,
    ElastiCacheProvisioner,
    MskHybridProvisioner,
    RdsProvisioner,
    S3Provisioner,
)
from .native import (
    ElastiCacheNativeProvisioner,
    MskNativeProvisioner,
    RdsNativeProvisioner,
)


@dataclass
class ProvisionContext:
    k8s: K8s
    namespace: str
    session_factory: Callable  # ()-> object with .client(service) -> boto3 client
    progress: Callable[[str], None]


_EMULATOR = {
    "rds": RdsProvisioner,
    "elasticache": ElastiCacheProvisioner,
    # hybrid: native broker + control-plane registration in the emulator
    "msk": MskHybridProvisioner,
    "dynamodb": DynamoProvisioner,  # in-process; no native backend exists
    "alb": AlbProvisioner,  # needs the patched ministack image (data plane)
    "secretsmanager": SecretsManagerProvisioner,  # in-process
}
_NATIVE = {
    "rds": RdsNativeProvisioner,
    "elasticache": ElastiCacheNativeProvisioner,
    "msk": MskNativeProvisioner,
}


def resolve_backend(backend: Backend, svc_class: str, spec: EnvSpec) -> str:
    if backend != "auto":
        return backend
    return "emulator" if svc_class in api_backed_services(spec.emulator) else "native"


def provision_all(spec: EnvSpec, ctx: ProvisionContext) -> dict[str, dict[str, str]]:
    """Run all provisioners for the spec. Returns secrets to write."""
    secrets: dict[str, dict[str, str]] = {}
    secrets.update(S3Provisioner().provision(spec.services.s3.buckets, ctx))
    for svc_class, items in (
        ("rds", spec.services.rds),
        ("elasticache", spec.services.elasticache),
        ("msk", spec.services.msk),
        ("dynamodb", spec.services.dynamodb),
        ("alb", spec.services.alb),
        ("secretsmanager", spec.services.secretsmanager),
    ):
        for backend in ("emulator", "native"):
            chosen = [i for i in items if resolve_backend(i.backend, svc_class, spec) == backend]
            if chosen:
                registry = _EMULATOR if backend == "emulator" else _NATIVE
                if svc_class not in registry:
                    raise ValueError(
                        f"{svc_class} has no {backend} backend "
                        f"(remove 'backend: {backend}' from the spec)"
                    )
                secrets.update(registry[svc_class]().provision(chosen, ctx))
    return secrets
