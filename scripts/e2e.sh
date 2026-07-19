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

echo "==> building + importing dragonfly (connectivity verifier) + hello (LB test app)"
docker build -q -t dragonfly:dev dragonfly/
docker build -q -t hello:dev hello/
k3d image import dragonfly:dev hello:dev -c "$CLUSTER"

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
