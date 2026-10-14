#!/usr/bin/env bash
# Nightly logical backup of the REVA database + object-store sha256 inventory.
#
#   scripts/backup.sh [--out DIR] [--no-objects] [--recipient AGE_PUBKEY] [--recipients-file FILE]
#
# Environment:
#   REVA_MIGRATION_DATABASE_URL  owner credential used read-only (REPEATABLE READ snapshot)
#   REVA_BACKUP_DIR              default output directory (./var/backups)
#   REVA_BACKUP_AGE_RECIPIENT    age public key; REQUIRED in production (REVA_ENV=production),
#   REVA_BACKUP_AGE_RECIPIENTS_FILE   ... or a recipients file. Optional locally.
#   REVA_PG_CONTAINER            run pg_dump inside this container (client older than server)
#   REVA_PG_BINDIR               directory holding pg_dump/pg_restore >= server major
#   REVA_OBJECT_BACKEND/REVA_OBJECT_ROOT/REVA_S3_*   object store to inventory
#
# Output: DIR/reva-backup-<UTC>/ {reva.dump[.age], objects.jsonl, backup.json} (mode 0700/0600).
# Copy the directory to storage in a separate account (see docs/operations.md).
# Secrets are read from the environment only and never printed.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${REVA_PYTHON:-python3}"
if [[ -z "${REVA_PYTHON:-}" ]] && command -v uv >/dev/null 2>&1 && [[ -f "$here/../uv.lock" ]]; then
  exec uv run --project "$here/.." --frozen python "$here/backup_restore.py" backup "$@"
fi
exec "$py" "$here/backup_restore.py" backup "$@"
