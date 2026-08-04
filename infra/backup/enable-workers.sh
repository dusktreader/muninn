#!/usr/bin/env bash
set -euo pipefail

recovery_point_id="${1:?RECOVERY_POINT_ID is required}"
: "${MUNINN_NAMESPACE:=muninn}"

kubectl -n "${MUNINN_NAMESPACE}" scale deployment/muninn-api deployment/muninn-job-manager deployment/muninn-worker --replicas=1
printf 'WORKERS_ENABLED_RECOVERY_POINT_ID=%s\n' "${recovery_point_id}"
