"""Azure-API provisioners backed by floci-az (+ kubedock for Service Bus).

The Azure SDKs are an optional dependency (``pip install mayfly[azure]``);
everything here imports them lazily so a base install never pays for them.

Key Vault quirk: the SDK enforces HTTPS and a challenge-based auth flow, so
talking to the emulator needs a fake token credential and challenge-resource
verification disabled — see ``AzureClients.keyvault``. This is the same
pattern apps use (documented in docs/docs/spec/services/azure.md).
"""

import secrets as _pysecrets

from ..emulators import AZURE_ENDPOINT, AZURE_SERVICE, SERVICEBUS_AMQP_PORT

# floci-az accepts the well-known Azurite development account (any account
# name works; shared-key signatures are accepted but not verified)
AZURE_ACCOUNT = "devstoreaccount1"
AZURE_ACCOUNT_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def storage_connection_string() -> str:
    """Azurite-style connection string aimed at the cluster-internal
    emulator — the ambient env var many Azure tools pick up unaided."""
    return (
        f"DefaultEndpointsProtocol=http;"
        f"AccountName={AZURE_ACCOUNT};"
        f"AccountKey={AZURE_ACCOUNT_KEY};"
        f"BlobEndpoint={AZURE_ENDPOINT}/{AZURE_ACCOUNT};"
        f"QueueEndpoint={AZURE_ENDPOINT}/{AZURE_ACCOUNT};"
        f"TableEndpoint={AZURE_ENDPOINT}/{AZURE_ACCOUNT};"
    )


def keyvault_url(vault: str) -> str:
    """Cluster-internal data-plane URL for a vault (floci-az path routing)."""
    return f"{AZURE_ENDPOINT}/{vault}-keyvault"


def servicebus_connection_string(namespace: str) -> str:
    """Ready-to-use connection string for the emulator's AMQP data plane."""
    return (
        f"Endpoint=sb://{AZURE_SERVICE}:{SERVICEBUS_AMQP_PORT}/;"
        f"SharedAccessKeyName=RootManageSharedAccessKey;"
        f"SharedAccessKey={AZURE_ACCOUNT_KEY};"
        f"UseDevelopmentEmulator=true"
    )


def _emulator_credential():
    """Fake token credential for the emulator: the Key Vault / Service Bus
    SDKs enforce a challenge-based bearer-token flow even over plain HTTP."""
    import time

    from azure.core.credentials import AccessToken

    class _EmulatorCredential:
        def get_token(self, *scopes, **kwargs):
            return AccessToken("fake-token", int(time.time()) + 3600)

        # newer azure-core token protocol
        def get_token_info(self, *scopes, **kwargs):
            return self.get_token(*scopes, **kwargs)

    return _EmulatorCredential()


def _http_downgrade_transport():
    """RequestsTransport that rewrites https:// -> http:// at send time.

    The Key Vault SDK hard-refuses bearer tokens over non-TLS URLs
    (``_enforce_tls``), so clients must be *constructed* with an https URL;
    this transport downgrades the actual request to the emulator's plain
    HTTP. Verified against floci-az 0.13.0."""
    from azure.core.pipeline.transport import RequestsTransport

    class _HttpDowngradeTransport(RequestsTransport):
        def send(self, request, **kwargs):
            original = request.url
            request.url = original.replace("https://", "http://", 1)
            try:
                return super().send(request, **kwargs)
            finally:
                # restore: the pipeline re-sends this same request object on
                # retry, and the auth policy refuses a non-https URL
                request.url = original

    return _HttpDowngradeTransport()


class AzureClients:
    """Lazy Azure SDK clients aimed at one emulator endpoint.

    ``endpoint`` is where THIS process reaches the emulator (the CLI's
    port-forward), always plain http; the SDKs are handed the https form and
    the downgrade transport rewrites it. URLs written into Secrets always use
    the cluster-internal ``azure:4577`` regardless.
    """

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    @property
    def _https_host(self) -> str:
        return self.endpoint.removeprefix("http://").removeprefix("https://")

    def keyvault(self, vault: str):
        from azure.keyvault.secrets import SecretClient

        return SecretClient(
            vault_url=f"https://{self._https_host}/{vault}-keyvault",
            credential=_emulator_credential(),
            verify_challenge_resource=False,
            transport=_http_downgrade_transport(),
        )

    def servicebus_admin(self, namespace: str):
        from azure.servicebus.management import ServiceBusAdministrationClient

        # the management (ATOM/HTTP) plane rides the REST endpoint on 4577;
        # the SDK forces https:// onto the fqns and drops the constructor's
        # transport when it builds its pipeline, so subclass to reinstate the
        # downgrade transport. (The AMQP data plane on 5673 is separate.)
        transport = _http_downgrade_transport()

        class _EmulatorAdminClient(ServiceBusAdministrationClient):
            def _build_pipeline(self, **kwargs):
                kwargs.setdefault("transport", transport)
                return super()._build_pipeline(**kwargs)

        return _EmulatorAdminClient(
            fully_qualified_namespace=self._https_host,
            credential=_emulator_credential(),
        )


def azure_client_factory(endpoint: str):
    """ProvisionContext.azure_clients factory for a forwarded endpoint."""

    def factory() -> AzureClients:
        return AzureClients(endpoint)

    return factory


class KeyVaultProvisioner:
    """Seed Key Vault secrets. In-process in floci-az; vaults are implicit
    (path-routed per vault name), so only the secrets need creating.

    Same convergence rules as Secrets Manager: literal ``value:`` entries
    converge to the spec on every up; ``generate: true`` entries get a
    random value once and are never rotated by re-ups. Each vault is
    mirrored to a k8s Secret ``keyvault-<vault>`` for apps that consume env
    vars instead of the SDK.
    """

    def provision(self, items, ctx) -> dict:
        secrets = {}
        for vault in items:
            client = ctx.azure_clients().keyvault(vault.name)
            existing = {p.name for p in client.list_properties_of_secrets()}
            data = {
                "KEYVAULT_URL": keyvault_url(vault.name),
                "KEYVAULT_NAME": vault.name,
            }
            for item in vault.secrets:
                if item.name in existing and item.generate:
                    ctx.progress(
                        f"keyvault: {vault.name}/{item.name} exists (generated; kept)"
                    )
                else:
                    value = (
                        item.value
                        if item.value is not None
                        else _pysecrets.token_urlsafe(24)
                    )
                    client.set_secret(item.name, value)
                    ctx.progress(
                        f"keyvault: {vault.name}/{item.name} "
                        + ("converged to spec value" if item.name in existing else "created")
                        + (" (generated)" if item.generate else "")
                    )
                data[item.env_key] = client.get_secret(item.name).value
            ctx.progress(f"keyvault: {vault.name} at {keyvault_url(vault.name)}")
            secrets[f"keyvault-{vault.name}"] = data
        return secrets


class ServiceBusProvisioner:
    """Service Bus namespaces with queues and topics/subscriptions.

    floci-az backs the data plane with a real AMQP broker (Artemis) spawned
    through kubedock — the ministack RDS pattern. Entities are created
    through the management API; apps consume the ready-made connection
    string from the mirrored Secret.
    """

    def provision(self, items, ctx) -> dict:
        secrets = {}
        for ns in items:
            admin = ctx.azure_clients().servicebus_admin(ns.name)
            queues = {q.name for q in admin.list_queues()}
            topics = {t.name for t in admin.list_topics()}
            for queue in ns.queues:
                if queue not in queues:
                    admin.create_queue(queue)
                ctx.progress(f"servicebus: {ns.name} queue {queue}")
            for topic in ns.topics:
                if topic.name not in topics:
                    admin.create_topic(topic.name)
                subs = {s.name for s in admin.list_subscriptions(topic.name)}
                for sub in topic.subscriptions:
                    if sub not in subs:
                        admin.create_subscription(topic.name, sub)
                ctx.progress(
                    f"servicebus: {ns.name} topic {topic.name}"
                    + (f" ({len(topic.subscriptions)} subscription(s))"
                       if topic.subscriptions else "")
                )
            ctx.progress(
                f"servicebus: {ns.name} ready at "
                f"{AZURE_SERVICE}:{SERVICEBUS_AMQP_PORT} (AMQP)"
            )
            secrets[f"servicebus-{ns.name}"] = {
                "SERVICEBUS_CONNECTION_STRING": servicebus_connection_string(ns.name),
                "SERVICEBUS_NAMESPACE": ns.name,
                "SERVICEBUS_QUEUES": ",".join(ns.queues),
                "SERVICEBUS_TOPICS": ",".join(t.name for t in ns.topics),
            }
        return secrets
