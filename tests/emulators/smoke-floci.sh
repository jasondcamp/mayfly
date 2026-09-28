#!/usr/bin/env bash
# smoke-floci.sh <namespace> [kubeconfig]
# floci (AWS) conformance: in-process APIs (s3/dynamodb/secretsmanager)
# through the emulator, and the automatic native fallback for container
# services (rds/elasticache/msk) — identical Secret contract either way.
set -euo pipefail

NS=${1:?usage: smoke-floci.sh <namespace> [kubeconfig]}
KC=${2:-}
K=(kubectl); [ -n "$KC" ] && K+=(--kubeconfig "$KC")
K+=(-n "$NS")

secret() { "${K[@]}" get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }
awscli() {
  "${K[@]}" run "$1" --rm -i --restart=Never --image=amazon/aws-cli:2.22.35 \
    --env=AWS_ENDPOINT_URL=http://aws:4566 --env=AWS_ACCESS_KEY_ID=test \
    --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=us-east-1 \
    --command -- sh -c "$2"
}

echo "== s3: put/get through the emulator"
awscli smoke-s3 'echo mayfly-was-here > /tmp/f && aws s3 cp /tmp/f s3://assets/smoke.txt && aws s3 cp s3://assets/smoke.txt -'

echo "== dynamodb: put/get item"
awscli smoke-ddb 'aws dynamodb put-item --table-name sessions --item "{\"id\":{\"S\":\"smoke\"}}" && aws dynamodb get-item --table-name sessions --key "{\"id\":{\"S\":\"smoke\"}}" --output text | grep smoke'

echo "== secretsmanager: get-secret-value returns the fixture"
awscli smoke-sm 'aws secretsmanager get-secret-value --secret-id app/api-key --query SecretString --output text | grep -x test-key-123'

echo "== rds (native fallback): psql via DATABASE_URL"
DB_URL=$(secret rds-appdb DATABASE_URL)
"${K[@]}" run smoke-pg --rm -i --restart=Never --image=postgres:16-alpine \
  --command -- psql "$DB_URL" -c \
  'create table if not exists smoke(v text); insert into smoke values ('"'"'ok'"'"'); select count(*) from smoke;'

echo "== rds: control plane is honestly empty (native, not emulator-backed)"
awscli smoke-rdsapi '[ "$(aws rds describe-db-instances --query "length(DBInstances)" --output text)" = "0" ]'

echo "== elasticache (native fallback): SET/GET via secret endpoint"
RHOST=$(secret elasticache-cache-a REDIS_HOST)
RPORT=$(secret elasticache-cache-a REDIS_PORT)
"${K[@]}" run smoke-redis --rm -i --restart=Never --image=valkey/valkey:8-alpine \
  --command -- sh -c "valkey-cli -h $RHOST -p $RPORT set smoke ok && valkey-cli -h $RHOST -p $RPORT get smoke | grep -x ok"

echo "== msk (native fallback): produce/consume via KAFKA_BROKERS"
BROKERS=$(secret msk-events KAFKA_BROKERS)
"${K[@]}" run smoke-kafka --rm -i --restart=Never --image=redpandadata/redpanda:v24.2.18 \
  --command -- sh -c "echo smoke-payload | rpk topic produce orders --brokers $BROKERS && rpk topic consume orders --brokers $BROKERS -n 1 -o start"

echo "== dragonfly: /healthz green"
"${K[@]}" run smoke-dragonfly --rm -i --restart=Never --image=busybox:1.36 \
  --command -- wget -qO- http://dragonfly:8080/healthz

echo "FLOCI SMOKE PASSED"
