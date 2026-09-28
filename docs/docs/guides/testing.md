---
sidebar_position: 4
---

# Testing

Three tiers, cheapest first:

```bash
make test    # unit tests — spec validation, naming, manifests, backend
             # resolution, patch merging. No cluster needed.
make lint    # ruff
make e2e     # the full loop on a disposable k3d cluster
```

## The e2e harness

`scripts/e2e.sh` is fully self-contained and never touches your default
kubeconfig (prerequisites: `k3d`, `kubectl`, `uv`, `helm` — the example
spec includes a `helmApps:` entry):

1. creates a throwaway k3d cluster;
2. builds the mayfly images (dragonfly, hello, caddis) from
   the working tree **under their published names** and imports them — so
   e2e always tests your local code and the cluster never pulls;
3. `mayfly up examples/env.yaml`;
4. runs the in-cluster smoke test — S3 round-trip, RDS control-plane +
   psql, caches, Kafka produce/consume, dynamo, the ALB, the podinfo helm
   app (rollout + interpolated secret value), dragonfly's `/api` and
   `/healthz`;
5. `mayfly up` again (idempotency proof), `mayfly down`, cluster deleted.

CI runs unit (Python 3.10 + 3.13 matrix) then e2e on every push and PR.

## Emulator conformance tests

A fourth, on-demand tier isolates each emulator kind. Run it when bumping
an emulator pin, changing a provisioner, or touching the manifests that
shape the emulator pods:

```bash
make emulator-test KIND=ministack   # every class through the real AWS API
make emulator-test KIND=floci       # in-process APIs + the native fallback
make emulator-test KIND=floci-az    # Azure: keyvault/servicebus emulated,
                                    # postgresflexible/azuresql native
```

Each run is the full disposable-cluster loop (`scripts/emulator-test.sh`):
create k3d cluster → `mayfly up` that kind's spec
(`tests/emulators/env-<kind>.yaml`) → its smoke script → `up` again
(idempotency) → `down` → delete cluster. Dragonfly (and `hello`, where the
spec uses it) is built from the working tree and the spec's image pin is
overridden with `--set`, so the checks always match your local code — the
floci-az smoke explicitly fails if it detects a dragonfly image without
the Azure checks rather than passing vacuously.

What each conformance run asserts, beyond "it comes up":

- **ministack** — control-plane truth (`describe-*` reports `available`),
  the kubedock container path (psql + redis against spawned pods), MSK's
  hybrid bootstrap-broker answer, the ALB data plane, DynamoDB and Secrets
  Manager round-trips.
- **floci** — the in-process trio (s3/dynamodb/secretsmanager) through the
  emulator, the automatic native fallback for containers, **and that the
  control plane is honestly empty** for native services (no phantom
  `DBInstances`).
- **floci-az** — the Secret contract keys, both database engines directly
  (psql, `sqlcmd`), the REST + AMQP ports, and dragonfly's four Azure
  tiles green (Key Vault reads, Service Bus AMQP send/receive).

In CI these run as the manually-dispatched `emulators` workflow — all
three in a matrix, or one:

```bash
gh workflow run emulators                       # all three
gh workflow run emulators -f emulator=floci-az  # just one
```

Unit tests (`tests/test_emulator_specs.py`) keep the conformance specs
validating, resolving to the intended backends, and in lock-step with the
workflow's matrix — so spec-model changes that would break this tier fail
in `make test` first.

## Testing unreleased emulator or kubedock changes

To run mayfly against local, unreleased code for kubedock, MiniStack, floci
or floci-az, put the code in `overrides/<component>` (a symlink to your
checkout works) and run the usual loop. `make emulator-test` and `make e2e`
build and import the overrides automatically; on a long-lived cluster, use
`make overrides CLUSTER=mayfly-dev` and `source overrides/.env`. mayfly then
uses the local images in place of the pinned ones (via `MAYFLY_*_IMAGE`) and
says so on every `up` and `render`. Full details, including what each
component expects and how it's built, are in
[`OVERRIDES.md`](https://github.com/jasondcamp/mayfly/blob/main/OVERRIDES.md)
in the repository.

## Interactive iteration

Keep a long-lived local cluster instead of paying cluster-create each run:

```bash
k3d cluster create mayfly-dev --kubeconfig-update-default=false -p "80:80@loadbalancer"
export KUBECONFIG=$(k3d kubeconfig write mayfly-dev)
mayfly up examples/env.yaml
./examples/smoke-test.sh <namespace>
```

After changing an app image locally, rebuild + `k3d image import` under the
same name/tag and `kubectl rollout restart` its deployment — same-tag images
don't trigger a rollout by themselves.

Before anything serious, give a real multi-node k3s cluster a second-tier
pass, pointed at a dedicated kubeconfig/context. Scheduling, image-pull
latency and storage behave differently there than on single-node k3d.
