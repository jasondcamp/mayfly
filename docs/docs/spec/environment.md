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

emulators: {...}              # which cloud emulators deploy (explicit)
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

## ingressDomain

```yaml
ingressDomain: envs.example.com   # default: localtest.me
```

The domain generated ingress hosts live under: apps with `ingress: {}` get
`<app>.<namespace>.<ingressDomain>`, exposed ALBs get
`<alb>.<namespace>.<ingressDomain>`, and [`emulators.aws.expose`](services/aws.md#laptop-access-to-the-aws-api)
publishes the AWS API at `aws.<namespace>.<ingressDomain>`.

The default, `localtest.me`, resolves to `127.0.0.1` — perfect for laptop
clusters. On a real cluster, point a wildcard DNS record
(`*.envs.example.com`, or `*.<namespace>.envs.example.com` per env) at
your ingress controller and set `ingressDomain` — every environment then
gets working URLs like `backend.pr-1234.envs.example.com` with no
per-environment DNS work. An app's explicit `ingress.host` always wins
over the generated name.

## TTL and the reaper

For unattended cleanup, install the in-cluster reaper CronJob —
see [getting started](../getting-started#unattended-cleanup-the-in-cluster-reaper).

Every environment carries a `mayfly.dev/expires-at` annotation
(`created + ttl`). `mayfly reap` deletes expired environments (namespaces
terminate in the background; they show `TERMINATING` in `mayfly list` until
gone). `mayfly extend --ttl 4h` pushes expiry out from now.

## emulators

Names exactly which cloud emulators the environment runs, keyed by cloud.
Nothing is inferred: only the clouds listed here deploy, and a spec that
declares a service class without its cloud's emulator
[fails validation](services/index.md#emulators).

```yaml
emulators:
  aws:
    kind: ministack    # ministack | floci
  azure:
    kind: floci-az     # floci-az
```

`kind` is required. Each entry also takes `image` / `version` (overriding
the digest-pinned default; `latest` is rejected) and `expose`. An
environment with only apps and no services can omit `emulators:` entirely.
The per-cloud details live with each cloud's services:

- [The AWS emulator](services/aws.md#the-aws-emulator), including
  [laptop access to the AWS API](services/aws.md#laptop-access-to-the-aws-api).
- [The Azure emulator](services/azure.md#the-azure-emulator).
