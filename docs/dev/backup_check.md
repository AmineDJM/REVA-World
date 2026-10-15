# Backup / restore end-to-end check (2026-10-15)

Goal: show that `scripts/backup.sh` + `scripts/restore.sh` restore a working REVA
instance (database **and** object store) and catch the failures that matter.
Environment: PostgreSQL 17.5 + PostGIS 3.5.2 in the `reva-db` container; host
`pg_dump` is 16.15 (older than the server), so the client tools ran in the
container (`REVA_PG_CONTAINER=reva-db`). age 1.x for encryption. Shared dev
machine; other agents' test suites were running at the same time, so the times
below are noisy upper-bound-ish samples, not benchmarks. The dev database `reva`
was never touched; everything ran on throwaway databases (`reva_bkcheck_src`,
`reva_bkcheck_dst`, dropped afterwards) and throwaway object roots.

## Commands

```bash
# throwaway env: .env for credentials, then override DB name / object root
set -a; . ./.env; set +a
export REVA_ENV=test REVA_PG_CONTAINER=reva-db
BASE="${REVA_MIGRATION_DATABASE_URL%/*}"
docker exec reva-db psql -U reva_owner -d postgres -c "CREATE DATABASE reva_bkcheck_src"

# 1. source: migrate, seed, load captured fixtures, publish a test_fixture release
export REVA_MIGRATION_DATABASE_URL=$BASE/reva_bkcheck_src REVA_DB_NAME=reva_bkcheck_src REVA_OBJECT_ROOT=$SP/objects_src
uv run reva db bootstrap          # 1.2 s, schema 0007_security
uv run reva seed                  # 1.3 s, 31 concepts, 6 entities, 6 models
uv run reva fixtures load         # 4.1 s, WDI 200 obs, Comtrade 282 obs + 202 relations, NE, Crossref
uv run reva beliefs publish --origin test_fixture   # 1.8 s, sequence 1, 482 estimates
uv run reva verify                # ok

# 2. backup (encrypted, objects included)
age-keygen -o $SP/age.key
scripts/backup.sh --out $SP/backups --recipient age1... --include-objects

# 3. restore into a NEW database and a NEW, EMPTY object root
docker exec reva-db psql -U reva_owner -d postgres -c "CREATE DATABASE reva_bkcheck_dst"
unset REVA_MIGRATION_DATABASE_URL
export REVA_RESTORE_DATABASE_URL=$BASE/reva_bkcheck_dst REVA_OBJECT_ROOT=$SP/objects_dst REVA_BACKUP_AGE_IDENTITY=$SP/age.key
scripts/restore.sh $SP/backups/reva-backup-<UTC> --restore-objects --verify-objects

# 4. independent checks on the restored copy
#    per-table count(*) via \gexec on both DBs + diff; alembic/trigger/policy/RLS/CONNECT counts;
#    write probes through each runtime login; release_as_of through reva_api
REVA_MIGRATION_DATABASE_URL=$BASE/reva_bkcheck_dst REVA_DB_NAME=reva_bkcheck_dst uv run reva verify

# 5. refusals
scripts/restore.sh <backup>                                    # target now non-empty
scripts/restore.sh <backup> --force                            # no confirmation
scripts/restore.sh <backup> --force --confirm-database wrong   # wrong name
scripts/restore.sh <backup> --force --confirm-database reva_bkcheck_dst --restore-objects --verify-objects

# 6. secrets: decrypt dump -> pg_restore --file=- (SQL text); decrypt objects tar;
#    grep every file for each secret value from the environment
```

## Results

Data: 56 REVA tables + `alembic_version`, 4,010 rows, 63 artifacts (64 objects:
the extra one is the unregistered quarantine probe written by `reva verify`),
object root 2.0 MB on disk.

| Item | Size |
|---|---|
| `reva.dump` plaintext (custom format, compress 6) / `reva.dump.age` | 767,072 / 767,448 B |
| `objects.tar` plaintext / `objects.tar.age` (64 members) | 1,331,200 / 1,331,720 B |
| `backup.json` (includes logical manifest + security catalog) | 96,459 B |
| `objects.jsonl` | 18,579 B |
| dump rendered as SQL (for the secret scan) | 2,586,426 B |

| Step | Wall time |
|---|---|
| `backup.sh` without objects archive (first run) | 1.15 s (pg_dump 0.44 s, manifest 0.08 s, inventory 0.02 s) |
| `backup.sh --include-objects` | 2.37 s (pg_dump 1.10 s, manifest 0.28 s, inventory 0.05 s, objects tar 0.03 s) |
| `restore.sh --restore-objects --verify-objects` | 4.36 s (decrypt+verify both archives 0.22 s, pg_restore 0.95 s, bootstrap 0.15 s, objects 0.27 s, manifest 0.35 s, artifact + release verification 1.09 s) |
| `restore.sh --force --confirm-database … --restore-objects --verify-objects` | 4.05 s |
| `reva verify` on the restored DB | 1.7 s |
| `pytest tests/integration/test_backup_restore_roundtrip.py` | 20–29 s (setup 7.4 s, test 11.2 s) |
| both backup/restore test files together | 45 s, 4 passed |

Verification on the restored copy:

| Check | Result |
|---|---|
| alembic revision | `0007_security` on both |
| row counts per table (independent `count(*)` diff) | identical, 56 tables, 4,010 rows |
| logical manifest (per-table content digests, artifact list, releases) | identical, `differences: []` |
| security catalog: triggers / RLS tables / policies / functions / grants / extensions | 147 / 5 / 15 / 38 / 285 / 2, identical |
| database ACL | `PUBLIC` without `CONNECT`; `reva_api`, `reva_worker`, `reva_sim` with `CONNECT` |
| runtime roles | log in after bootstrap; worker `UPDATE evidence.observation` → permission denied; worker `DELETE meta.artifact` → permission denied; api `INSERT ops.publication` → permission denied; sim `SELECT ops.api_token` → permission denied; owner `UPDATE belief.release` → "Immutable scientific record" |
| as-of through `reva_api` | release `3f30206d…`, 482 estimates |
| objects in the fresh root | 64 restored; 63/63 artifacts backed by an object hashing to `meta.artifact.sha256` with matching size; 63 locations reported as relocated (see finding 1) |
| sealed release manifests | 1/1 verified (manifest hash + logical digest, row fields, estimates Parquet row count + logical digest, pinned inputs = `belief.input`) |
| `reva verify` (CLI) | `ok: true`, `reva_worker` sees `0007_security` |
| non-empty target | refused ("target database is not empty …"); `--force` without / with a wrong `--confirm-database` refused; with the right name: REVA schemas dropped, restore matches |
| `--force` with `REVA_ENV=production` | refused (tested by function call; the CLI refuses even earlier because `.env` exists in production) |
| secrets | 0 hits for every secret value in the environment (owner/runtime DB passwords, session secret, admin hash, …) in `reva.dump.age`, the decrypted dump as SQL, the decrypted objects tar, `backup.json`, `objects.jsonl`, `SHA256SUMS`; the dump contains no `CREATE ROLE` / `PASSWORD` |

## Findings and fixes (in `scripts/backup_restore.py`)

1. **Restore into a fresh object root failed** (`object store does not contain
   every restored artifact location`). `meta.artifact_location.storage_uri`
   keeps the absolute `file://` root of the source; the checker resolved keys
   relative to the new root. The application reads by (namespace, sha256) and
   never needed the old root, so the check now matches by store key and reports
   `relocated_locations`. Also, objects were not part of the backup at all: only
   an inventory. Added `backup.sh --include-objects` (streamed tar, age-encrypted
   with the dump's recipients, every object re-hashed) and
   `restore.sh --restore-objects` (member names validated against the
   content-addressed layout, put through the store port, which re-hashes and
   never overwrites).
2. **Data-only comparison could not see lost security objects.** The logical
   manifest now includes a normalised security catalog (triggers, RLS flags and
   policies, function security/config/body digest, exploded grants without
   grantor/owner). Backups taken before this change have no catalog and are still
   accepted (the catalog comparison is skipped for them).
3. **Artifact check was location-only.** It now also requires every non-inline
   `meta.artifact` to be backed by a present object of `meta.artifact.byte_size`
   (`unbacked_artifacts`), and `--verify-objects` re-verifies every sealed release.
4. **No explicit override for a non-empty target.** Added
   `--force --confirm-database <name>` (drops only the REVA schemas and
   `alembic_version`, after both archives verified; refused in production). The
   emptiness check now also counts existing REVA schemas, not only tables.
5. **Secret guard.** `backup.json` and `objects.jsonl` are scanned for every
   credential in the environment before `SHA256SUMS` is written; a hit fails the
   backup.

## Roles

Roles are cluster-global and not in the dump. Restore creates any missing
`reva_api`/`reva_worker`/`reva_sim`/`reva_restricted_deletion` as `NOLOGIN`;
bootstrap (migration role with `SUPERUSER` or `CREATEROLE`) sets the passwords and
`LOGIN`. Object-level grants come back from the dump and the database-level
grants are re-applied by restore; both are part of the compared catalog. Dropping
the runtime roles to prove recreation was **not** exercised here: the cluster is
shared with the dev database and other agents' test databases.

## Not covered

S3 backend (logic shared with the filesystem path through the store port, but not
run here); production-size volumes; restore across PostgreSQL major versions.
