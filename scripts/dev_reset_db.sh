#!/usr/bin/env bash
# Local development only: drop and recreate the local database and object root.
set -euo pipefail
[ "${REVA_ENV:-local}" = "production" ] && { echo "refusing in production"; exit 1; }
PGPASSWORD=local_reva_owner_only psql -h 127.0.0.1 -U reva_owner -d postgres -c "DROP DATABASE IF EXISTS reva WITH (FORCE)" -c "CREATE DATABASE reva"
rm -rf ./var/objects
