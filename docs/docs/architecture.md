---
sidebar_position: 5
---

# Architecture and design notes

## The topology

With `emulators.aws` declared, the environment's namespace runs one `aws`
pod containing **two containers**: the MiniStack emulator and [kubedock](https://github.com/joyrex2001/kubedock),
a minimal Docker API that materializes "containers" as Kubernetes pods.
The colocation is load-bearing: MiniStack's container readiness checks and
port bindings assume the Docker daemon is on its own localhost, and sharing
a pod makes that literally true.

When you `create-db-instance`, MiniStack asks its "Docker daemon" (kubedock,
over localhost) to run a postgres container; kubedock spawns it as a pod;
MiniStack's published-port mode advertises `aws:15432`, which works because
kubedock's reverse-proxy listens on that port inside the shared pod and the
`aws` Service pre-exposes the port range. (mayfly sets
`MINISTACK_RDS_PUBLIC_ENDPOINT=1` and `MINISTACK_HOST=aws` to get that
answer; the public-endpoint flag also short-circuits MiniStack's
Docker-network detection, which is why RDS never hits the
`network kubedock not found` failure described below.) mayfly additionally
creates a per-service Service (`rds-appdb:5432`) selecting the spawned pod
directly by the labels MiniStack sets and kubedock copies (`dbid`,
`clusterid`) — uniform naming across backends, pod-to-pod data path,
survives emulator restarts. MSK's `GetBootstrapBrokers` returns mayfly's
native Redpanda broker via `MINISTACK_MSK_BOOTSTRAP`.

Services the emulator can't back honestly use the **native** backend: mayfly
deploys the container itself (Redpanda for MSK — registered into the MSK
control plane afterwards, so the API still answers; valkey/postgres/etc.
under floci). The Secret contract is identical either way.

The spec's [`emulators:`](spec/environment.md#emulators) block decides which
of these pods exist; nothing is inferred from the services. With
`emulators.azure` declared, the namespace runs one `azure` pod with the
same two-container shape: [floci-az](https://floci.io/az/) plus its own kubedock. floci-az is
a hybrid — Key Vault is in-process, but Service Bus is Docker-backed (an
Artemis broker), so the emulator needs a Docker daemon on its localhost,
exactly the MiniStack situation. The Azure database classes
(`postgresflexible`, `azuresql`) don't use the emulator: their faithful
implementation *is* a real engine pod, so `backend: auto` resolves to
native. They still require `emulators.azure`, because every service class
requires its cloud's emulator; that keeps what deploys exactly what the
spec says.

## Hard-won gotchas

These cost real debugging time; they're encoded in mayfly so you never hit
them, and recorded here so future changes don't regress them.

- **`enableServiceLinks: false` on every pod.** The `aws` Service otherwise
  injects `AWS_PORT=tcp://...`-style env vars into pods; Quarkus-based
  emulators fatally misparse the analogous `*_PORT` as an integer config
  property and crash-loop.
- **kubedock's missing volumes API decides floci's backends.** floci's
  container-backed services (RDS and friends) need the Docker volumes API,
  which kubedock doesn't implement (it answers 501). So with
  `emulators.aws.kind: floci` every container service resolves to the native
  backend, and floci serves only its in-process APIs. MiniStack's Docker
  calls avoid the volumes API, which is why its RDS works through kubedock.
- **`DOCKER_NETWORK` must stay unset** for MiniStack under kubedock. Set,
  it forces ElastiCache down a network-attach path kubedock rejects
  (`network kubedock not found`), causing a *silent* fallback that advertises unusable endpoints. Unset,
  services take the published-port branch and advertise working
  `aws:<port>` endpoints. (RDS is immune either way — its public-endpoint
  mode short-circuits network detection.)
- **Helm is a renderer, never a lifecycle.** `helmApps:` runs `helm
  template` client-side (exact version pin, values generated from the
  spec — `${secret:...}` placeholders resolve after provisioning) and
  applies the docs with the same server-side apply as everything else: no
  release object, the namespace TTL is the only teardown. Rendered pod
  templates get `enableServiceLinks: false` stamped like generated apps,
  and cluster-scoped objects are skipped — they'd outlive the TTL. Helm
  apps must be *applied* before the regular apps' readiness wait:
  dragonfly's `/readyz` latches only when every check — including helm
  `check:` targets — is green, so waiting on dragonfly first would
  deadlock `up`.
- **Emulator describe-calls return `200` + empty lists for nonexistent
  resources** where real AWS raises (`DBInstanceNotFound`). Existence
  checks must test emptiness; `describe || create` idioms silently skip
  creation and poll forever.
- **kubedock reaps spawned pods after 1h by default** — far shorter than
  environment TTLs, and the control plane keeps reporting `available`
  while the pods are gone. There is no flag to disable the reaper, so
  mayfly sets `--reapmax=876000h` (a century — effectively never; namespace
  deletion is the real cleanup). Environments deployed before this flag
  existed lose their service pods after an hour and **`mayfly up` cannot
  heal that state**: the control plane still claims the resources exist, so
  provisioning skips them — recycle with `mayfly down && mayfly up`.
- **kubedock needs memory headroom**: it holds every reverse-proxy listener
  and container's bookkeeping in RAM; a tight limit gets OOMKilled after a
  day, silently severing all `aws:<port>` data planes.
- **kubedock's reverse proxy leaks upstream sockets per connection.** A
  client that connects-and-closes on every request (health checkers,
  naive scripts) exhausts the backing service's connection limit through
  the proxy — memcached's default 1024 dies in hours. Long-lived clients
  through `aws:<port>` endpoints must reuse connections (dragonfly holds
  one persistent client per cache endpoint for exactly this reason);
  upstream-fix candidate in kubedock.
- **Deployment waits need full rollout semantics.** Checking
  `availableReplicas` alone returns during a rolling update while the *old*
  pod still serves — provisioning then lands in the old emulator's memory
  and vanishes seconds later. mayfly waits on observed generation +
  updated replicas.
- **Emulator state is in-memory.** An emulator container restart forgets
  AWS state while spawned pods live on. Re-running `mayfly up` heals;
  `mayfly status` reads cluster state (Secrets), never emulator memory.
- **The Key Vault SDK refuses bearer tokens over plain HTTP**, and
  `verify_challenge_resource=False` doesn't lift that. Every client —
  mayfly's provisioner, dragonfly, and your apps — builds the client with
  the `https://` vault URL plus a transport that rewrites to `http://` at
  send time, and a fake token credential; see the
  [Azure spec page](spec/services/azure#connecting-to-key-vault). The Service Bus
  management client needs the same transport, and ignores a `transport=`
  constructor argument, so mayfly subclasses it to re-inject one.
- **floci-az's Service Bus is management-only by default.** Without
  `FLOCI_AZ_SERVICES_SERVICE_BUS_MOCKED=false` it accepts entity creation
  but runs no broker, so AMQP connections are refused. mayfly sets it (plus
  start-on-boot) whenever a `servicebus` class is declared, and floci-az
  then spawns an Artemis broker through kubedock.
- **Service Bus's broker can't start under kubedock yet.** kubedock
  starts a container on the first file copied into it, where real Docker
  waits for an explicit start. floci-az creates the Artemis container,
  copies in `broker.xml` and its patched protocol jars, then starts it —
  so under default kubedock the broker would boot unpatched. kubedock's
  `--pre-archive` defers the start, but it bundles *all* of a container's
  pre-start files into a single ConfigMap, and floci-az 0.13.0's payload
  is about 1.6 MB (two patched jars of 733 KB and 859 KB) — over the
  1 MiB object limit. The `azure` pod's kubedock runs with `--pre-archive`
  so this fails loudly at provisioning (floci-az returns 500 on entity
  creation) rather than running an unpatched broker. Key Vault and the
  native database classes are unaffected. Fixing it needs either
  per-file ConfigMaps in kubedock or a pre-patched broker image in
  floci-az.
- **Python Service Bus clients need azure-servicebus 7.15.0+.** Earlier
  releases can't decode AMQP performatives whose trailing optional fields
  are omitted — legal under AMQP 1.0 §1.4, and what Artemis sends — and
  raise `IndexError`. dragonfly pins the `7.15.0b2` pre-release until
  7.15.0 ships. Separately, floci-az 0.13.0 doesn't resolve the full-URI
  subscription address the Python SDK sends, so subscription receives
  from Python fail; dragonfly's topic-only check is send-only until
  floci-az fixes it.
- **SQL Server images are amd64-only** (`azuresql` native backend) and run
  under emulation on Apple Silicon k3d — expect a slow first start; the
  readiness probe is a real `sqlcmd SELECT 1`, so waits are honest. The
  engine also refuses to start under ~2GB of memory, hence the generous
  limit on that pod.
- **Azure discovery in dragonfly is spec-driven, not control-plane-driven.**
  mayfly injects `MAYFLY_AZURE_CHECKS` (derived from the spec) because
  floci-az's ARM listing support is unverified and native database pods are
  invisible to any control plane. If floci-az's management plane proves out,
  keyvault/servicebus discovery can move to it.

## Emulator provenance

The default emulator is stock upstream MiniStack, digest-pinned. Two mayfly
features began life as single-file overlays in a patched image
(`ghcr.io/jasondcamp/mayfly-ministack`, now retired): the **ALB HTTP data
plane** for `instance`/`ip` targets and the **valkey ElastiCache engine**.
Both were upstreamed (ministack#1113, #1115) and ship in MiniStack ≥ 1.4.4,
so the stock pin covers everything. Old specs pinning the retired patched
image keep working — it stays published on GHCR — but there is no reason
to use it for new environments.

## The overlay pattern

The retired image above followed a deliberate pattern for when mayfly needs
upstream behavior that doesn't exist yet. It ran for four days, shipped two
features ahead of upstream, and deleted cleanly — reuse it next time:

To *try* a change locally before committing to this, use dev overrides
(`OVERRIDES.md` in the repository): they run a local build of kubedock,
MiniStack, floci or floci-az without publishing anything.

**When to reach for it:** a dependency (emulator, sidecar, ...) is missing
a bounded feature or has a bug, you can implement it, and waiting for
upstream would block mayfly. The overlay and an upstream PR are one
decision — never carry a patch you aren't actively upstreaming.

**Structure** — an `emulator/`-style directory in this repo:

```text
emulator/
  Dockerfile      # FROM <upstream>@<digest>  (the SAME digest mayfly pins)
                  # COPY patches/x.py <upstream module path>/x.py
  patches/x.py    # complete upstream file + your change — one file per
                  # concern, each mapping 1:1 to an upstream PR
  README.md       # per patch: what/why, upstream PR link, retirement terms
```

**Rules that made it work:**

- **Whole-file overlays, not diffs.** `COPY` over the module at build time —
  no patch tooling, and what runs is exactly what you read. Keep each file
  identical to upstream except the change, so the overlay diff *is* the
  upstream PR diff.
- **Base is the pinned digest** from the `EMULATORS` registry — the overlay
  changes exactly one thing relative to what mayfly already runs.
- **Publish like any mayfly image**: `ghcr.io/jasondcamp/mayfly-<name>`,
  versioned with mayfly (pyproject), multi-arch, built by the CI matrix and
  `scripts/publish-images.sh`.
- **Core stays agnostic**: the spec's `emulators.<cloud>.image`/`version` override
  selects the patched image; nothing else in mayfly knows it exists. Add an
  `up`-time guard that errors clearly when a spec uses the feature on the
  stock image (the ALB/valkey guard lived in `cli.py` while the divergence
  existed).
- **Write the patch to upstream's conventions from day one** — their style,
  their tests, their CHANGELOG — and submit promptly. The overlay is a
  waiting room, not a fork.
- **Retirement is part of the pattern** (this exact sequence ran for
  1.4.4): upstream merges *and releases* → bump the pin to stock, delete
  the directory, drop the CI/publish entries and the guard, update docs.
  Merged-but-unreleased doesn't count — pins point at releases (kubedock's
  `--reapmax` fix waited in exactly that state).
