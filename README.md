# mayfly

Short lived ephemeral environment infrastructure.

One YAML spec declares an environment — S3 buckets, RDS databases, ElastiCache,
MSK/Kafka, DynamoDB, ALBs, and app deployments. `mayfly up` materializes it in
an isolated Kubernetes namespace, `mayfly down` (or the TTL reaper) destroys
it. No Docker socket, no privileged pods, no real AWS.

**Full documentation: https://docs.mayfly.sh** (source in `docs/`,
built with `docs/build.sh`).

## How it works

- Each environment is one **namespace**, named deterministically from the
  spec's `seed` (`merry-blonde-stoat`, or `env-merry-blonde-stoat` with
  `namespacePrefix: env`); namespace deletion is complete teardown. Two
  specs with different seeds coexist; re-running `up` on the same spec is
  idempotent, and `up` refuses a namespace whose recorded seed differs
  (name collisions error instead of cross-contaminating).
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
uv sync            # dev setup (or: pip install -e . for just the CLI)
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

The seed is the environment's identity: same seed → same environment
(idempotent update/heal), new seed → a fresh environment alongside it.
`--seed` overrides the spec without editing it — `mayfly up --seed pr-1234`
in CI gives each PR its own environment from one shared spec file, and
`mayfly up --seed scratch-$(whoami)` gives you a personal sandbox.

## Spec

```yaml
apiVersion: mayfly/v1alpha1
seed: pr-1234          # deterministic env name derives from this
# namespacePrefix: env # namespace becomes env-<name>; omit for bare <name>
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
      engine: redis    # redis | valkey | memcached (valkey needs the patched emulator image)
      version: "7.2"   # engine version -> container image tag
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

For anything without a dedicated field, each app takes a `patch:` — arbitrary
YAML deep-merged onto the generated Deployment as the final step (maps merge
recursively; lists of named objects like `containers`/`volumes`/`env` merge
by name; other lists replace). mayfly re-asserts its invariants afterward
(selector, `app` label, `enableServiceLinks: false`), so a patch can add
sidecars, volumes, tolerations, or securityContext but can't silently break
the wiring the environment depends on.

Each app becomes a Deployment + Service reachable at `<name>:8080`
in-namespace. App pods get `AWS_ENDPOINT_URL` plus whatever the listed
secrets carry (`DATABASE_URL`, `REDIS_URL`, `KAFKA_BROKERS`, ...), and apps
deploy only after all services are provisioned.

## Internal ALBs

`services.alb` gives an environment an **emulated ALB with a working data
plane** — created through the real `elbv2` API (target groups, listeners,
`describe-load-balancers`), with live traffic routed to the target app:

```yaml
services:
  alb:
    - name: hello-alb
      targetApp: hello   # must be one of apps:
```

Requests to `http://aws:4566/_alb/hello-alb/` (or Host header
`hello-alb.alb.localhost`) proxy through the ALB to the app with ALB-style
`X-Forwarded-*` and `X-Amzn-Trace-Id` headers; the `alb-<name>` secret
carries `ALB_URL`/`ALB_DNS_NAME`. This requires mayfly's patched emulator
image (`ghcr.io/jasondcamp/mayfly-ministack`, source in `emulator/`) — upstream
MiniStack's ALB data plane forwards to Lambda targets only; the one-file
patch (`emulator/patches/alb.py`) adds HTTP proxying for `instance`/`ip`
targets and is a candidate for an upstream PR. Path-pattern/host-header
listener rules, redirects, and fixed-responses all come from upstream and
work against the same data plane. For real AWS ALBs later, apps take
`ingress: {className: alb, annotations: {...}}` (see `examples/env-alb.yaml`).

## dragonfly — the connectivity verifier

`dragonfly/` is a small companion app that proves an environment's wiring
end-to-end with **zero configuration**: it discovers services the way a
real AWS application would — `describe-db-instances`,
`describe-cache-clusters`, `list-clusters`/`get-bootstrap-brokers` against
the emulator's control plane (using the `AWS_ENDPOINT_URL` mayfly injects
into every app pod) — then round-trips data through every instance found:
postgres insert+select, redis SET+GET, kafka produce+consume. Declare a
service in the spec and a tile appears; no secrets to mount, no lists to
maintain. It serves a web interface at `/` (one live status tile per
instance, auto-refresh every 5s), JSON at `/api`, and `/healthz` for its
readiness probe — so the dragonfly pod only goes **Ready** once every
discovered service actually works, making `mayfly up`'s success itself a
connectivity test. It's also a standing fidelity test of the emulator's
discovery APIs: if a `describe-*` call returns an endpoint that doesn't
work, dragonfly is the first to know.

Published as `ghcr.io/jasondcamp/mayfly-dragonfly` (multi-arch; siblings:
`mayfly-hello`, `mayfly-ministack` — `scripts/publish-images.sh` builds and
pushes all three). Clusters pull them directly; for local iteration the e2e
script builds the working tree under the same names and imports them.

```bash
mayfly up examples/env.yaml
kubectl -n <namespace> port-forward svc/dragonfly 8080:8080  # then open http://localhost:8080
```

The e2e harness builds and imports it automatically; the example spec wires
it to all three services.

Secrets written per service: `s3-buckets` (BUCKETS, S3_ENDPOINT),
`rds-<name>` (DATABASE_URL, DB_*), `elasticache-<name>` (REDIS_URL, REDIS_*),
`msk-<name>` (KAFKA_BROKERS), `dynamodb-<name>` (TABLE_NAME, HASH_KEY,
DYNAMODB_ENDPOINT).

**Invariant:** every service section the spec supports is (a) provisioned
with a Secret contract and (b) verified by dragonfly — adding a service
kind to mayfly means adding its dragonfly check in the same change.

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
