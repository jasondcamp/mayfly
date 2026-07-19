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
  (`aws:15432`). ElastiCache works the same way (`aws:16379`). MSK is
  **hybrid**: mayfly deploys a real Redpanda broker natively, then registers
  the cluster through the MSK control-plane API — `ListClusters`/
  `DescribeCluster` answer correctly and `GetBootstrapBrokers` (via
  `MINISTACK_MSK_BOOTSTRAP`) returns that broker. Anything the chosen
  emulator can't back falls to the **native** backend (all container
  services on floci) with the identical Secret contract.
- Every service's endpoints land in a per-service Kubernetes **Secret** —
  the only contract apps consume. Endpoints are uniform across backends:
  always `servicename:standard-port` (`rds-appdb:5432`,
  `elasticache-cache-a:6379`, `msk-events:9092`). For emulator-backed
  services mayfly creates that Service selecting the kubedock-spawned pod
  directly (label selectors `dbid`/`clusterid`), so data traffic goes
  pod-to-pod and survives emulator restarts, while the AWS API's own
  `aws:<published-port>` answer stays valid through the reverse-proxy. App
  pods also get `AWS_ENDPOINT_URL` pointing at the emulator with
  `test`/`test` credentials.

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
  myapi:
    image: ghcr.io/you/myapi:sha-abc123
    port: 3000
    command: ["/bin/server"]     # optional entrypoint override
    args: ["--verbose"]
    replicas: 2
    env: {LOG_LEVEL: debug}
    secrets: [rds-appdb, elasticache-cache-a]  # env-from these secrets
    resources: {cpu: 100m, memory: 128Mi, memoryLimit: 512Mi}
    readiness: {path: /healthz}  # httpGet probe; omit for none
    imagePullSecret: regcred     # copied into the namespace at `up` from
                                 # --pull-secret-namespace (default "default")
```

Each app becomes a Deployment + Service reachable at `<name>:8080`
in-namespace. App pods get `AWS_ENDPOINT_URL` plus whatever the listed
secrets carry (`DATABASE_URL`, `REDIS_URL`, `KAFKA_BROKERS`, ...), and apps
deploy only after all services are provisioned.

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
  with `MINISTACK_RDS_PUBLIC_ENDPOINT=1` and `MINISTACK_HOST=aws`,
  `describe-db-instances` advertises `aws:<port>` — an endpoint that
  actually works from any pod in the namespace. Note the RDS public-endpoint
  flag is load-bearing twice over: besides host-published ports and
  localhost readiness, it short-circuits Docker-network detection entirely
  (`_get_ministack_network()` returns None), which is why RDS never hits the
  `network kubedock not found` failure that sinks ElastiCache.
- **`DOCKER_NETWORK` must stay unset for MiniStack under kubedock.** Setting
  it forces ElastiCache down the network-attach path, which kubedock rejects
  (`network kubedock not found`) → MiniStack silently falls back to
  advertising its compose-sidecar default `redis:6379` (and even with the
  network pre-created via kubedock's `/networks/create`, the network path
  advertises kubedock's fake container IP `127.0.0.1`). With the variable
  unset, ElastiCache takes the published-port branch and advertises
  `MINISTACK_HOST:16379+` — which resolves and works in-cluster. RDS is
  indifferent either way: its `PUBLIC_ENDPOINT` mode short-circuits network
  detection entirely.
- Every pod sets `enableServiceLinks: false`: the `aws` Service otherwise
  injects `AWS_PORT=tcp://...`-style env vars, and Quarkus-based emulators
  (floci) fatally misparse the analogous `FLOCI_PORT` as an int property.
- Emulators return `200` + an empty list for describe-calls on nonexistent
  resources where real AWS raises (e.g. `DBInstanceNotFound`); existence
  checks must test emptiness, not exceptions.
- Emulator state is in-memory: an emulator pod restart forgets AWS state
  while the service pods live on (kubedock reaps its orphans after 1h).
  `mayfly status` therefore reads cluster state (Secrets), not the emulator.
