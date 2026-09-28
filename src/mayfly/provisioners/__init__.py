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
from ..spec import AZURE_CLASSES, Backend, EnvSpec
from .aws import (
    AlbProvisioner,
    DynamoProvisioner,
    ElastiCacheProvisioner,
    MskHybridProvisioner,
    RdsProvisioner,
    S3Provisioner,
    SecretsManagerProvisioner,
)
from .azure import KeyVaultProvisioner, ServiceBusProvisioner
from .native import (
    AzureSqlNativeProvisioner,
    ElastiCacheNativeProvisioner,
    MskNativeProvisioner,
    PostgresFlexibleNativeProvisioner,
    RdsNativeProvisioner,
)

@dataclass
class ProvisionContext:
    k8s: K8s
    namespace: str
    # ()-> object with .client(service) -> boto3 client; None when the spec
    # declares no AWS emulator
    session_factory: Callable | None
    progress: Callable[[str], None]
    ingress_domain: str = "localtest.me"
    # ()-> object with .keyvault(vault) / .servicebus_admin(namespace);
    # None when the spec declares no Azure emulator
    azure_clients: Callable | None = None


_EMULATOR = {
    "rds": RdsProvisioner,
    "elasticache": ElastiCacheProvisioner,
    # hybrid: native broker + control-plane registration in the emulator
    "msk": MskHybridProvisioner,
    "dynamodb": DynamoProvisioner,  # in-process; no native backend exists
    "alb": AlbProvisioner,  # needs the patched ministack image (data plane)
    "secretsmanager": SecretsManagerProvisioner,  # in-process
    "keyvault": KeyVaultProvisioner,  # in-process in floci-az
    "servicebus": ServiceBusProvisioner,  # Docker-backed (Artemis) via kubedock
}
_NATIVE = {
    "rds": RdsNativeProvisioner,
    "elasticache": ElastiCacheNativeProvisioner,
    "msk": MskNativeProvisioner,
    "postgresflexible": PostgresFlexibleNativeProvisioner,
    "azuresql": AzureSqlNativeProvisioner,
}


def resolve_backend(backend: Backend, svc_class: str, spec: EnvSpec) -> str:
    if backend != "auto":
        return backend
    em = spec.emulators.azure if svc_class in AZURE_CLASSES else spec.emulators.aws
    if em is None:  # spec validation guarantees this for declared classes
        raise ValueError(f"{svc_class} has no emulator declared for its cloud")
    return "emulator" if svc_class in api_backed_services(em) else "native"


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
        ("keyvault", spec.services.keyvault),
        ("servicebus", spec.services.servicebus),
        ("postgresflexible", spec.services.postgresflexible),
        ("azuresql", spec.services.azuresql),
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
