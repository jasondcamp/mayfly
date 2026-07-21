---
sidebar_position: 5
---

# Images and releases

mayfly ships five container images, published multi-arch (amd64 + arm64)
to GHCR:

| Image | Source | Purpose |
|---|---|---|
| `ghcr.io/jasondcamp/mayfly-dragonfly` | `dragonfly/` | connectivity verifier |
| `ghcr.io/jasondcamp/mayfly-hello` | `hello/` | LB/ingress test app: shows serving pod + forwarded headers |
| `ghcr.io/jasondcamp/mayfly-ministack` | `emulator/` | MiniStack + mayfly patches (ALB HTTP data plane, valkey engine) |
| `ghcr.io/jasondcamp/mayfly-caddis` | `caddis/` | sample app: Flask API + Kafka worker |
| `ghcr.io/jasondcamp/mayfly-caddis-frontend` | `caddis-frontend/` | sample app UI: nginx + same-origin /api proxy |

## CI publishing

Push to the `deploy` branch (or run the workflow manually) →
`.github/workflows/docker-build.yml`: tests gate, per-platform builds on
**native runners** (no QEMU), per-digest pushes, then a merged multi-arch
manifest per image tagged `<branch>`, `<branch>-<sha>`, `latest`, and the
version from `pyproject.toml`. Auth is the built-in `GITHUB_TOKEN`.

Note: the free arm runners require a **public** repository, and new GHCR
packages start **private** — flip each to public once in the package
settings.

## Local publishing

```bash
scripts/publish-images.sh          # version from pyproject.toml
scripts/publish-images.sh 1.2.3    # explicit
```

Requires a `write:packages` login:
`gh auth token | docker login ghcr.io -u <you> --password-stdin`.

## The CLI package

`mayfly-cli` on PyPI (the command installs as `mayfly`). `scripts/release.sh`
bumps the patch version (pass `minor`/`major`/`--no-bump` to override),
runs lint + tests, builds sdist + wheel, installs the wheel into a scratch
venv and proves the entry point works, then `--publish` uploads via
`uv publish`.
