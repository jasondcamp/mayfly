# mayfly

Short lived ephemeral environment infrastructure.

One YAML spec declares an environment — S3 buckets, RDS databases, ElastiCache,
MSK/Kafka, and app deployments. `mayfly up` materializes it in an isolated
Kubernetes namespace, `mayfly down` (or the TTL reaper) destroys it. No Docker
socket, no privileged pods, no real AWS.

## How it works

- Each environment is one **namespace**, named deterministically from the
  spec's `seed` (`env-merry-blonde-stoat-f1ee`); namespace deletion is
  complete teardown. Two specs with different seeds coexist; re-running
  `up` on the same spec is idempotent.
- A **swappable AWS emulator** (`emulator.kind`: `ministack` default, or
  `floci`) runs inside the namespace behind a single Service — apps and
  provisioners always use `http://aws:4566`. Emulator images are
  **digest-pinned** by mayfly; the spec can override image/version
  (`latest` is rejected).
- Service provisioning picks a **backend** per service (`auto | emulator |
  native`). With ministack, RDS goes through the **real AWS API**:
  `create-db-instance` spawns an actual postgres container via kubedock,
  `describe-db-instances` returns a working in-cluster endpoint
  (`aws:15432`). MSK is **hybrid**: mayfly deploys a real Redpanda broker
  natively, then registers the cluster through the MSK control-plane API —
  `ListClusters`/`DescribeCluster` answer correctly and
  `GetBootstrapBrokers` (via `MINISTACK_MSK_BOOTSTRAP`) returns that
  broker. Services the chosen emulator can't back (ElastiCache endpoints)
  are provisioned **natively** with the identical Secret contract.
- Every service's endpoints land in a per-service Kubernetes **Secret** —
  the only contract apps consume. App pods also get `AWS_ENDPOINT_URL`
  pointing at the emulator with `test`/`test` credentials.

## Install

```bash
uv venv && uv pip install -e '.[dev]'   # or: pip install -e '.[dev]'
```

Requires `kubectl` on PATH (used for port-forwarding) and a Kubernetes
cluster — k3d/k3s is plenty.

## Usage

```bash
mayfly up env.yaml                 # create/update the environment
mayfly status env.yaml             # pods + provisioned secrets
mayfly list                        # all mayfly environments, age + TTL
mayfly render env.yaml             # print resolved plan, touch nothing
mayfly extend env.yaml --ttl 4h    # push expiry out
mayfly down env.yaml               # teardown (namespace delete)
mayfly reap [--dry-run]            # delete every expired environment
```

All cluster-touching commands take `--context` / `--kubeconfig`. `down` and
`reap` refuse namespaces not labeled `mayfly.dev/managed=true`.

## Spec

```yaml
apiVersion: mayfly/v1alpha1
seed: pr-1234          # deterministic env name derives from this
ttl: 8h                # reaped after this

emulator:
  kind: ministack      # ministack | floci; omit for pinned default
  # image: ministackorg/ministack   # override to self-host/pin your own
  # version: "1.4.3"                # tag; 'latest' is rejected

services:
  s3:
    buckets: [assets, uploads]
  rds:
    - name: appdb
      engine: postgres # postgres | mysql | mariadb
      dbName: app
      # backend: auto  # auto | emulator | native
  elasticache:
    - name: cache-a
  msk:
    - name: events
      topics: [orders]

apps:
  echo:
    image: ealen/echo-server:latest
    port: 80
    secrets: [rds-appdb, elasticache-cache-a]  # env-from these secrets
```

Secrets written per service: `s3-buckets` (BUCKETS, S3_ENDPOINT),
`rds-<name>` (DATABASE_URL, DB_*), `elasticache-<name>` (REDIS_URL, REDIS_*),
`msk-<name>` (KAFKA_BROKERS).

## Development & testing

Three tiers, cheapest first:

```bash
make test    # unit tests (spec, naming, manifests, backend resolution) — no cluster
make lint    # ruff
make e2e     # full loop on a disposable k3d cluster: create cluster ->
             # up -> smoke test -> up again (idempotency) -> down -> delete cluster
```

`make e2e` never touches your default kubeconfig. CI runs unit + e2e on every
PR (`.github/workflows/ci.yml`).

For iterating against a long-lived local cluster instead of paying cluster
create/pull each run:

```bash
k3d cluster create mayfly-dev --kubeconfig-update-default=false
export KC=$(k3d kubeconfig write mayfly-dev)
mayfly up examples/env.yaml --kubeconfig "$KC"
./examples/smoke-test.sh <namespace> "$KC"   # namespace from mayfly render/up output
```

A real (multi-node) k3s box is worth a second-tier pass before anything
serious — scheduling, image-pull latency, and storage behave differently
than single-node k3d — pointed at a dedicated kubeconfig/context.

## Design notes / gotchas discovered

- **kubedock's Docker volumes gap decided floci's backends.** floci's
  container-backed services (RDS etc.) require the Docker volumes API, which
  kubedock doesn't implement (501) — so with `emulator.kind: floci` every
  container service uses the native backend and floci serves only in-process
  APIs. MiniStack's Docker calls avoid the volumes API, which is why its RDS
  works through kubedock.
- **MiniStack + kubedock must share a pod.** MiniStack's container readiness
  checks and port bindings assume the Docker daemon is on its own localhost;
  colocating kubedock in the same pod makes that literally true. Combined
  with `DOCKER_NETWORK`, `MINISTACK_RDS_PUBLIC_ENDPOINT=1` and
  `MINISTACK_HOST=aws`, `describe-db-instances` advertises `aws:<port>` —
  an endpoint that actually works from any pod in the namespace.
- MiniStack's ElastiCache advertises the Docker container name
  (`redis:6379`), which doesn't resolve in-cluster and has no public-endpoint
  knob — hence the native backend for caches until that changes upstream.
- Every pod sets `enableServiceLinks: false`: the `aws` Service otherwise
  injects `AWS_PORT=tcp://...`-style env vars, and Quarkus-based emulators
  (floci) fatally misparse the analogous `FLOCI_PORT` as an int property.
- Emulators return `200` + an empty list for describe-calls on nonexistent
  resources where real AWS raises (e.g. `DBInstanceNotFound`); existence
  checks must test emptiness, not exceptions.
- Emulator state is in-memory: an emulator pod restart forgets AWS state
  while the service pods live on (kubedock reaps its orphans after 1h).
  `mayfly status` therefore reads cluster state (Secrets), not the emulator.
