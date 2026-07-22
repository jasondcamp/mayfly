---
sidebar_position: 1
---

# Environment

One YAML file describes an environment. `mayfly up` validates it strictly
(unknown keys are rejected), derives the environment's identity from
`seed`, and converges the cluster on it.

```yaml
apiVersion: mayfly/v1alpha1   # required literal
seed: pr-1234                 # environment identity
namespacePrefix: env          # optional; namespace = <prefix>-<name>, else <name>
ttl: 8h                       # 30m / 8h / 2d — reaped after this

emulator: {...}
services: {...}
apps: {...}
```

## Identity and naming

The seed hashes to a deterministic `adjective-adjective-animal` name
(`pr-1234` → `jolly-bold-tapir`), which names the namespace. Rules:

- Same seed → same environment: `up` is an idempotent update/heal.
- New seed → a new environment alongside the old one.
- `--seed` on the CLI overrides the file without editing it.
- `up` refuses a namespace whose recorded seed label differs — a rare
  word-collision between two seeds becomes a loud error, never
  cross-contamination.
- `up` also refuses a pre-existing namespace that mayfly didn't create
  (no `mayfly.dev/managed` label). mayfly only ever operates on its own
  labeled namespaces, so it coexists with anything else running in the
  cluster — adopting a foreign namespace would make it deletable by
  `down`/`reap`.

## TTL and the reaper

For unattended cleanup, install the in-cluster reaper CronJob —
see [getting started](../getting-started#unattended-cleanup-the-in-cluster-reaper).

Every environment carries a `mayfly.dev/expires-at` annotation
(`created + ttl`). `mayfly reap` deletes expired environments (namespaces
terminate in the background; they show `TERMINATING` in `mayfly list` until
gone). `mayfly extend --ttl 4h` pushes expiry out from now.

## emulator

```yaml
emulator:
  kind: ministack             # ministack | floci (default: ministack)
  image: ghcr.io/jasondcamp/mayfly-ministack   # optional override
  version: "0.1.3"            # image tag; 'latest' is rejected
  expose: false               # opt-in: AWS API at aws.<namespace>.localtest.me
```

### Laptop access to the AWS API

With `expose: true`, the emulator's API is served through the cluster
ingress at `aws.<namespace>.localtest.me` — the AWS CLI and SDKs on your
machine work with no port-forward. A profile makes it painless:

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

The emulator runs inside the namespace behind a Service named `aws` on port
4566. Every app pod gets `AWS_ENDPOINT_URL=http://aws:4566` with
`test`/`test` credentials, so unmodified AWS SDK code works.

Defaults are **digest-pinned** upstream images. mayfly's patched image
(source in the repo's `emulator/` directory) is required for
`services.alb` and ElastiCache engine `valkey` — `up` errors up-front,
before touching the cluster, if the spec needs it while on the stock
default.
