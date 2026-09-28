#!/usr/bin/env bash
# smoke-floci-az.sh <namespace> [kubeconfig]
# floci-az (Azure) conformance. The SDK-level round-trips (Key Vault auth
# dance, Service Bus AMQP) live in dragonfly — this script proves the
# secrets contract, hits the databases directly, and then requires
# dragonfly to report every Azure tile green. NOTE: needs a dragonfly
# image with the Azure checks (the emulator-test runner builds one from
# the working tree); it fails loudly on an older image instead of
# passing vacuously.
set -euo pipefail

NS=${1:?usage: smoke-floci-az.sh <namespace> [kubeconfig]}
KC=${2:-}
K=(kubectl); [ -n "$KC" ] && K+=(--kubeconfig "$KC")
K+=(-n "$NS")

secret() { "${K[@]}" get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }

echo "== secrets: contract keys present"
[ "$(secret keyvault-app-secrets KEYVAULT_URL)" = "http://azure:4577/app-secrets-keyvault" ]
[ "$(secret keyvault-app-secrets SECRET_API_KEY)" = "test-key-123" ]
[ -n "$(secret keyvault-app-secrets SECRET_SIGNING_KEY)" ]
secret servicebus-main SERVICEBUS_CONNECTION_STRING | grep -q 'sb://azure:5673/'
[ "$(secret servicebus-main SERVICEBUS_QUEUES)" = "jobs" ]

echo "== azure emulator: REST + AMQP ports reachable in-cluster"
"${K[@]}" run smoke-azports --rm -i --restart=Never --image=busybox:1.36 \
  --command -- sh -c 'nc -z azure 4577 && nc -z azure 5673 && echo ports open'

echo "== postgresflexible (native): psql via DATABASE_URL"
DB_URL=$(secret pgflex-appdb DATABASE_URL)
"${K[@]}" run smoke-pgflex --rm -i --restart=Never --image=postgres:16-alpine \
  --command -- psql "$DB_URL" -c \
  'create table if not exists smoke(v text); insert into smoke values ('"'"'ok'"'"'); select count(*) from smoke;'

echo "== azuresql (native): sqlcmd insert+select via DB_* secret"
SQL_HOST=$(secret azuresql-coredb DB_HOST)
SQL_PASS=$(secret azuresql-coredb DB_PASSWORD)
SQL_DB=$(secret azuresql-coredb DB_NAME)
"${K[@]}" run smoke-mssql --rm -i --restart=Never \
  --image=mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04 \
  --command -- /opt/mssql-tools18/bin/sqlcmd -S "$SQL_HOST" -U sa -P "$SQL_PASS" -d "$SQL_DB" -C -Q \
  "IF OBJECT_ID('smoke') IS NULL CREATE TABLE smoke (v varchar(16)); INSERT INTO smoke VALUES ('ok'); SELECT count(*) FROM smoke;"

echo "== dragonfly: every Azure tile present and /healthz green"
API=$("${K[@]}" run smoke-dragonfly-api --rm -i --restart=Never --image=busybox:1.36 \
  --command -- wget -qO- http://dragonfly:8080/api)
for kind in keyvault servicebus pgflex azuresql; do
  echo "$API" | grep -q "\"kind\": \"$kind\"" \
    || { echo "dragonfly reports no $kind tile — old dragonfly image without Azure checks?"; exit 1; }
done
"${K[@]}" run smoke-dragonfly-hz --rm -i --restart=Never --image=busybox:1.36 \
  --command -- wget -qO- http://dragonfly:8080/healthz

echo "FLOCI-AZ SMOKE PASSED"
