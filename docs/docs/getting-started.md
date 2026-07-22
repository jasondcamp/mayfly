---
sidebar_position: 2
---

# Getting started

## Prerequisites

- A Kubernetes cluster — [k3d](https://k3d.io) (k3s in Docker) is plenty:

  ```bash
  brew install k3d
  k3d cluster create mayfly-dev --kubeconfig-update-default=false \
    -p "80:80@loadbalancer"          # port 80 -> Traefik, for ingress URLs
  export KUBECONFIG=$(k3d kubeconfig write mayfly-dev)
  ```

- `kubectl` on PATH (mayfly uses it for port-forwarding).
- Python 3.9+.

## Install

```bash
pip install mayfly-cli        # installs the `mayfly` command
# or for development:
git clone https://github.com/jasondcamp/mayfly && cd mayfly && uv sync
```

## First environment

```bash
mayfly up examples/env-minimal.yaml   # one database + the dragonfly dashboard
mayfly up examples/env.yaml           # or the kitchen sink
```

The repo's `examples/` directory has runnable specs from a one-database
starter up through per-PR CI templates and multi-engine cache demos, each
with a header explaining what it shows.

Watch it provision: emulator up, S3 buckets, postgres via the RDS API,
caches, Kafka, DynamoDB tables, an ALB, then your apps — each gated on a
readiness probe. The summary prints your URLs:

```text
  Namespace: merry-blonde-stoat
  Dragonfly: http://dragonfly.merry-blonde-stoat.localtest.me/
  Caddis:    http://caddis.merry-blonde-stoat.localtest.me/
  ALB hello-alb: http://hello-alb.merry-blonde-stoat.localtest.me/
  AWS API:   http://aws.merry-blonde-stoat.localtest.me
```

`*.localtest.me` resolves to `127.0.0.1` for free — with the k3d port
mapping above, those URLs work in your browser immediately. On a real
cluster, set [`ingressDomain`](spec/environment#ingressdomain) to your
wildcard DNS zone and the same URLs appear under your own domain.

## The CLI

```bash
mayfly up env.yaml                 # create/update the environment
mayfly status env.yaml             # pods + provisioned secrets
mayfly list                        # all environments, age + TTL
mayfly render env.yaml             # print the resolved plan, touch nothing
mayfly extend env.yaml --ttl 4h    # push expiry out
mayfly restart env.yaml [--app x]  # rolling-restart apps (services untouched)
mayfly down env.yaml               # teardown (namespace delete)
mayfly reap [--dry-run]            # delete every expired environment
mayfly install                     # in-cluster reaper CronJob (see below)
mayfly uninstall                   # remove it (environments untouched)
```

All cluster-touching commands take `--context` / `--kubeconfig`. `down` and
`reap` refuse namespaces not labeled `mayfly.dev/managed=true`, so mayfly
can never delete something it didn't create.

## Unattended cleanup: the in-cluster reaper

`mayfly reap` from a laptop works, but nobody runs it at 3am. `mayfly
install` puts a CronJob in a `mayfly-system` namespace that runs `mayfly
reap` on a schedule (default every 10 minutes, `--schedule` to change), so
expired environments are deleted with nobody watching:

```bash
mayfly install                       # uses ghcr.io/jasondcamp/mayfly-cli:<version>
kubectl -n mayfly-system get jobs    # recent runs; pod logs show what was reaped
mayfly uninstall                     # removes the CronJob + RBAC; envs untouched
```

There is no registry or database behind this: the reaper lists namespaces
labeled `mayfly.dev/managed=true` and deletes the ones past their
`mayfly.dev/expires-at` — the same live cluster state every other command
reads. RBAC can't scope namespace deletion by label, so the ClusterRole
grants namespace get/list/delete and the label guard is enforced in code
(nothing unlabeled is ever touched). If the reaper is down, environments
simply linger until the next `reap` — it fails safe.

## Use the AWS CLI against your environment

With `emulator: {expose: true}` in the spec (on by default in the example —
see [the security note](spec/environment#laptop-access-to-the-aws-api)
before using it on a shared cluster), the AWS API is at
`aws.<namespace>.localtest.me`. Set up a profile once:

```ini
# ~/.aws/config
[profile mayfly]
region = us-east-1
endpoint_url = http://aws.<namespace>.localtest.me

# ~/.aws/credentials
[mayfly]
aws_access_key_id = test
aws_secret_access_key = test
```

Then everything just works: `aws --profile mayfly s3 ls`,
`aws --profile mayfly rds describe-db-instances`,
`aws --profile mayfly secretsmanager list-secrets`, ...

## Seeds are identity

The seed hashes to the environment's name. Same seed → same environment
(idempotent update/heal); new seed → a fresh environment alongside.
`--seed` overrides the spec without editing it:

```bash
mayfly up env.yaml --seed pr-1234           # CI: one env per PR
mayfly up env.yaml --seed scratch-$(whoami) # personal sandbox
```

## Override spec fields with `--set`

`up` and `render` take repeatable `--set path.to.field=value` overrides,
applied to the spec before validation — so CI can deploy PR images without
templating the YAML:

```bash
mayfly up env.yaml --seed pr-1234 \
  --set apps.backend.image=ghcr.io/acme/backend:pr-1234 \
  --set apps.backend.replicas=1
```

Paths walk maps by key and named lists by entry name
(`services.rds.appdb.dbName=other`) or index (`services.s3.buckets.0=tmp`).
Scalars only, and intermediate keys must already exist — a typo'd app name
errors with the list of valid names instead of silently deploying a stray
app. `mayfly render env.yaml --set ...` previews the result without touching
the cluster.
