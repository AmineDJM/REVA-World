#!/usr/bin/env bash
# Restore a REVA backup into a NEW, EMPTY database and verify it.
#
#   scripts/restore.sh BACKUP_DIR [--identity AGE_IDENTITY_FILE] [--skip-bootstrap] [--verify-objects]
#
# Environment:
#   REVA_RESTORE_DATABASE_URL    owner credential of the empty target database (must exist)
#   REVA_API_DB_PASSWORD, REVA_WORKER_DB_PASSWORD, REVA_SIM_DB_PASSWORD
#                                runtime role passwords re-applied by bootstrap. Roles are
#                                cluster-global: use the values of the environment that will
#                                serve from this cluster, or pass --skip-bootstrap.
#   REVA_BACKUP_AGE_IDENTITY     age identity file for encrypted dumps
#   REVA_PG_CONTAINER / REVA_PG_BINDIR   as for backup.sh
#
# Steps: checksum -> decrypt -> refuse non-empty target -> ensure runtime roles ->
# pg_restore --no-owner --single-transaction -> database-level grants -> bootstrap ->
# logical-manifest comparison with the manifest recorded at backup time (exit 1 on any difference).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${REVA_PYTHON:-python3}"
if [[ -z "${REVA_PYTHON:-}" ]] && command -v uv >/dev/null 2>&1 && [[ -f "$here/../uv.lock" ]]; then
  exec uv run --project "$here/.." --frozen python "$here/backup_restore.py" restore "$@"
fi
exec "$py" "$here/backup_restore.py" restore "$@"
