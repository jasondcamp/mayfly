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
  while the pods are gone. mayfly sets `--reapmax=8760h`; namespace
  deletion is the real cleanup.
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

## The emulator patches

The default emulator image (`ghcr.io/jasondcamp/mayfly-ministack`) is
upstream MiniStack, digest-pinned, plus two single-file overlays (source in
`emulator/patches/`):

1. **ALB HTTP data plane** — upstream forwards ALB traffic to Lambda
   targets only; the patch proxies `instance`/`ip` targets over HTTP with
   ALB-realistic headers. Submitted upstream.
2. **valkey engine for ElastiCache** — `valkey/valkey:{major.minor}-alpine`
   images, engine plumbing through single-node and cluster-mode spawns.

When these merge upstream, the overlays and the `emulator/` directory
delete cleanly — mayfly reverts to the stock pinned image.
