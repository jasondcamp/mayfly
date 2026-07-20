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
mayfly up examples/env.yaml
```

Watch it provision: emulator up, S3 buckets, postgres via the RDS API,
caches, Kafka, DynamoDB tables, an ALB, then your apps — each gated on a
readiness probe. The summary prints your URLs:

```text
  Namespace: merry-blonde-stoat
  Dragonfly: http://dragonfly.merry-blonde-stoat.localtest.me/
  ALB hello-alb: http://hello-alb.merry-blonde-stoat.localtest.me/
```

`*.localtest.me` resolves to `127.0.0.1` for free — with the k3d port
mapping above, those URLs work in your browser immediately.

## The CLI

```bash
mayfly up env.yaml                 # create/update the environment
mayfly status env.yaml             # pods + provisioned secrets
mayfly list                        # all environments, age + TTL
mayfly render env.yaml             # print the resolved plan, touch nothing
mayfly extend env.yaml --ttl 4h    # push expiry out
mayfly down env.yaml               # teardown (namespace delete)
mayfly reap [--dry-run]            # delete every expired environment
```

All cluster-touching commands take `--context` / `--kubeconfig`. `down` and
`reap` refuse namespaces not labeled `mayfly.dev/managed=true`, so mayfly
can never delete something it didn't create.

## Seeds are identity

The seed hashes to the environment's name. Same seed → same environment
(idempotent update/heal); new seed → a fresh environment alongside.
`--seed` overrides the spec without editing it:

```bash
mayfly up env.yaml --seed pr-1234           # CI: one env per PR
mayfly up env.yaml --seed scratch-$(whoami) # personal sandbox
```
