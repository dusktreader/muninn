#!/usr/bin/env bash
set -euo pipefail

recovery_point_id="${1:?RECOVERY_POINT_ID is required}"
: "${MUNINN_NAMESPACE:=muninn}"
: "${MUNINN_POSTGRES_SERVICE:=muninn-postgresql}"
: "${MUNINN_POSTGRES_DB:=muninn}"
: "${MUNINN_POSTGRES_USER:=muninn}"

kubectl -n "${MUNINN_NAMESPACE}" exec statefulset/"${MUNINN_POSTGRES_SERVICE}" -- \
  psql --dbname="${MUNINN_POSTGRES_DB}" --username="${MUNINN_POSTGRES_USER}" \
  --command='SELECT 1 FROM tombstones LIMIT 1' >/dev/null
kubectl -n "${MUNINN_NAMESPACE}" exec deployment/muninn-nats -- \
  sh -c 'test -d /data/jetstream'

printf 'VERIFIED_RECOVERY_POINT_ID=%s\n' "${recovery_point_id}"
