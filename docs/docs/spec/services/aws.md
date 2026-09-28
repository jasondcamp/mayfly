---
sidebar_position: 1
sidebar_label: AWS
---

# AWS services

The AWS classes run against an AWS emulator in the environment's
namespace — [MiniStack](https://github.com/ministackorg/ministack) or
[floci](https://floci.io/), your choice of `kind` — behind a Service named `aws` on
port 4566. Unmodified AWS SDK code works: every app is pointed at it
automatically (see [app environment](#app-environment)).

## The AWS emulator

Every AWS class requires `emulators.aws` (see
[emulators](index.md#emulators)):

```yaml
emulators:
  aws:
    kind: ministack           # ministack | floci (required)
    # image: my-registry/ministack   # optional override (self-hosted mirror etc.)
    # version: "1.4.4"               # image tag; 'latest' is rejected
    expose: false             # opt-in: AWS API at aws.<namespace>.<ingressDomain>
```

Swapping `kind` swaps the whole AWS backend without touching the rest of
the spec: [`auto` backends](index.md#backends) re-resolve against what the
chosen emulator can back honestly, and the Secret contract stays the same.

Defaults are **digest-pinned** upstream images; the pinned MiniStack
(≥ 1.4.4) includes the ALB data plane and the valkey ElastiCache engine —
both upstreamed from mayfly. Override `image`/`version` only to pin your
own build or a self-hosted mirror.

### Laptop access to the AWS API

With `emulators.aws.expose: true`, the emulator's API is served through the cluster
ingress at `aws.<namespace>.localtest.me` (under your
[`ingressDomain`](../environment.md#ingressdomain)) — the AWS CLI and SDKs
on your machine work with no port-forward. A profile makes it painless:

```ini
# ~/.aws/config
[profile mayfly]
region = us-east-1
endpoint_url = http://aws.<namespace>.localtest.me

# ~/.aws/credentials
[mayfly]
aws_access_key_id = test
aws_secret_access_key = test
```

Then `aws --profile mayfly rds describe-db-instances`, `... s3 ls`, etc.

**Default is off, deliberately**: the emulated API is unauthenticated — it
can mutate environment state and read Secrets Manager values — so it should
never be reachable by default on a shared cluster. Without `expose`, use
`kubectl -n <namespace> port-forward svc/aws 4566:4566` and
`endpoint_url = http://localhost:4566`. Either way this is a convenience
for humans: apps under test should keep using the in-cluster
`AWS_ENDPOINT_URL` mayfly injects.

## Service classes

Each class supports [`backend: auto | emulator | native`](index.md#backends)
per entry; on AWS, `auto` picks the emulator wherever the chosen `kind`
backs the class honestly, else native — the notes below say how each class
lands.

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

  secretsmanager:
    - name: app/api-key
      value: "test-key-123"             # literal fixture
    - name: app/signing-key
      generate: true                    # random per env; kept across re-ups
```

## Notes per class

- **s3** — in-process in the emulator; instant.
- **rds** — on the default emulator this is the real RDS API: the instance
  is an actual postgres container spawned via kubedock, and
  `describe-db-instances` returns a working endpoint.
- **elasticache** — `version` selects the container image tag (the default
  emulator maps redis versions to the major tag: `7.2` → `redis:7-alpine`).
  Engines: `redis`, `valkey` (MiniStack ≥ 1.4.4), and `memcached` (listens
  on 11211 with its own secret keys).
- **msk** — hybrid: mayfly deploys a real Redpanda broker natively, then
  registers the cluster through the MSK control-plane API, so
  `ListClusters` / `DescribeCluster` / `GetBootstrapBrokers` all answer
  correctly. `topics` are created at provision time.
- **dynamodb** — in-process; emulator-only (no native backend exists).
- **alb** — an emulated ALB with a **working data plane**; see the
  [Internal ALBs guide](../../guides/internal-albs.md).
- **secretsmanager** — in-process; emulator-only. Literal `value:` entries
  converge to the spec on re-up; `generate: true` entries get a random
  value on first creation and are never rotated by re-ups. Values in a
  committed spec are test fixtures — don't put production credentials
  there; use `generate:` when the app just needs *a* secret to exist. Apps
  can fetch at runtime via the SDK (`GetSecretValue` against the injected
  `AWS_ENDPOINT_URL`) — the AWS-native pattern — or consume the mirrored
  k8s Secret.

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
| secretsmanager | `sm-<mangled-name>` | `SECRET_NAME`, `SECRET_ARN`, `SECRET_VALUE` |

\* present when the cluster has Traefik (k3s/k3d) for browser access.

Endpoints in secrets are always cluster-internal
`servicename:standard-port` (`rds-appdb:5432`, `elasticache-cache-a:6379`,
`msk-events:9092`), uniform across backends. For emulator-backed services
mayfly creates that Service selecting the spawned pod directly, so data
traffic goes pod-to-pod and survives emulator restarts — while the AWS
API's own published-port answers (`aws:15432`) stay valid too.

## App environment

When the spec declares `emulators.aws`, every app and init job gets the AWS
bundle, so unmodified SDK code talks to the emulator:

| Env var | Value |
|---|---|
| `AWS_ENDPOINT_URL` | `http://aws:4566` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `test` / `test` |
| `AWS_DEFAULT_REGION` | `us-east-1` |

Spec `env:` entries override these, like everything mayfly injects.
