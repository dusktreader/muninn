#!/usr/bin/env bash
set -euo pipefail

recovery_point_id="${1:?RECOVERY_POINT_ID is required}"
backup_dir="${MUNINN_BACKUP_DIR:-.artifacts/backups}/${recovery_point_id}"
: "${MUNINN_NAMESPACE:=muninn}"
: "${MUNINN_POSTGRES_SERVICE:=muninn-postgresql}"
: "${MUNINN_POSTGRES_DB:=muninn}"
: "${MUNINN_POSTGRES_USER:=muninn}"

test -f "${backup_dir}/postgres.dump"
test -f "${backup_dir}/minio-data.tar.gz"
test -f "${backup_dir}/jetstream-data.tar.gz"

kubectl -n "${MUNINN_NAMESPACE}" scale deployment/muninn-api deployment/muninn-job-manager deployment/muninn-worker --replicas=0
kubectl -n "${MUNINN_NAMESPACE}" cp "${backup_dir}/postgres.dump" \
  "${MUNINN_POSTGRES_SERVICE}:/tmp/muninn.dump"
kubectl -n "${MUNINN_NAMESPACE}" exec statefulset/"${MUNINN_POSTGRES_SERVICE}" -- \
  pg_restore --clean --if-exists --dbname="${MUNINN_POSTGRES_DB}" --username="${MUNINN_POSTGRES_USER}" /tmp/muninn.dump
kubectl -n "${MUNINN_NAMESPACE}" exec deployment/muninn-minio -- tar -C /data -xzf - < "${backup_dir}/minio-data.tar.gz"
kubectl -n "${MUNINN_NAMESPACE}" exec deployment/muninn-nats -- tar -C /data -xzf - < "${backup_dir}/jetstream-data.tar.gz"

printf 'RESTORED_RECOVERY_POINT_ID=%s\n' "${recovery_point_id}"
