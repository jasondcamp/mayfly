#!/usr/bin/env bash
# e2e.sh [cluster-name]
# Full end-to-end test on a disposable k3d cluster: create cluster ->
# mayfly up -> smoke test -> mayfly down -> delete cluster.
# The default kubeconfig is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=${1:-mayfly-e2e}
SPEC=examples/env.yaml

for bin in k3d kubectl uv; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

cleanup() {
  echo "==> deleting k3d cluster ${CLUSTER}"
  k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> creating k3d cluster ${CLUSTER}"
k3d cluster create "$CLUSTER" \
  --kubeconfig-update-default=false \
  --kubeconfig-switch-context=false \
  --wait --timeout 180s
KC=$(k3d kubeconfig write "$CLUSTER")

# Build the images locally under their published names and import them, so
# e2e always tests the working tree (the cluster never needs to pull).
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
echo "==> building + importing mayfly images (dragonfly, hello, ministack) at ${V}"
docker build -q -t ghcr.io/jasondcamp/mayfly-dragonfly:${V} dragonfly/
docker build -q -t ghcr.io/jasondcamp/mayfly-hello:${V} hello/
docker build -q -t ghcr.io/jasondcamp/mayfly-ministack:${V} emulator/
docker build -q -t ghcr.io/jasondcamp/mayfly-caddis:${V} caddis/
docker build -q -t ghcr.io/jasondcamp/mayfly-caddis-frontend:${V} caddis-frontend/
k3d image import ghcr.io/jasondcamp/mayfly-dragonfly:${V} ghcr.io/jasondcamp/mayfly-hello:${V} ghcr.io/jasondcamp/mayfly-ministack:${V} ghcr.io/jasondcamp/mayfly-caddis:${V} ghcr.io/jasondcamp/mayfly-caddis-frontend:${V} -c "$CLUSTER"

echo "==> mayfly up"
uv run mayfly up "$SPEC" --kubeconfig "$KC"

NS=$(uv run mayfly render "$SPEC" | sed -n 's/^  namespace: //p' | head -1)
echo "==> smoke test (namespace ${NS})"
./examples/smoke-test.sh "$NS" "$KC"

echo "==> idempotency: mayfly up again"
uv run mayfly up "$SPEC" --kubeconfig "$KC"

echo "==> mayfly down"
uv run mayfly down "$SPEC" --kubeconfig "$KC"

echo "==> E2E PASSED"
