"""Unit tests for the Azure provisioners (fake clients, fake k8s)."""

from types import SimpleNamespace

import pytest

from mayfly.provisioners import (
    ProvisionContext,
    provision_all,
    resolve_backend,
)
from mayfly.provisioners.azure import (
    KeyVaultProvisioner,
    ServiceBusProvisioner,
    keyvault_url,
    servicebus_connection_string,
)
from mayfly.provisioners.native import (
    MSSQL_IMAGE,
    MSSQL_PASSWORD,
    MSSQL_USER,
    AzureSqlNativeProvisioner,
    PostgresFlexibleNativeProvisioner,
)
from mayfly.spec import EnvSpec

# ------------------------------------------------------------------- fakes


class FakeK8s:
    def __init__(self):
        self.applied: list[dict] = []
        self.waited: list[str] = []
        self.execs: list[list[str]] = []

    def apply(self, manifest, namespace=None):
        self.applied.append(manifest)

    def apply_all(self, manifests, namespace=None):
        self.applied.extend(manifests)

    def wait_deployment(self, namespace, name, timeout=300):
        self.waited.append(name)

    def exec_in_deployment(self, namespace, name, command):
        self.execs.append(command)

    def by_kind(self, kind):
        return [m for m in self.applied if m["kind"] == kind]


class FakeSecretClient:
    """Mimics azure.keyvault.secrets.SecretClient (the slice we use)."""

    def __init__(self, store: dict):
        self.store = store

    def list_properties_of_secrets(self):
        return [SimpleNamespace(name=n) for n in self.store]

    def set_secret(self, name, value):
        self.store[name] = value

    def get_secret(self, name):
        return SimpleNamespace(name=name, value=self.store[name])


class FakeSbAdmin:
    """Mimics ServiceBusAdministrationClient (the slice we use)."""

    def __init__(self, state: dict):
        self.state = state  # {"queues": set(), "topics": {name: set(subs)}}

    def list_queues(self):
        return [SimpleNamespace(name=n) for n in self.state["queues"]]

    def list_topics(self):
        return [SimpleNamespace(name=n) for n in self.state["topics"]]

    def list_subscriptions(self, topic):
        return [SimpleNamespace(name=n) for n in self.state["topics"][topic]]

    def create_queue(self, name):
        self.state["queues"].add(name)

    def create_topic(self, name):
        self.state["topics"].setdefault(name, set())

    def create_subscription(self, topic, name):
        self.state["topics"][topic].add(name)


class FakeAzureClients:
    def __init__(self, kv_stores: dict, sb_state: dict):
        self.kv_stores = kv_stores  # {vault: {secret: value}}
        self.sb_state = sb_state

    def keyvault(self, vault):
        return FakeSecretClient(self.kv_stores.setdefault(vault, {}))

    def servicebus_admin(self, namespace):
        return FakeSbAdmin(self.sb_state)


def _ctx(k8s=None, kv_stores=None, sb_state=None):
    kv_stores = kv_stores if kv_stores is not None else {}
    sb_state = sb_state if sb_state is not None else {"queues": set(), "topics": {}}
    clients = FakeAzureClients(kv_stores, sb_state)
    return (
        ProvisionContext(
            k8s=k8s or FakeK8s(),
            namespace="env-t",
            session_factory=None,
            progress=lambda m: None,
            azure_clients=lambda: clients,
        ),
        kv_stores,
        sb_state,
    )


EMULATORS = {"aws": {"kind": "ministack"}, "azure": {"kind": "floci-az"}}


def _spec(services: dict) -> EnvSpec:
    return EnvSpec.model_validate({"seed": "x", "emulators": EMULATORS, "services": services})


# ---------------------------------------------------------------- keyvault


def test_keyvault_creates_and_mirrors():
    spec = _spec(
        {
            "keyvault": [
                {
                    "name": "app-secrets",
                    "secrets": [
                        {"name": "api-key", "value": "test-key-123"},
                        {"name": "signing-key", "generate": True},
                    ],
                }
            ]
        }
    )
    ctx, kv_stores, _ = _ctx()
    out = KeyVaultProvisioner().provision(spec.services.keyvault, ctx)
    data = out["keyvault-app-secrets"]
    # cluster-internal URL, never the CLI's forwarded endpoint
    assert data["KEYVAULT_URL"] == "http://azure:4577/app-secrets-keyvault"
    assert data["KEYVAULT_NAME"] == "app-secrets"
    assert data["SECRET_API_KEY"] == "test-key-123"
    assert len(data["SECRET_SIGNING_KEY"]) > 20  # generated
    assert kv_stores["app-secrets"]["api-key"] == "test-key-123"


def test_keyvault_reup_converges_value_keeps_generated():
    spec = _spec(
        {
            "keyvault": [
                {
                    "name": "vault",
                    "secrets": [
                        {"name": "api-key", "value": "new-value"},
                        {"name": "signing-key", "generate": True},
                    ],
                }
            ]
        }
    )
    kv_stores = {"vault": {"api-key": "old-value", "signing-key": "keep-me"}}
    ctx, _, _ = _ctx(kv_stores=kv_stores)
    out = KeyVaultProvisioner().provision(spec.services.keyvault, ctx)
    assert out["keyvault-vault"]["SECRET_API_KEY"] == "new-value"  # converged
    assert out["keyvault-vault"]["SECRET_SIGNING_KEY"] == "keep-me"  # not rotated


def test_keyvault_empty_vault_still_mirrors_url():
    spec = _spec({"keyvault": [{"name": "vault"}]})
    ctx, _, _ = _ctx()
    out = KeyVaultProvisioner().provision(spec.services.keyvault, ctx)
    assert set(out["keyvault-vault"]) == {"KEYVAULT_URL", "KEYVAULT_NAME"}


def test_keyvault_url_helper():
    assert keyvault_url("v") == "http://azure:4577/v-keyvault"


# -------------------------------------------------------------- servicebus


def test_servicebus_creates_entities_and_contract():
    spec = _spec(
        {
            "servicebus": [
                {
                    "name": "main",
                    "queues": ["jobs", "emails"],
                    "topics": [{"name": "events", "subscriptions": ["worker", "audit"]}],
                }
            ]
        }
    )
    ctx, _, sb_state = _ctx()
    out = ServiceBusProvisioner().provision(spec.services.servicebus, ctx)
    assert sb_state["queues"] == {"jobs", "emails"}
    assert sb_state["topics"] == {"events": {"worker", "audit"}}
    data = out["servicebus-main"]
    assert data["SERVICEBUS_NAMESPACE"] == "main"
    assert data["SERVICEBUS_QUEUES"] == "jobs,emails"
    assert data["SERVICEBUS_TOPICS"] == "events"
    cs = data["SERVICEBUS_CONNECTION_STRING"]
    assert "sb://azure:5673/" in cs
    assert "UseDevelopmentEmulator=true" in cs
    assert "localhost" not in cs


def test_servicebus_idempotent_reup():
    spec = _spec({"servicebus": [{"name": "main", "queues": ["jobs"]}]})
    sb_state = {"queues": {"jobs"}, "topics": {}}

    class ExplodingAdmin(FakeSbAdmin):
        def create_queue(self, name):
            raise AssertionError("queue already exists; must not re-create")

    clients = FakeAzureClients({}, sb_state)
    clients.servicebus_admin = lambda ns: ExplodingAdmin(sb_state)
    ctx = ProvisionContext(
        k8s=FakeK8s(), namespace="env-t", session_factory=None,
        progress=lambda m: None, azure_clients=lambda: clients,
    )
    out = ServiceBusProvisioner().provision(spec.services.servicebus, ctx)
    assert out["servicebus-main"]["SERVICEBUS_QUEUES"] == "jobs"


def test_servicebus_connection_string_helper():
    cs = servicebus_connection_string("main")
    assert cs.startswith("Endpoint=sb://azure:5673/;")
    assert "SharedAccessKeyName=RootManageSharedAccessKey" in cs


# ----------------------------------------------------------------- pgflex


def test_pgflex_native_manifests_and_contract():
    spec = _spec({"postgresflexible": [{"name": "appdb", "dbName": "orders"}]})
    k8s = FakeK8s()
    ctx, _, _ = _ctx(k8s=k8s)
    out = PostgresFlexibleNativeProvisioner().provision(spec.services.postgresflexible, ctx)
    (deploy,) = k8s.by_kind("Deployment")
    (svc,) = k8s.by_kind("Service")
    pod = deploy["spec"]["template"]["spec"]
    assert pod["enableServiceLinks"] is False
    assert pod["containers"][0]["image"] == "postgres:16-alpine"
    assert svc["metadata"]["name"] == "pgflex-appdb"
    assert k8s.waited == ["pgflex-appdb"]
    data = out["pgflex-appdb"]
    # identical key set to the rds-* postgres contract
    assert set(data) == {"DATABASE_URL", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}
    assert data["DATABASE_URL"] == "postgresql://app:apppass@pgflex-appdb:5432/orders"
    assert data["DB_HOST"] == "pgflex-appdb"
    assert data["DB_PORT"] == "5432"


def test_pgflex_version_selects_image():
    spec = _spec({"postgresflexible": [{"name": "a", "version": "15"}]})
    k8s = FakeK8s()
    ctx, _, _ = _ctx(k8s=k8s)
    PostgresFlexibleNativeProvisioner().provision(spec.services.postgresflexible, ctx)
    (deploy,) = k8s.by_kind("Deployment")
    assert deploy["spec"]["template"]["spec"]["containers"][0]["image"] == "postgres:15-alpine"


# ---------------------------------------------------------------- azuresql


def test_azuresql_native_manifests_and_contract():
    spec = _spec({"azuresql": [{"name": "coredb", "dbName": "orders"}]})
    k8s = FakeK8s()
    ctx, _, _ = _ctx(k8s=k8s)
    out = AzureSqlNativeProvisioner().provision(spec.services.azuresql, ctx)
    (deploy,) = k8s.by_kind("Deployment")
    pod = deploy["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["enableServiceLinks"] is False
    assert container["image"] == MSSQL_IMAGE
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["ACCEPT_EULA"] == "Y"
    assert env["MSSQL_SA_PASSWORD"] == MSSQL_PASSWORD
    # database created via sqlcmd exec once the engine is ready
    assert any("CREATE DATABASE [orders]" in " ".join(c) for c in k8s.execs)
    data = out["azuresql-coredb"]
    assert set(data) == {"DATABASE_URL", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}
    assert data["DATABASE_URL"] == f"mssql://{MSSQL_USER}:{MSSQL_PASSWORD}@azuresql-coredb:1433/orders"
    assert data["DB_PORT"] == "1433"
    assert data["DB_USER"] == "sa"


def test_azuresql_image_pinned_no_latest():
    assert ":latest" not in MSSQL_IMAGE
    assert MSSQL_IMAGE.startswith("mcr.microsoft.com/mssql/server:2022-")


# --------------------------------------------------- backends + fan-out


def test_azure_backend_resolution():
    spec = _spec({})
    assert resolve_backend("auto", "keyvault", spec) == "emulator"
    assert resolve_backend("auto", "servicebus", spec) == "emulator"
    assert resolve_backend("auto", "postgresflexible", spec) == "native"
    assert resolve_backend("auto", "azuresql", spec) == "native"
    assert resolve_backend("native", "keyvault", spec) == "native"  # explicit wins
    # AWS classes still resolve against emulators.aws
    assert resolve_backend("auto", "rds", spec) == "emulator"


def test_provision_all_covers_all_azure_classes():
    spec = _spec(
        {
            "keyvault": [{"name": "vault", "secrets": [{"name": "k", "value": "v"}]}],
            "servicebus": [{"name": "main", "queues": ["jobs"]}],
            "postgresflexible": [{"name": "appdb"}],
            "azuresql": [{"name": "coredb"}],
        }
    )
    ctx, _, _ = _ctx()
    secrets = provision_all(spec, ctx)
    assert set(secrets) == {
        "keyvault-vault",
        "servicebus-main",
        "pgflex-appdb",
        "azuresql-coredb",
    }


def test_provision_all_rejects_impossible_backend():
    spec = _spec({"azuresql": [{"name": "a", "backend": "emulator"}]})
    ctx, _, _ = _ctx()
    with pytest.raises(ValueError, match="no emulator backend"):
        provision_all(spec, ctx)
    spec = _spec({"keyvault": [{"name": "vault", "backend": "native"}]})
    with pytest.raises(ValueError, match="no native backend"):
        provision_all(spec, ctx)


# ------------------------------------------------- real-SDK construction


def test_azure_clients_construct_real_sdk_objects():
    pytest.importorskip("azure.keyvault.secrets")
    pytest.importorskip("azure.servicebus.management")
    from mayfly.provisioners.azure import AzureClients

    from mayfly.provisioners.azure import _emulator_credential

    clients = AzureClients("http://127.0.0.1:14577")
    kv = clients.keyvault("vault")
    # SDK is handed https:// (its TLS enforcement); the downgrade transport
    # rewrites to the emulator's http:// at send time
    assert kv.vault_url == "https://127.0.0.1:14577/vault-keyvault"
    token = _emulator_credential().get_token("https://vault.azure.net/.default")
    assert token.token == "fake-token"
    sb = clients.servicebus_admin("main")
    assert sb is not None


def test_http_downgrade_transport_restores_url_for_retries(monkeypatch):
    # The pipeline re-sends the SAME request object on retry, and the
    # bearer-token policy refuses a non-https URL. Mutating request.url in
    # place turned a retryable 500 into "Bearer token authentication is not
    # permitted for non-TLS protected URLs" (seen live under kubedock).
    pytest.importorskip("azure.core")
    from azure.core.pipeline.transport import RequestsTransport
    from azure.core.rest import HttpRequest

    from mayfly.provisioners.azure import _http_downgrade_transport

    seen = []

    def fake_send(self, request, **kwargs):
        seen.append(request.url)
        if len(seen) == 1:
            raise ConnectionError("transient")
        return "response"

    monkeypatch.setattr(RequestsTransport, "send", fake_send)
    transport = _http_downgrade_transport()
    request = HttpRequest("PUT", "https://127.0.0.1:14577/jobs?api-version=2024-05")
    with pytest.raises(ConnectionError):
        transport.send(request)
    assert request.url.startswith("https://")  # restored even on failure
    assert transport.send(request) == "response"
    assert seen == ["http://127.0.0.1:14577/jobs?api-version=2024-05"] * 2
    assert request.url.startswith("https://")
