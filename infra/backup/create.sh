#!/usr/bin/env bash
set -euo pipefail

recovery_point_id="${RECOVERY_POINT_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
backup_dir="${MUNINN_BACKUP_DIR:-.artifacts/backups}/${recovery_point_id}"
mkdir -p "${backup_dir}"

: "${MUNINN_POSTGRES_SERVICE:=muninn-postgresql}"
: "${MUNINN_POSTGRES_DB:=muninn}"
: "${MUNINN_POSTGRES_USER:=muninn}"
: "${MUNINN_NAMESPACE:=muninn}"

kubectl -n "${MUNINN_NAMESPACE}" scale deployment/muninn-job-manager deployment/muninn-worker --replicas=0
trap 'kubectl -n "${MUNINN_NAMESPACE}" scale deployment/muninn-job-manager deployment/muninn-worker --replicas=1 >/dev/null' EXIT

kubectl -n "${MUNINN_NAMESPACE}" exec statefulset/"${MUNINN_POSTGRES_SERVICE}" -- \
  pg_dump --format=custom --file=/tmp/muninn.dump --dbname="${MUNINN_POSTGRES_DB}" --username="${MUNINN_POSTGRES_USER}"
kubectl -n "${MUNINN_NAMESPACE}" cp \
  "${MUNINN_POSTGRES_SERVICE}:/tmp/muninn.dump" "${backup_dir}/postgres.dump"
kubectl -n "${MUNINN_NAMESPACE}" exec deployment/muninn-minio -- \
  tar -C /data -czf - . > "${backup_dir}/minio-data.tar.gz"
kubectl -n "${MUNINN_NAMESPACE}" exec deployment/muninn-nats -- \
  tar -C /data -czf - . > "${backup_dir}/jetstream-data.tar.gz"

printf 'RECOVERY_POINT_ID=%s\n' "${recovery_point_id}"
