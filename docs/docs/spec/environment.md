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

## TTL and the reaper

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
```

The emulator runs inside the namespace behind a Service named `aws` on port
4566. Every app pod gets `AWS_ENDPOINT_URL=http://aws:4566` with
`test`/`test` credentials, so unmodified AWS SDK code works.

Defaults are **digest-pinned** upstream images. mayfly's patched image
(source in the repo's `emulator/` directory) is required for
`services.alb` and ElastiCache engine `valkey` — `up` errors up-front,
before touching the cluster, if the spec needs it while on the stock
default.
