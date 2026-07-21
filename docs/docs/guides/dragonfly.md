---
sidebar_position: 2
---

# dragonfly — the connectivity verifier

`dragonfly` is mayfly's bundled companion app: it proves an environment's
wiring end-to-end with **zero configuration**. It discovers services the way
a real AWS application would — `describe-db-instances`,
`describe-cache-clusters`, `list-clusters` + `get-bootstrap-brokers`,
`list-tables`, `list-buckets`, `describe-load-balancers` against the
in-namespace control plane — then round-trips real data through every
instance found:

| Kind | Check |
|---|---|
| rds | insert + select a row (postgres wire protocol) |
| elasticache | SET/GET (redis/valkey) or set/get (memcached — engine-aware) |
| msk | produce + consume a message, verified byte-for-byte |
| dynamodb | put-item + get-item + delete (hash key discovered from the table) |
| s3 | put + get + delete an object per bucket |
| alb | an HTTP request through the data plane |
| secretsmanager | get-secret-value per secret, verified non-empty |
| apps | each app's own `readiness` check from the spec (HTTP or TCP), through its Service |

Declare a service in the spec and a tile appears — no secrets to mount, no
lists to maintain. Apps are covered the same way with zero extra config:
mayfly derives each app's check from the `readiness` block it already has
(`hello` → `GET /healthz`, `pgbouncer` → TCP `:5432`) and hands the list to
dragonfly via the injected `MAYFLY_APP_CHECKS` env var — an APPS card shows
every app's live health (dragonfly skips checking itself). Apps without a
`readiness` have no defined check, so no tile. Adding a service kind to mayfly means adding its
dragonfly check in the same change; that invariant is project policy.

## Interfaces

- **`/`** — web UI: one card per service kind, one row per instance
  (`dot — name — check result — latency`), auto-refreshing every 5s. The
  dot is green/yellow/red (connected / slow ≥1s / failed) always paired
  with words; failures print the error inline.
- **`/api`** — the same report as JSON, including each instance's AWS
  `status` field.
- **`/healthz`** — strict current-state truth: 200 only when every
  discovered instance and app verifies right now.
- **`/readyz`** — the readiness latch: strict until the environment first
  goes fully healthy (so `mayfly up` still gates on proven connectivity),
  then stays ready for the pod's lifetime — a later failure shows red tiles
  on a *reachable* dashboard instead of taking the dashboard down with the
  thing it's reporting on. Point the spec's `readiness` at `/readyz`.

## In the spec

```yaml
apps:
  dragonfly:
    image: ghcr.io/jasondcamp/mayfly-dragonfly:0.1.3
    port: 8080
    readiness: {path: /healthz, initialDelaySeconds: 3, periodSeconds: 10, timeoutSeconds: 30}
    ingress: {}    # http://dragonfly.<namespace>.localtest.me
```

It also doubles as a standing fidelity test of the emulator itself: if a
`describe-*` call ever advertises an endpoint that doesn't actually work,
dragonfly's tiles are the first place it shows.
