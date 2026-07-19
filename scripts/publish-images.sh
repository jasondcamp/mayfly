#!/usr/bin/env bash
# publish-images.sh [version]
# Build and push mayfly's container images to ghcr.io/jasondcamp as
# multi-arch (amd64 + arm64): mayfly-dragonfly, mayfly-hello,
# mayfly-ministack. Tags: <version> and latest.
#
# Auth: docker must be logged in to ghcr.io with a token that has
# write:packages, e.g.:  gh auth token | docker login ghcr.io -u jasondcamp --password-stdin
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)}
REGISTRY=ghcr.io/jasondcamp

command -v docker >/dev/null || { echo "missing dependency: docker" >&2; exit 1; }
docker buildx inspect mayfly-builder >/dev/null 2>&1 \
  || docker buildx create --name mayfly-builder --driver docker-container >/dev/null

for pair in dragonfly:dragonfly hello:hello ministack:emulator; do
  name="mayfly-${pair%%:*}"; dir="${pair##*:}"
  echo "==> ${REGISTRY}/${name}:${VERSION} (+ latest) from ${dir}/"
  docker buildx build --builder mayfly-builder \
    --platform linux/amd64,linux/arm64 \
    -t "${REGISTRY}/${name}:${VERSION}" -t "${REGISTRY}/${name}:latest" \
    --push "${dir}/"
done

echo "==> published. New packages default to PRIVATE — make them public once at:"
echo "    https://github.com/jasondcamp?tab=packages  (each package -> settings -> Change visibility)"
