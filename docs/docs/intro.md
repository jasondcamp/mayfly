---
sidebar_position: 1
slug: /
---

# mayfly

**Short lived ephemeral environments on Kubernetes.** One YAML spec declares
an environment — S3 buckets, RDS databases, ElastiCache, MSK/Kafka,
DynamoDB, ALBs, and your app containers. `mayfly up` materializes it in an
isolated namespace; `mayfly down` (or the TTL reaper) destroys it. No Docker
socket, no privileged pods, no real AWS.

```bash
pip install mayfly-cli
mayfly up env.yaml
```

## How it works

- **One namespace per environment**, named deterministically from the spec's
  `seed` (`merry-blonde-stoat`). Namespace deletion is complete teardown.
  Same seed converges idempotently; different seeds coexist side by side.
- **A swappable AWS emulator** runs inside the namespace behind a single
  Service — apps and provisioners always use `http://aws:4566`. Emulator
  images are digest-pinned.
- **Real AWS APIs where they're truthful**: with the default emulator,
  `create-db-instance` spawns an actual postgres container,
  `describe-db-instances` returns a working in-cluster endpoint, and an
  emulated ALB routes live traffic to your app. Anything the emulator can't
  back honestly falls to a **native** backend (a plain pod) with the
  identical contract.
- **Per-service Secrets are the only app contract**: `DATABASE_URL`,
  `REDIS_URL`, `KAFKA_BROKERS`, ... — endpoints are always
  `servicename:standard-port`, uniform across backends.
- **dragonfly**, the bundled verifier, discovers every service through the
  AWS control plane and round-trips real data through each — a live
  dashboard proving your environment actually works.

## Where to go next

- [Getting started](getting-started) — local cluster to first environment
  in a few minutes.
- [Spec reference](spec/environment) — every field.
- [Guides](guides/internal-albs) — internal ALBs, dragonfly, testing, image
  publishing.
- [Architecture](architecture) — how the emulator topology works and the
  hard-won gotchas behind it.
