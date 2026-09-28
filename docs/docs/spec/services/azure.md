---
sidebar_position: 2
sidebar_label: Azure
---

# Azure services

Azure service classes live alongside the AWS ones under `services:`, and
one environment can mix both clouds freely: declare `emulators.azure`,
`emulators.aws` or both.

There is deliberately **no AKS service class**: the mayfly namespace *is*
the cluster. Apps that run on AKS in production just run as `apps:` here —
AKS is the substrate, not a dependency to emulate.

The Azure provisioners use the official Azure SDKs, shipped as an optional
extra — `pip install 'mayfly-cli[azure]'`. `mayfly up` fails fast with a
clear message if a spec needs them and they're missing. See
`examples/env-azure.yaml` for a full worked spec.

## The Azure emulator

Every Azure class requires `emulators.azure`, including the database
classes that run as native pods (see [emulators](index.md#emulators)).
Declared, the Azure emulator ([floci-az](https://floci.io/az/)) deploys into
the environment's namespace as a Deployment and Service named `azure`:
REST API on `azure:4577` and Service Bus AMQP on `azure:5673`,
digest-pinned like the AWS emulators. The pod pairs floci-az with its own
kubedock sidecar (Service Bus is Docker-backed, the MiniStack topology; see
[architecture](../../architecture.md)).

```yaml
emulators:
  azure:
    kind: floci-az         # the only kind today (required)
    # image: my-mirror/floci-az
    # version: 1.2.3       # pinned tag; never 'latest'
    expose: false          # Azure API at az.<namespace>.<ingressDomain> via the
                           # cluster ingress. Default OFF — the API is
                           # unauthenticated and can read Key Vault values, so
                           # keep it unexposed on shared clusters.
```

## Service classes

Each class supports [`backend: auto | emulator | native`](index.md#backends)
per entry, with the same semantics as the AWS classes — but note that `auto` resolves
differently per class: `keyvault` and `servicebus` are emulator-backed,
while the database classes resolve to **native** (a real engine pod is the
faithful implementation; there is no emulator fallback wired for them).

```yaml
services:
  keyvault:
    - name: app-secrets              # vault name (3-24 chars, alnum + hyphens)
      secrets:
        - name: api-key
          value: "test-key-123"      # literal fixture
        - name: signing-key
          generate: true             # random per env; kept across re-ups

  servicebus:
    - name: main                     # Service Bus namespace
      queues: [jobs, emails]
      topics:
        - name: events
          subscriptions: [worker, audit]

  postgresflexible:
    - name: appdb                    # Azure Database for PostgreSQL (Flexible Server)
      dbName: app
      version: "16"                  # pg major -> container image tag

  azuresql:
    - name: coredb                   # Azure SQL (SQL Server engine)
      dbName: app
```

## Notes per class

- **keyvault** — in-process in the emulator; instant. Seeded secrets follow
  the `secretsmanager` rules: set exactly one of `value:` (a committed test
  fixture — never production credentials) or `generate: true` (random on
  first creation, never rotated by re-ups). Apps can fetch at runtime via
  the Key Vault SDK — see [connecting to Key Vault](#connecting-to-key-vault)
  below for the credential pattern the emulator needs — or consume the
  mirrored k8s Secret.
- **servicebus** — the emulator backs each namespace with a real AMQP data
  plane (an Artemis broker floci-az spawns via kubedock — the same
  mechanism MiniStack uses for RDS). Queues, topics, and subscriptions are
  created at provision time. Each entry needs at least one queue or topic.
  Python clients have two current limits; see
  [connecting to Service Bus](#connecting-to-service-bus).

  :::warning Not working on Kubernetes yet
  floci-az's broker can't start under kubedock today (its startup files
  exceed kubedock's ConfigMap limit — see
  [architecture](../../architecture.md)), so `mayfly up` fails when provisioning
  a `servicebus` entry. Key Vault and both database classes work.
  :::
- **postgresflexible** — a real postgres pod, deployed natively. From an
  app's point of view Flexible Server is postgres with a connection string,
  and the Secret keys are **identical to `rds-*` postgres** — an app moves
  between AWS RDS and Flexible Server with zero env changes.
- **azuresql** — a real SQL Server pod (`mcr.microsoft.com/mssql/server`,
  pinned CU tag), deployed natively; mayfly creates the database via
  `sqlcmd` once the engine accepts logins. Same `DB_*` key names with an
  `mssql://` scheme and port 1433. SQL Server enforces password
  complexity, so the fixture credentials are `sa` + a complexity-passing
  constant rather than the `app`/`apppass` convention (the values in the
  Secret are always authoritative — consume them, don't hardcode). Notes:
  the engine needs ~2GB memory, and the images are amd64-only — on Apple
  Silicon k3d they run under emulation with a slow first start.

## Secrets: the app contract

| Service | Secret | Keys |
|---|---|---|
| keyvault | `keyvault-<vault>` | `KEYVAULT_URL`, `KEYVAULT_NAME`, one `SECRET_<MANGLED_NAME>` per seeded secret (`api-key` → `SECRET_API_KEY`) |
| servicebus | `servicebus-<ns>` | `SERVICEBUS_CONNECTION_STRING`, `SERVICEBUS_NAMESPACE`, `SERVICEBUS_QUEUES`, `SERVICEBUS_TOPICS` |
| postgresflexible | `pgflex-<name>` | `DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` |
| azuresql | `azuresql-<name>` | `DATABASE_URL` (`mssql://`), `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` |

`SERVICEBUS_QUEUES` / `SERVICEBUS_TOPICS` are comma-joined entity names.
`SERVICEBUS_CONNECTION_STRING` is ready to pass straight to the Service Bus
SDK. As everywhere in mayfly, endpoints in Secrets are cluster-internal
(`azure:4577`, `azure:5673`, `pgflex-appdb:5432`, `azuresql-coredb:1433`) —
never localhost.

## App environment

When the spec declares `emulators.azure`, every app and init job gets
(alongside the `AWS_*` bundle, if `emulators.aws` is declared too):

| Env var | Value |
|---|---|
| `AZURE_EMULATOR_ENDPOINT` | `http://azure:4577` |
| `AZURE_STORAGE_CONNECTION_STRING` | Azurite-style connection string aimed at the emulator (`devstoreaccount1` + the well-known development key) |

Spec `env:` entries override both, like everything mayfly injects.

## dragonfly

Every Azure class gets a [dragonfly](../../guides/dragonfly.md) tile with a real
data round-trip: Key Vault secret reads (or a set/get probe on empty
vaults), Service Bus send/receive/complete over AMQP on the namespace's
first queue, and insert+select against both databases. A namespace with
only topics gets a send-only check until floci-az supports Python
subscription receives (see above). The checks are derived from the spec and injected as
`MAYFLY_AZURE_CHECKS` — spec-driven rather than control-plane-discovered,
because floci-az's ARM listing support is unverified and the native
database pods are invisible to any control plane.

## Connecting to Key Vault

The Key Vault SDK refuses to send a bearer token to any non-HTTPS URL,
even with challenge verification disabled, and the emulator only speaks
plain HTTP. The working pattern (verified against floci-az 0.13.0) hands
the SDK the `https://` form of the vault URL plus a transport that
downgrades each request to `http://` at send time, and a fake token
credential. In Python:

```python
import os
import time

from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from azure.keyvault.secrets import SecretClient


class EmulatorCredential:
    def get_token(self, *scopes, **kwargs):
        return AccessToken("fake-token", int(time.time()) + 3600)

    def get_token_info(self, *scopes, **kwargs):  # newer azure-core protocol
        return self.get_token(*scopes, **kwargs)


class HttpDowngradeTransport(RequestsTransport):
    def send(self, request, **kwargs):
        original = request.url
        request.url = original.replace("https://", "http://", 1)
        try:
            return super().send(request, **kwargs)
        finally:
            request.url = original  # retries re-send this object; keep it https


# KEYVAULT_URL (from the keyvault-<vault> Secret) is the emulator's http URL
vault_url = os.environ["KEYVAULT_URL"].replace("http://", "https://", 1)
client = SecretClient(
    vault_url=vault_url,
    credential=EmulatorCredential(),
    verify_challenge_resource=False,
    transport=HttpDowngradeTransport(),
)
print(client.get_secret("api-key").value)
```

Gate this on your app's environment config (the same place you'd branch on
`AWS_ENDPOINT_URL` today); in real Azure, use `DefaultAzureCredential` as
usual.

## Connecting to Service Bus

Pass `SERVICEBUS_CONNECTION_STRING` straight to the SDK — it carries
`UseDevelopmentEmulator=true`, which selects plain AMQP. Two current
limits, both on the Python side:

- **Python apps need `azure-servicebus` 7.15.0 or later.** Earlier versions
  can't decode AMQP frames whose trailing optional fields are omitted
  (legal under AMQP 1.0, and what the emulator's Artemis broker sends)
  and fail with `IndexError`. Until 7.15.0 is released, pin the
  pre-release `7.15.0b2`, as dragonfly does. .NET and Java clients are
  unaffected.
- **Receiving from a topic subscription fails from Python** against
  floci-az 0.13.0 (`MessagingEntityNotFoundError`). The Python SDK
  addresses a subscription by full URI and floci-az doesn't reduce that
  form yet. Sending to topics and everything on queues works; so does
  subscription receive from .NET and Java.
