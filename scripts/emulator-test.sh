#!/usr/bin/env bash
# emulator-test.sh <ministack|floci|floci-az> [cluster-name]
# Emulator conformance test on a disposable k3d cluster: create cluster ->
# mayfly up the emulator's spec -> per-emulator smoke test -> up again
# (idempotency) -> down -> delete cluster. The default kubeconfig is never
# touched. Use it when bumping an emulator pin or changing provisioners:
#
#   ./scripts/emulator-test.sh ministack
#   ./scripts/emulator-test.sh floci
#   ./scripts/emulator-test.sh floci-az
#
# CI: the `emulators` workflow (manual dispatch) runs the same script.
set -euo pipefail
cd "$(dirname "$0")/.."

KIND=${1:?usage: emulator-test.sh <ministack|floci|floci-az> [cluster-name]}
CLUSTER=${2:-mayfly-emu-${KIND}}
SPEC=tests/emulators/env-${KIND}.yaml
SMOKE=tests/emulators/smoke-${KIND}.sh
[ -f "$SPEC" ] || { echo "unknown emulator kind: ${KIND} (no ${SPEC})" >&2; exit 1; }

for bin in k3d kubectl uv docker; do
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

# Dev overrides (OVERRIDES.md): locally built kubedock/ministack/floci/floci-az
# images from overrides/, imported into the cluster and exported for mayfly.
OVERRIDES_PLAN=$(./scripts/overrides.sh plan)  # fails the run on a bad override
if [ -n "$OVERRIDES_PLAN" ]; then
  echo "==> building dev overrides"
  ./scripts/overrides.sh build --cluster "$CLUSTER" >/dev/null
  # shellcheck disable=SC1091
  source overrides/.env
fi

# Build the apps the spec uses from the working tree and import them, so
# the test always exercises local code (dragonfly's checks especially —
# the published pin may lag the spec surface).
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
SET_ARGS=(--set "apps.dragonfly.image=ghcr.io/jasondcamp/mayfly-dragonfly:${V}")
IMAGES=("ghcr.io/jasondcamp/mayfly-dragonfly:${V}")
echo "==> building + importing dragonfly at ${V} (working tree)"
docker build -q -t "ghcr.io/jasondcamp/mayfly-dragonfly:${V}" dragonfly/
if grep -q '^  hello:' "$SPEC"; then
  echo "==> building + importing hello at ${V}"
  docker build -q -t "ghcr.io/jasondcamp/mayfly-hello:${V}" hello/
  SET_ARGS+=(--set "apps.hello.image=ghcr.io/jasondcamp/mayfly-hello:${V}")
  IMAGES+=("ghcr.io/jasondcamp/mayfly-hello:${V}")
fi
k3d image import "${IMAGES[@]}" -c "$CLUSTER"

echo "==> mayfly up (${KIND})"
uv run mayfly up "$SPEC" "${SET_ARGS[@]}" --kubeconfig "$KC"

NS=$(uv run mayfly render "$SPEC" | sed -n 's/^  namespace: //p' | head -1)
echo "==> smoke test (${SMOKE}, namespace ${NS})"
chmod +x "$SMOKE"
"$SMOKE" "$NS" "$KC"

echo "==> idempotency: mayfly up again"
uv run mayfly up "$SPEC" "${SET_ARGS[@]}" --kubeconfig "$KC"

echo "==> mayfly down"
uv run mayfly down "$SPEC" --kubeconfig "$KC"

echo "==> EMULATOR TEST PASSED (${KIND})"
