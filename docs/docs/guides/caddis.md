---
sidebar_position: 3
---

# caddis — the sample app

`caddis` is mayfly's worked example: a little file collector (caddisfly
larvae build cases from what they gather) showing every service doing its
real job in one small, readable app. Open
`http://caddis.<namespace>.localtest.me`, drop a file on it, and watch:

1. **Upload** — the nginx frontend proxies `/api` (same-origin, no CORS) to
   the Flask API, which streams the file to **S3**, inserts a row in
   **postgres** (connecting *through pgbouncer*), bumps **redis** counters,
   and publishes `file.uploaded` to **Kafka**.
2. **Process** — `caddis-worker`, a plain Kafka consumer-group loop (same
   image, `command: ["python", "worker.py"]`), consumes the event, reads
   the object back from S3, computes its sha256, flips the row to
   `processed`, and pushes to the redis **activity feed**. The UI's status
   flips ⏳ → ✓ on its own — async processing made visible.
3. **Download** — links are HMAC-signed with a key the API fetched at boot
   from **Secrets Manager** (`GetSecretValue` on the environment's
   `generate: true` secret); a bad token gets a 403.

The stats tiles (files / bytes / processed) and the pipeline activity feed
are redis; the feed doubles as a live view of Kafka consumption.

**Click any file row** for its per-step timeline: received → s3 put → db
insert → kafka publish → *worker pickup lag* → checksum → pipeline total.
The API times its synchronous steps, the worker adds pickup lag (computed
from the event's `published_at`) and processing time, all persisted on the
row — the async handoff between web and worker becomes a number you can
point at.

## The pieces

| App | Image | Role |
|---|---|---|
| `caddis` | `mayfly-caddis-frontend` | nginx: static UI + `/api` reverse proxy |
| `caddis-api` | `mayfly-caddis` | Flask API (gunicorn) |
| `caddis-worker` | `mayfly-caddis` | Kafka consumer (command override) |

Notes worth stealing for your own apps:

- **Web/worker split, one image** — the 12-factor pattern via mayfly's
  `command:` override. Scale the worker with `replicas:` and Kafka's
  consumer group splits partitions across them.
- **The worker has a heartbeat** — a tiny `/healthz` that goes 503 if the
  poll loop stalls, so a wedged consumer is a red tile on dragonfly's APPS
  card instead of a silent failure.
- **Per-request DB connections are fine** — because pgbouncer is doing the
  pooling; that's the point of having it in the environment.
- **No Celery** — Celery has no Kafka broker; the idiomatic Kafka worker is
  a ~30-line consumer-group loop, which is what `worker.py` is.
- **Config comes entirely from the environment**: the elasticache and msk
  secrets via `secrets:`, `DATABASE_URL` pointed at the pool, the signing
  key by name from Secrets Manager.

## In the spec

```yaml
services:
  msk:
    - name: events
      topics: [caddis.files]
  secretsmanager:
    - name: app/signing-key
      generate: true

apps:
  caddis:
    image: ghcr.io/jasondcamp/mayfly-caddis-frontend:<v>
    port: 8080
    readiness: {path: /healthz}
    ingress: {}
  caddis-api:
    image: ghcr.io/jasondcamp/mayfly-caddis:<v>
    port: 8080
    secrets: [elasticache-cache-a, msk-events]
    env:
      DATABASE_URL: postgresql://app:apppass@pgbouncer:5432/app
      S3_BUCKET: uploads
      SIGNING_SECRET_NAME: app/signing-key
    readiness: {path: /healthz}
  caddis-worker:
    image: ghcr.io/jasondcamp/mayfly-caddis:<v>
    command: ["python", "worker.py"]
    port: 8080
    secrets: [elasticache-cache-a, msk-events]
    env: {DATABASE_URL: postgresql://app:apppass@pgbouncer:5432/app, S3_BUCKET: uploads}
    readiness: {path: /healthz}
```

The smoke test uploads a file and asserts the row reaches `processed` —
one HTTP call proving S3, postgres, Kafka, redis, and Secrets Manager
end-to-end, which is the strongest single assertion in the suite.
