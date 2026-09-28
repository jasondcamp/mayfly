---
sidebar_position: 2
---

# Services

Everything under `services:` is a backing service your apps consume — a
bucket, a database, a broker, a vault. Service classes are grouped by
cloud, each cloud has its own emulator, and one environment can mix clouds
freely:

| Cloud | Service classes | Emulator | Reference |
|---|---|---|---|
| AWS | `s3`, `rds`, `elasticache`, `msk`, `dynamodb`, `alb`, `secretsmanager` | MiniStack or floci, at `aws:4566` | [AWS services](aws.md) |
| Azure | `keyvault`, `servicebus`, `postgresflexible`, `azuresql` | floci-az, at `azure:4577` | [Azure services](azure.md) |

## Emulators

Each emulator runs inside the environment's namespace, digest-pinned, and
is declared under [`emulators:`](../environment.md#emulators), keyed by
cloud. The declaration is explicit and strict:

- Only the emulators you declare deploy. A declared emulator always
  deploys, whether or not anything uses it.
- **Every service class requires its cloud's emulator**, whichever backend
  its entries run on. A spec with a `postgresflexible` database needs
  `emulators.azure` even though the database itself is a native pod, and
  an `rds` entry with `backend: native` still needs `emulators.aws`.
- A missing emulator is a validation error before anything touches the
  cluster:

  ```
  services keyvault need the azure emulator: declare emulators.azure (kind: floci-az)
  ```

Apps get each deployed emulator's endpoint and credentials in their
environment (`AWS_*`, `AZURE_*`), and only for the emulators that are
declared. An app that calls AWS APIs with no spec class (creating SQS
queues at runtime, say) still needs `emulators.aws` for its endpoint.

## Backends

Each service class supports `backend: auto | emulator | native` per entry:

- **emulator** — provisioned through the cloud's real API against the
  in-namespace emulator (`create-db-instance`, `create-cache-cluster`,
  Key Vault `set_secret`, ...), so runtime control-plane calls answer
  truthfully.
- **native** — a plain pod + Service deployed directly by mayfly.
- **auto** (default) — emulator where the chosen emulator supports the
  class honestly, else native. What that means varies per class; each
  cloud's page says which way `auto` resolves.

The Secret contract is identical whichever backend a class lands on.

## Secrets: the app contract

Every service entry produces a Kubernetes Secret, named for its class and
entry (`rds-appdb`, `keyvault-app-secrets`, `pgflex-appdb`). **The Secret's
keys are the contract**: apps consume them via `envFrom`, mayfly never
renames or removes them, and classes that play the same role share key
names — `rds` postgres and `postgresflexible` both yield `DATABASE_URL` and
`DB_*`, so an app moves between clouds with zero env changes. The per-class
tables are on each cloud's page: [AWS](aws.md#secrets-the-app-contract),
[Azure](azure.md#secrets-the-app-contract).

Endpoints in Secrets are always cluster-internal
`servicename:standard-port` (`rds-appdb:5432`, `azure:4577`,
`pgflex-appdb:5432`) — never localhost, uniform across backends. Helm
releases can reference any of these keys in their values; see
[values from provisioned services](../helm.md#values-from-provisioned-services).
