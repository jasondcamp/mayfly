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
kubeconfig:

1. creates a throwaway k3d cluster;
2. builds the mayfly images (dragonfly, hello, the patched emulator) from
   the working tree **under their published names** and imports them — so
   e2e always tests your local code and the cluster never pulls;
3. `mayfly up examples/env.yaml`;
4. runs the in-cluster smoke test — S3 round-trip, RDS control-plane +
   psql, caches, Kafka produce/consume, dynamo, the ALB, dragonfly's
   `/api` and `/healthz`;
5. `mayfly up` again (idempotency proof), `mayfly down`, cluster deleted.

CI runs unit (Python 3.9 + 3.13 matrix) then e2e on every push and PR.

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
