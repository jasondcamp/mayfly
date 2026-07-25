---
sidebar_position: 5
---

# Architecture and design notes

## The topology

Each environment's namespace runs one `aws` pod containing **two
containers**: the MiniStack emulator and [kubedock](https://github.com/joyrex2001/kubedock),
a minimal Docker API that materializes "containers" as Kubernetes pods.
The colocation is load-bearing: MiniStack's container readiness checks and
port bindings assume the Docker daemon is on its own localhost, and sharing
a pod makes that literally true.

When you `create-db-instance`, MiniStack asks its "Docker daemon" (kubedock,
over localhost) to run a postgres container; kubedock spawns it as a pod;
MiniStack's published-port mode advertises `aws:15432`, which works because
kubedock's reverse-proxy listens on that port inside the shared pod and the
`aws` Service pre-exposes the port range. mayfly additionally creates a
per-service Service (`rds-appdb:5432`) selecting the spawned pod directly —
uniform naming across backends, pod-to-pod data path, survives emulator
restarts.

Services the emulator can't back honestly use the **native** backend: mayfly
deploys the container itself (Redpanda for MSK — registered into the MSK
control plane afterwards, so the API still answers; valkey/postgres/etc.
under floci). The Secret contract is identical either way.

## Hard-won gotchas

These cost real debugging time; they're encoded in mayfly so you never hit
them, and recorded here so future changes don't regress them.

- **`enableServiceLinks: false` on every pod.** The `aws` Service otherwise
  injects `AWS_PORT=tcp://...`-style env vars into pods; Quarkus-based
  emulators fatally misparse the analogous `*_PORT` as an integer config
  property and crash-loop.
- **`DOCKER_NETWORK` must stay unset** for MiniStack under kubedock. Set,
  it forces ElastiCache down a network-attach path kubedock rejects,
  causing a *silent* fallback that advertises unusable endpoints. Unset,
  services take the published-port branch and advertise working
  `aws:<port>` endpoints. (RDS is immune either way — its public-endpoint
  mode short-circuits network detection.)
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
- **Core stays agnostic**: the spec's `emulator.image`/`version` override
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
