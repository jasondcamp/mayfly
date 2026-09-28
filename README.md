# mayfly

Short lived ephemeral environment infrastructure.

One YAML spec declares an environment: cloud services, backing stores, and
app deployments. `mayfly up` materializes it in an isolated Kubernetes
namespace, `mayfly down` (or the TTL reaper) destroys it. No Docker socket,
no privileged pods, no real AWS or Azure.

**Website: https://mayfly.sh** · **Documentation: https://docs.mayfly.sh**
(source in `docs/`, built with `docs/build.sh`).

## Clouds

Each cloud runs as a digest-pinned emulator inside the environment's
namespace. The spec's `emulators:` block names exactly which ones deploy,
and one environment can mix clouds freely:

| Cloud | Service classes | Emulator | Docs |
|---|---|---|---|
| AWS | S3, RDS, ElastiCache, MSK, DynamoDB, ALB, Secrets Manager | MiniStack or floci | [AWS services](https://docs.mayfly.sh/spec/services/aws) |
| Azure | Key Vault, Service Bus, Postgres Flexible Server, Azure SQL | floci-az | [Azure services](https://docs.mayfly.sh/spec/services/azure) |

Every service's endpoints land in a per-service Kubernetes Secret
(`DATABASE_URL`, `REDIS_URL`, `KAFKA_BROKERS`, ...), which is the only
contract apps consume. See [Services](https://docs.mayfly.sh/spec/services)
for backends and the Secret contract.

## Install

```bash
pip install mayfly-cli              # the `mayfly` command
pip install 'mayfly-cli[azure]'     # plus the Azure SDKs, for Azure classes
uv sync                             # dev setup from a checkout
```

Requires `kubectl` on PATH and a Kubernetes cluster (k3d/k3s is plenty).
`helm` is only needed for specs with `helmApps:`. See
[Getting started](https://docs.mayfly.sh/getting-started).

## Usage

```bash
mayfly up env.yaml                 # create/update the environment
mayfly status env.yaml             # pods + provisioned secrets
mayfly list                        # all mayfly environments, age + TTL
mayfly render env.yaml             # print resolved plan, touch nothing
mayfly restart env.yaml [--app x]  # rolling-restart apps
mayfly extend env.yaml --ttl 4h    # push expiry out
mayfly down env.yaml               # teardown (namespace delete)
mayfly reap [--dry-run]            # delete every expired environment
mayfly install / uninstall         # in-cluster reaper CronJob
mayfly version
```

The seed is the environment's identity: same seed → same environment
(idempotent update/heal), new seed → a fresh one alongside it. `mayfly up
--seed pr-1234` in CI gives each PR its own environment from one shared
spec.

## Spec

```yaml
apiVersion: mayfly/v1alpha1
seed: pr-1234
ttl: 8h

emulators:           # explicit: only these deploy
  aws:
    kind: ministack  # or floci
  azure:
    kind: floci-az

services:
  rds:
    - name: appdb
      engine: postgres
      dbName: app
  keyvault:
    - name: app-secrets
      secrets:
        - name: api-key
          generate: true

apps:
  myapi:
    image: ghcr.io/you/myapi:sha-abc123
    port: 3000
    secrets: [rds-appdb, keyvault-app-secrets]  # env-from these secrets
```

Full reference: [Environment](https://docs.mayfly.sh/spec/environment),
[Services](https://docs.mayfly.sh/spec/services),
[Apps](https://docs.mayfly.sh/spec/apps),
[Helm](https://docs.mayfly.sh/spec/helm).

`examples/` has runnable specs, smallest first: `env-minimal.yaml`,
`env-caches.yaml`, `env-pr.yaml` (per-PR CI template), `env-azure.yaml`,
`env-helm.yaml`, `env.yaml` (the kitchen sink), and `env-alb.yaml` (real
AWS ALBs on EKS).

## Guides

- [Internal ALBs](https://docs.mayfly.sh/guides/internal-albs): an emulated
  ALB with a working data plane.
- [dragonfly](https://docs.mayfly.sh/guides/dragonfly): the zero-config
  connectivity verifier; its readiness makes `mayfly up` a connectivity test.
- [caddis](https://docs.mayfly.sh/guides/caddis): the sample app, every
  service doing its real job.
- [Testing](https://docs.mayfly.sh/guides/testing) and
  [images](https://docs.mayfly.sh/guides/images).
- [Architecture and design notes](https://docs.mayfly.sh/architecture): the
  topology and the hard-won gotchas.

## Development

```bash
make test    # unit tests, no cluster
make lint    # ruff
make e2e     # full loop on a disposable k3d cluster
```

`make e2e` never touches your default kubeconfig. CI runs unit + e2e on
every PR. Emulator conformance tests and testing unreleased upstream code
are covered in [Testing](https://docs.mayfly.sh/guides/testing) and
[OVERRIDES.md](OVERRIDES.md).
