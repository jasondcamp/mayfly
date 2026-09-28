# TODO

## Azure Service Bus on Kubernetes

Service Bus provisioning fails under kubedock today: floci-az's Artemis
broker never starts (see `docs/docs/architecture.md`, "Service Bus's broker
can't start under kubedock yet"). Key Vault and the native database classes
are unaffected.

**Why:** floci-az customizes the stock `apache/activemq-artemis` broker by
rebuilding two of its jars with floci-az's patches compiled in, then copying
them over the stock ones before the broker starts:

| File copied into the broker | Size | floci-az's actual change |
|---|---|---|
| `artemis-amqp-protocol-2.44.0.jar` | 859 KB | +34 KB over stock (825 KB) |
| `proton-j-0.34.1.jar` | 733 KB | same size as stock |
| extension jar + `broker.xml` + keystore | ~35 KB | all floci-az's |

About 1.5 MB of the ~1.6 MB payload is unmodified upstream code, re-shipped
because a jar is the unit of replacement. kubedock's `--pre-archive` stores
all of a container's pre-start files in one ConfigMap, which Kubernetes caps
at 1 MiB, so the broker pod is never created.

### 1. kubedock: one ConfigMap per pre-archived file (in progress)

- [x] Fix on the kubedock fork: branch `fix/pre-archive-configmap-per-file`
      in `~/git/kubedock` (uncommitted; `make test` + golint pass; PR
      description drafted).
- [ ] Commit, push, open the upstream PR (issue joyrex2001/kubedock#314).
- [x] Verified on k3d via `overrides/kubedock` (2026-09-26): the broker pod
      now starts (six single-file ConfigMaps instead of one oversized one).
- [ ] **Next blocker:** floci-az can't reach the broker it started. It
      thinks it runs in a container, so it dials the container IP + internal
      port (`127.0.0.1:5672`); kubedock reports every container's IP as
      `127.0.0.1` and only reverse-proxies *published* ports (5673/5674) in
      the pod. floci-az times out after 60s and restarts the broker in a
      loop. See "floci-az: reach sidecars through a remote Docker daemon"
      below.
- [ ] Once released, bump `KUBEDOCK_IMAGE` in `src/mayfly/emulators.py`.
- [ ] Remove the "KNOWN GAP" comment there and the Service Bus warnings in
      `docs/docs/spec/services/azure.md` and `docs/docs/architecture.md`.
- [ ] Run `make emulator-test KIND=floci-az` end to end.

Caveat: this only just fits. The largest jar is 859 KB against the 1 MiB
cap (~190 KB headroom); a bigger Artemis release or more floci-az patches
breaks it again. That's why item 2 is the durable fix.

### 1b. floci-az: reach sidecars through a remote Docker daemon

- [x] Built on `feat/docker-endpoint-mode` in `~/git/floci-az-jasondcamp`
      (issue floci-io/floci-az#331): `floci-az.docker.endpoint-mode:
      auto|published`. mayfly's `azure` pod already sets
      `FLOCI_AZ_DOCKER_ENDPOINT_MODE=published` (0.13.0 ignores it).
      Verified 2026-09-27: `make emulator-test KIND=floci-az` PASSED with
      `overrides/floci-az` + `overrides/kubedock`. Service Bus works end to end
      on Kubernetes.
- [x] Committed + pushed `feat/docker-endpoint-mode` (`d18e8bf`); PR for #331.
- [ ] Merge + release; bump the floci-az pin in `src/mayfly/emulators.py`.
- [ ] Original analysis: when floci-az's Docker daemon is remote (`DOCKER_HOST=tcp://...`, as
      with kubedock or docker-in-docker), connect to sidecars at the
      daemon's host + the **published** port, not the container IP +
      internal port. Container IPs of a remote daemon generally aren't
      routable from floci-az (Testcontainers uses the same rule). Logic is
      in `ContainerLifecycleManager` (endpoint selection around line 913,
      `resolveContainerIp`); `ContainerDetector` has no override today.
- [ ] Test via `overrides/floci-az` + `overrides/kubedock`, then
      `make emulator-test KIND=floci-az`.

### 2. floci-az: pre-patched Artemis image (later)

- [ ] floci-az PR: support a broker image that already contains the patched
      jars and extension, and skip copying them when it's in use. floci-az
      already has the setting to override the broker image
      (`artemisImage`); it needs a way to know the image is pre-patched
      (config flag or image label) and to skip the three jar copies. The
      copied payload drops to ~5 KB of config.
- [ ] Build/publish that image per Artemis version (floci-az or mayfly).
- [ ] Point mayfly's floci-az env at it; `--pre-archive` stays for configs.

Side benefit: no 1.6 MB copy per namespace, so broker startup is faster
everywhere, not only under kubedock.

### 3. floci-az: URI-addressed subscription/DLQ fix (ready, uncommitted)

- [x] PR floci-io/floci-az#330 (issue #329), from the fork clone
      `~/git/floci-az-jasondcamp`. Reworked after review: Service Bus
      reduction runs only on Service Bus brokers (Event Hubs unchanged), plus
      a Python compat test (`tests/test_servicebus.py`, pins
      `azure-servicebus==7.15.0b2`). Greptile approved.
- [ ] Merge + release; bump that compat pin to 7.15.0 when it ships.
- [ ] Once released: bump floci-az's pin in `src/mayfly/emulators.py`,
      restore dragonfly's topic-subscription receive in
      `check_servicebus` (`dragonfly/app.py`), drop the Python-subscription
      limit from `docs/docs/spec/services/azure.md`.

### 4. azure-servicebus 7.15.0 GA

- [ ] When 7.15.0 is released (changelog date 2026-10-06), replace the
      `7.15.0b2` pre-release pin in `dragonfly/Dockerfile` and the dev
      group in `pyproject.toml` (`tests/test_dragonfly_azure.py` checks
      they match).

## kubedock 0.23.0: `--reapmax=0`

- [ ] kubedock 0.23.0 includes #290 (`--reapmax=0` disables reaping).
      Bump `KUBEDOCK_IMAGE` (still 0.22.0) in `src/mayfly/emulators.py`,
      switch `--reapmax=876000h` to `--reapmax=0`, and update the matching
      notes in `CLAUDE.md` and `docs/docs/architecture.md`.
