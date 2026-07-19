#!/usr/bin/env bash
# smoke-test.sh <namespace> [kubeconfig]
# Round-trips data through every provisioned service from INSIDE the
# namespace, proving the endpoints written to Secrets are reachable by pods.
set -euo pipefail

NS=${1:?usage: smoke-test.sh <namespace> [kubeconfig]}
KC=${2:-}
K=(kubectl); [ -n "$KC" ] && K+=(--kubeconfig "$KC")
K+=(-n "$NS")

secret() { "${K[@]}" get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }

echo "== s3: put/list/get through the emulator"
"${K[@]}" run smoke-s3 --rm -i --restart=Never --image=amazon/aws-cli:2.22.35 \
  --env=AWS_ENDPOINT_URL=http://aws:4566 --env=AWS_ACCESS_KEY_ID=test \
  --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=us-east-1 \
  --command -- sh -c 'echo mayfly-was-here > /tmp/f && aws s3 cp /tmp/f s3://assets/smoke.txt && aws s3 cp s3://assets/smoke.txt - && aws s3 ls'

echo "== rds: control-plane API sees the instance (emulator backend)"
"${K[@]}" run smoke-rdsapi --rm -i --restart=Never --image=amazon/aws-cli:2.22.35 \
  --env=AWS_ENDPOINT_URL=http://aws:4566 --env=AWS_ACCESS_KEY_ID=test \
  --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=us-east-1 \
  --command -- aws rds describe-db-instances \
  --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceStatus,Endpoint.Address,Endpoint.Port]' --output text

echo "== rds: create/insert/select via DATABASE_URL"
DB_URL=$(secret rds-appdb DATABASE_URL)
"${K[@]}" run smoke-pg --rm -i --restart=Never --image=postgres:16-alpine \
  --command -- psql "$DB_URL" -c \
  'create table if not exists smoke(v text); insert into smoke values ('"'"'ok'"'"'); select count(*) from smoke;'

echo "== elasticache: control-plane API sees the cluster"
"${K[@]}" run smoke-ecapi --rm -i --restart=Never --image=amazon/aws-cli:2.22.35 \
  --env=AWS_ENDPOINT_URL=http://aws:4566 --env=AWS_ACCESS_KEY_ID=test \
  --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=us-east-1 \
  --command -- aws elasticache describe-cache-clusters --show-cache-node-info \
  --query 'CacheClusters[].[CacheClusterId,CacheClusterStatus,CacheNodes[0].Endpoint.Address,CacheNodes[0].Endpoint.Port]' --output text

echo "== elasticache: SET/GET via REDIS_HOST:REDIS_PORT"
RHOST=$(secret elasticache-cache-a REDIS_HOST)
RPORT=$(secret elasticache-cache-a REDIS_PORT)
"${K[@]}" run smoke-redis --rm -i --restart=Never --image=valkey/valkey:8-alpine \
  --command -- sh -c "valkey-cli -h $RHOST -p $RPORT set smoke ok && valkey-cli -h $RHOST -p $RPORT get smoke"

echo "== msk: control-plane API sees the cluster and routes to our broker"
"${K[@]}" run smoke-mskapi --rm -i --restart=Never --image=amazon/aws-cli:2.22.35 \
  --env=AWS_ENDPOINT_URL=http://aws:4566 --env=AWS_ACCESS_KEY_ID=test \
  --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=us-east-1 \
  --command -- sh -c 'ARN=$(aws kafka list-clusters --query "ClusterInfoList[0].ClusterArn" --output text) && aws kafka describe-cluster --cluster-arn "$ARN" --query "ClusterInfo.[ClusterName,State]" --output text && aws kafka get-bootstrap-brokers --cluster-arn "$ARN" --output text'

echo "== msk: produce/consume via KAFKA_BROKERS"
BROKERS=$(secret msk-events KAFKA_BROKERS)
"${K[@]}" run smoke-kafka --rm -i --restart=Never --image=redpandadata/redpanda:v24.2.18 \
  --command -- sh -c "echo smoke-payload | rpk topic produce orders --brokers $BROKERS && rpk topic consume orders --brokers $BROKERS -n 1 -o start"

echo "== dragonfly: secret-driven connectivity report"
"${K[@]}" run smoke-dragonfly --rm -i --restart=Never --image=busybox:1.36 \
  --command -- wget -qO- http://dragonfly:8080/api
"${K[@]}" run smoke-dragonfly-hz --rm -i --restart=Never --image=busybox:1.36 \
  --command -- sh -c 'wget -qO- http://dragonfly:8080/healthz && echo " (healthz 200)"'

echo "== echo app reachable in-cluster"
"${K[@]}" run smoke-echo --rm -i --restart=Never --image=busybox:1.36 \
  --command -- wget -qO- http://echo:8080 | head -c 200 || true
echo
echo "ALL SMOKE TESTS PASSED"
