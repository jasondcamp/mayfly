---
sidebar_position: 4
---

# Helm apps

`helmApps:` deploys a Helm chart as part of the environment — a chart is,
in practice, a wrapper around a specific application version and a set of
variables, and mayfly generates those variables from the spec on the fly.

**Helm is only a renderer here.** mayfly runs `helm template` client-side
with an exact version pin and a values file it generates, then pushes the
rendered manifests through the same pipeline as everything else: forced
into the environment namespace, invariants enforced, applied with
server-side apply. **No helm release object exists** — `helm list` will
show nothing, there is nothing to `helm uninstall`, and the namespace TTL
is the only lifecycle. `mayfly down` (or the reaper) removes everything.

Requires the `helm` CLI on PATH — only when the spec actually has
`helmApps:`; `mayfly up` fails with a friendly error before touching the
cluster otherwise.

```yaml
helmApps:
  podinfo:
    enabled: true                                   # default true
    chart: podinfo                                  # bare chart name — required
    repo: https://stefanprodan.github.io/podinfo    # HTTP(S) chart repo — required
    version: 6.9.1                                  # exact pin — required;
                                                    # no ranges (^ ~ >=), no latest
    timeoutSeconds: 300        # total rollout budget for the chart's Deployments
    values:                    # passed to helm template as a generated values file
      replicaCount: 1
      ui:
        message: "api key is ${secret:sm-app-api-key:SECRET_VALUE}"
    check:                     # optional dragonfly health check (APPS card)
      kind: http               # http | tcp
      target: http://podinfo:9898/healthz   # tcp targets are "service:port"
```

## Values from provisioned services

Any string inside `values:` may reference the [per-service Secret
contract](./services/index.md#secrets-the-app-contract) with
`${secret:<secret-name>:<KEY>}` — e.g. `${secret:rds-appdb:DATABASE_URL}`
or `${secret:sm-app-api-key:SECRET_VALUE}`. Placeholders resolve at `up`
time, after services are provisioned and before the chart renders, so the
chart receives plain values. An unknown secret name or key is a hard error
(listing what is available) before anything is applied.

`mayfly render` runs without a cluster and therefore without provisioned
secrets: it renders charts with placeholders left **verbatim** so the plan
stays inspectable.

## What mayfly enforces on rendered manifests

Rendered docs are not applied as-is:

- `metadata.namespace` is forced to the environment namespace.
- Every object is labeled `mayfly.dev/helm-app: <name>` for attribution.
- `enableServiceLinks: false` is asserted on every pod template
  (Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, bare Pods) —
  same invariant as generated apps; see
  [architecture](../architecture.md) for why.
- **Cluster-scoped objects are skipped** with a warning (Namespaces,
  ClusterRoles, CRDs, PVs, webhooks, ...). They would outlive the
  namespace TTL and collide between environments — an environment must
  stay namespace-contained. Charts that require cluster-scoped installs
  don't fit the ephemeral-environment model.
- **Chart hooks are excluded** (`--no-hooks`): test pods and
  pre/post-install jobs only mean something inside helm's lifecycle,
  which doesn't exist here. Use [`initApps:`](./apps.md) for one-shot
  setup instead.

## Ordering and readiness

Helm apps are applied after `apps:` are applied but before their readiness
is awaited (so dragonfly checks that reference helm apps can go green).
Readiness for a helm app means: every **Deployment** the chart rendered
completes its rollout within `timeoutSeconds` (a total budget per helm
app). StatefulSets and DaemonSets are applied but not awaited.

## Limitations

- HTTP(S) chart repositories only — no OCI references or local chart
  paths yet.
- Re-running `up` re-renders and re-applies (server-side apply is
  idempotent), but objects a new chart version no longer renders are not
  pruned — like apps removed from the spec, they die with the namespace.
- A chart renders its own resource names; mayfly rejects a `helmApps:` key
  that exactly matches an `apps:` key, but cannot statically prevent every
  name collision a chart might produce.
- Unknown custom-resource kinds are assumed namespaced; a truly
  cluster-scoped one fails at apply time with the server's error.
