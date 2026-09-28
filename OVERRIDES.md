# Dev overrides

Run mayfly against **unreleased** kubedock, MiniStack, floci or floci-az
code: put the code in `overrides/<component>`, build, and mayfly uses your
local image instead of the pinned one. Use it to try a fix before it's
upstream, or to reproduce a bug against a patched build.

This is for local testing only. `overrides/` is gitignored, and nothing
here changes what mayfly ships. To *ship* a divergence while waiting on
upstream, use the overlay pattern in `docs/docs/architecture.md`.

## Quick start

```bash
# 1. Point a component at your checkout (a symlink is simplest)
ln -s ~/git/kubedock overrides/kubedock

# 2a. Full test loop: the scripts build + import overrides automatically
make emulator-test KIND=floci-az      # or: make e2e

# 2b. Or on your long-lived dev cluster
make overrides CLUSTER=mayfly-dev     # build + k3d image import
source overrides/.env                 # export MAYFLY_*_IMAGE
mayfly up examples/env-azure.yaml --kubeconfig "$(k3d kubeconfig write mayfly-dev)"
```

`mayfly up` and `mayfly render` print a line for every active override:

```
note: dev override kubedock -> mayfly-override/kubedock:dev-3f9a1c2b7d04 (MAYFLY_KUBEDOCK_IMAGE)
```

and `render` records them in its header under `devOverrides`. To stop using
overrides, open a new shell (or `unset` the variables) and remove the
entries from `overrides/`.

## What goes in `overrides/<component>`

Components: `kubedock`, `ministack`, `floci`, `floci-az`. Each can be one of
two things, detected automatically:

### A `rootfs/` overlay (any component)

Files under `overrides/<component>/rootfs/` are copied over the component's
**pinned** image at the same paths. This is the old `emulator/` pattern:
replace whole files, no patch tooling.

```
overrides/ministack/rootfs/opt/ministack/ministack/services/alb.py
```

Best for interpreted code (MiniStack's Python) and config files. It won't
work for compiled code: kubedock is a Go binary and floci/floci-az are
native Java images.

### A source checkout (or a symlink to one)

| Component | Detected by | Build |
|---|---|---|
| `kubedock` | `go.mod` | `go build` in `golang:<go.mod version>-alpine` for the Docker host's architecture; the binary replaces `/usr/local/bin/kubedock` in the pinned kubedock image |
| `ministack` | `Dockerfile` | the checkout's own `Dockerfile` |
| `floci`, `floci-az` | `pom.xml` + `docker/Dockerfile.jvm-package` | `mvn package -DskipTests` in `maven:3.9-eclipse-temurin-25`, then `docker/Dockerfile.jvm-package` |

Anything else in `overrides/` is an error, so a typo can't silently fall back
to the pinned image.

Notes:

- **floci / floci-az build as JVM images.** The published images are native
  (GraalVM) builds, which take far too long to build locally. Behavior
  matches; startup is slower and memory use higher.
- **The floci recipe is untested.** floci-az's layout is verified; floci
  (AWS) is assumed to match since it's the same project family.
- **Build caches** (Go modules, Maven repository) live in
  `overrides/.cache/`, so rebuilds are quick. Delete it to start clean.
- Builds write into your checkout the way a normal build would (for
  example floci-az's `target/`).

## How it works

- `scripts/overrides.sh build [--cluster NAME]` builds each override and
  tags it with the built image's ID: `mayfly-override/<component>:dev-<id>`.
  Every build gets a new tag, even for unchanged code, so pods always roll
  over to the latest build (a reused tag would leave running pods on the old
  image). With `--cluster` the image is `k3d image import`ed. It writes
  `overrides/.env`:

  ```bash
  export MAYFLY_KUBEDOCK_IMAGE=mayfly-override/kubedock:dev-3f9a1c2b7d04
  ```

- mayfly reads `MAYFLY_KUBEDOCK_IMAGE`, `MAYFLY_MINISTACK_IMAGE`,
  `MAYFLY_FLOCI_IMAGE` and `MAYFLY_FLOCI_AZ_IMAGE`. An override wins over
  the spec's `image`/`version` and the pinned default, for every environment
  that uses the component. Values need an explicit tag or digest, and
  `latest` is rejected.
- A kubedock override runs in both the `aws` and `azure` emulator pods, and
  mayfly passes `--initimage` set to the same image. kubedock's own
  default for its setup init containers is `joyrex2001/kubedock:<version>`,
  which doesn't exist for a local build.
- `scripts/overrides.sh plan` lists what's in `overrides/` and how each
  component will build, without building anything.
- `make emulator-test` and `make e2e` run the build automatically when
  `overrides/` isn't empty, and fail if an entry is invalid.

You can also set the `MAYFLY_*_IMAGE` variables by hand to any image the
cluster can pull. The build script is only a convenience.
