---
sidebar_position: 2
---

# Services

Each service class supports `backend: auto | emulator | native` per entry:

- **emulator** — provisioned through the real AWS API against the
  in-namespace emulator (`create-db-instance`, `create-cache-cluster`, ...),
  so runtime `describe-*` calls answer truthfully.
- **native** — a plain pod + Service deployed directly by mayfly.
- **auto** (default) — emulator where the chosen emulator supports the
  class honestly, else native. The Secret contract is identical either way.

```yaml
services:
  s3:
    buckets: [assets, uploads]

  rds:
    - name: appdb
      engine: postgres                  # postgres | mysql | mariadb
      dbName: app

  elasticache:
    - name: cache-a
      engine: redis                     # redis | valkey | memcached
      version: "7.2"                    # engine version -> image tag

  msk:
    - name: events
      topics: [orders]

  dynamodb:
    - name: sessions
      hashKey: id                       # default "id"

  alb:
    - name: hello-alb
      targetApp: hello                  # must be an apps: key
```

## Notes per class

- **s3** — in-process in the emulator; instant.
- **rds** — on the default emulator this is the real RDS API: the instance
  is an actual postgres container spawned via kubedock, and
  `describe-db-instances` returns a working endpoint.
- **elasticache** — `version` selects the container image tag (the default
  emulator maps redis versions to the major tag: `7.2` → `redis:7-alpine`).
  Engine `valkey` requires mayfly's patched emulator image; `memcached`
  listens on 11211 with its own secret keys.
- **msk** — hybrid: mayfly deploys a real Redpanda broker natively, then
  registers the cluster through the MSK control-plane API, so
  `ListClusters` / `DescribeCluster` / `GetBootstrapBrokers` all answer
  correctly. `topics` are created at provision time.
- **dynamodb** — in-process; emulator-only (no native backend exists).
- **alb** — an emulated ALB with a **working data plane**; see the
  [Internal ALBs guide](../guides/internal-albs).

## Secrets: the app contract

| Service | Secret | Keys |
|---|---|---|
| s3 | `s3-buckets` | `BUCKETS`, `S3_ENDPOINT` |
| rds | `rds-<name>` | `DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` |
| elasticache (redis/valkey) | `elasticache-<name>` | `CACHE_ENGINE`, `REDIS_URL`, `REDIS_HOST`, `REDIS_PORT` |
| elasticache (memcached) | `elasticache-<name>` | `CACHE_ENGINE`, `MEMCACHED_HOST`, `MEMCACHED_PORT` |
| msk | `msk-<name>` | `KAFKA_BROKERS` |
| dynamodb | `dynamodb-<name>` | `TABLE_NAME`, `HASH_KEY`, `DYNAMODB_ENDPOINT` |
| alb | `alb-<name>` | `ALB_URL`, `ALB_DNS_NAME`, `ALB_HOST`, `ALB_TARGET_APP`, `ALB_PUBLIC_URL`* |

\* present when the cluster has Traefik (k3s/k3d) for browser access.

Endpoints in secrets are always cluster-internal
`servicename:standard-port` (`rds-appdb:5432`, `elasticache-cache-a:6379`,
`msk-events:9092`), uniform across backends. For emulator-backed services
mayfly creates that Service selecting the spawned pod directly, so data
traffic goes pod-to-pod and survives emulator restarts — while the AWS
API's own published-port answers (`aws:15432`) stay valid too.
