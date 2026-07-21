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
  Caddis:    http://caddis.merry-blonde-stoat.localtest.me/
  ALB hello-alb: http://hello-alb.merry-blonde-stoat.localtest.me/
  AWS API:   http://aws.merry-blonde-stoat.localtest.me
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
mayfly restart env.yaml [--app x]  # rolling-restart apps (services untouched)
mayfly down env.yaml               # teardown (namespace delete)
mayfly reap [--dry-run]            # delete every expired environment
```

All cluster-touching commands take `--context` / `--kubeconfig`. `down` and
`reap` refuse namespaces not labeled `mayfly.dev/managed=true`, so mayfly
can never delete something it didn't create.

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
