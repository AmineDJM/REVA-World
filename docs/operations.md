# Operations

## Targets

| Target | Value | Basis |
|---|---|---|
| RPO | ≤ 24 h | nightly encrypted logical export + managed DB recovery (verify the plan's capabilities) |
| RTO | ≤ 4 h | restore into a new database + bootstrap + verification, measured below |
| Restore drill | monthly, clean environment | `scripts/restore.sh` into a new empty database |
| Object inventory | weekly | `scripts/backup_restore.py inventory` / `backup.sh` |

Measured locally (PostgreSQL 17.5 / PostGIS 3.5 in Docker, MVP fixture data:
55–57 tables, 4,166–4,222 rows, 62 objects, 0.57–0.61 MB custom-format dump), from
`tests/integration/test_backup_restore.py`, six runs on 2026-10-14 on a shared
development machine with other test suites running concurrently:

| Step | Wall time |
|---|---|
| `backup.sh` end to end (snapshot, logical manifest, `pg_dump`, inventory with sha256 of every object, age encryption) | 1.18–2.60 s |
| of which `pg_dump` | 0.37–0.86 s |
| `restore.sh` end to end (checksum, decrypt, `pg_restore`, grants, bootstrap, manifest re-computation, object check) | 1.73–3.42 s |
| of which `pg_restore` | 0.84–1.46 s |
| of which bootstrap | 0.04–0.09 s |

These are small-data numbers; they do not extrapolate to production volumes.
Re-measure in each restore drill and record the result here.

## Backup

```bash
# Owner credential used read-only inside a REPEATABLE READ snapshot.
export REVA_MIGRATION_DATABASE_URL=...           # Render: the reva-db external connection string
export REVA_BACKUP_AGE_RECIPIENT=age1...         # REQUIRED when REVA_ENV=production
export REVA_OBJECT_BACKEND=s3 REVA_S3_BUCKET=... REVA_S3_ACCESS_KEY_ID=... REVA_S3_SECRET_ACCESS_KEY=...
scripts/backup.sh --out /secure/backups          # or: make backup BACKUP_DIR=...
scripts/backup.sh --out /secure/backups --include-objects   # also archive every object (filesystem store / fresh bucket)
```

What it does (`scripts/backup_restore.py backup`):

1. opens a `REPEATABLE READ READ ONLY` transaction and exports its snapshot;
2. computes the **logical manifest** in that snapshot: per table, row count and
   sha256 over sorted per-row sha256 of the row text (fixed UTC/ISO/hex session
   settings), the artifact sha256 list digest and the release list
   (id, manifest sha256, cutoff, publication sequence);
3. runs `pg_dump --format=custom --snapshot=<id>`, i.e. the dump contains exactly
   the manifested state; the password is passed as `PGPASSWORD`, never on argv.
   With encryption, pg_dump's stdout is **streamed straight into `age --encrypt`**
   (`scripts/backup_restore.py:stream_dump`): the plaintext exists only in pipe
   buffers and is never written to disk; its sha256 and size are computed in the
   stream and recorded. `public.spatial_ref_sys` data is excluded (PostGIS
   recreates it; REVA uses EPSG:4326 only and adds no custom SRIDs);
4. inventories the object store (key, sha256 of the bytes, size) and checks every
   `meta.artifact_location` exists with matching address/size — any missing,
   mismatched or corrupt object fails the backup;
5. with `--include-objects`, streams every inventoried object into
   `objects.tar` (`objects.tar.age` when encrypting; same recipients, never a
   plaintext file), re-hashing each object against its address while streaming —
   needed to rebuild a filesystem store or a fresh bucket from the backup alone;
6. writes `backup.json` (archive and plaintext sha256, sizes, versions, timings,
   logical manifest), `objects.jsonl` and **`SHA256SUMS`** (`sha256sum -c`
   format) over the archive(s), the inventory and `backup.json`, all mode 0600 in a
   0700 directory. Before `SHA256SUMS` is written, `backup.json` and
   `objects.jsonl` (plaintext even for encrypted backups) are scanned for every
   credential in the environment (DB URL passwords, `*PASSWORD*`, `*SECRET*`,
   `*TOKEN*`, `*API_KEY*`, `*ACCESS_KEY*`); a hit fails the backup.

The logical manifest also records the **security catalog** of the REVA schemas:
append-only/immutability triggers, RLS flags and policies, functions
(`SECURITY DEFINER`, `search_path`, body digest) and grants on schemas, tables,
columns, sequences, functions and the database, normalised to
(object, grantee, privilege) with grantor and the owner's implicit entry dropped
(restore uses `--no-owner`). A restore that lost a trigger, policy or grant fails
the comparison.

Secrets: the dump contains no roles or passwords (roles are cluster-global and
not dumped; checked by rendering the decrypted dump as SQL). `ops.api_token`
stores only SHA-256 digests of API tokens.

Integrity and authenticity: `age` encrypts with authenticated
ChaCha20-Poly1305 chunks, so a modified ciphertext fails decryption even if an
attacker rewrites every checksum (tested). `SHA256SUMS` detects accidental or
naive modification without the key. For origin authenticity (who made the
backup), sign the checksum file on the backup host with a key kept elsewhere,
e.g. `minisign -S -s backup.key -m SHA256SUMS`, and verify before restoring with
`minisign -V -p backup.pub -m SHA256SUMS`; this signing step is operational and
not automated by the scripts.

Ownership semantics: the dump keeps owners and ACLs; restore uses `--no-owner`
so the restoring migration owner owns everything (what bootstrap verifies),
while table/function grants to the runtime roles are restored.

Store backups **outside the production account** (separate cloud account or
offline media); keep the age identity separately from the backups. Objects are
protected by bucket versioning/replication to a second account (`rclone sync`
or provider replication); the inventory proves what must exist.

When the host `pg_dump` is older than the server, set `REVA_PG_CONTAINER=<name>`
(tools run via `docker exec`) or `REVA_PG_BINDIR=/usr/lib/postgresql/17/bin`.

## Restore

```bash
createdb ... reva_restore                         # a NEW, EMPTY database
export REVA_RESTORE_DATABASE_URL=postgresql://owner:...@host:5432/reva_restore
export REVA_API_DB_PASSWORD=... REVA_WORKER_DB_PASSWORD=... REVA_SIM_DB_PASSWORD=...
export REVA_BACKUP_AGE_IDENTITY=/secure/age.key
scripts/restore.sh /secure/backups/reva-backup-<UTC> --verify-objects
# from a backup taken with --include-objects, into a fresh object root/bucket:
scripts/restore.sh /secure/backups/reva-backup-<UTC> --restore-objects --verify-objects
```

Steps: verify `SHA256SUMS` (missing or mismatching → refuse) → verify the archive
sha256 recorded in `backup.json` → **pass 1**: decrypt to nowhere, which
authenticates the ciphertext and checks the plaintext sha256 (refuse on any
failure; the target is not touched) → refuse a non-empty target → ensure the
runtime roles exist (`NOLOGIN`) → **pass 2**: `age --decrypt` streamed directly
into `pg_restore --no-owner --no-comments --single-transaction --exit-on-error`
(no plaintext file; the plaintext digest is re-checked) → re-apply database-level
grants (`REVOKE ALL … FROM PUBLIC`, `GRANT CONNECT` to the three runtime roles) →
bootstrap (role passwords, ownership/deletion-role verification) → with
`--restore-objects`, put every object of the archive into the *configured* store
(member names validated against the content-addressed layout, bytes re-hashed,
existing identical objects reused) → recompute the logical manifest (data and
security catalog) and compare with the one in `backup.json` (any difference exits
1) → with `--verify-objects`: every non-inline `meta.artifact` is backed by an
object whose bytes hash to its sha256 with the recorded size, and every sealed
belief release re-verifies (manifest bytes hash to `manifest_sha256` and to the
recorded logical digest; origin/method/cutoff/ontology and model digests equal the
release row; the estimates Parquet is the registered artifact with the pinned row
count and logical digest; pinned inputs equal `belief.input`).

`meta.artifact_location.storage_uri` is immutable history (a filesystem location
keeps its original absolute root). The application reads objects by
(namespace, sha256) from the configured store, so the check matches by key and
reports `relocated_locations` instead of failing on a fresh root.

Non-empty targets are refused. `--force --confirm-database <target-name>` drops
the target's REVA schemas and `alembic_version` (after both archives verified)
and restores into it; `--force` is refused when `REVA_ENV=production`.

Restore does not need a superuser: the restoring role only has to own the empty
target database (`--no-owner` makes it the owner of every restored object), and
PostGIS must be creatable by it or pre-created by an administrator. The test
restores as a `NOSUPERUSER` database owner and checks identical manifests, as-of
answers and object ownership; the result reports `restored_as_superuser`.
Bootstrap after such a restore needs a migration role with `SUPERUSER` or
`CREATEROLE` (it only sets the runtime roles' passwords/`LOGIN`; it no longer sets
privileged role attributes) — otherwise use `--skip-bootstrap` and let the next
pre-deploy run it.

Roles are cluster-global: the role passwords supplied to restore are applied to
the cluster. Use the target environment's values (on Render: the API service's
generated values) or `--skip-bootstrap` and let the next deploy's pre-deploy set
them. To cut over, point `reva-db` at the restored database (or restore into the
Render database after recreating it empty) and redeploy.

`tests/integration/test_backup_restore.py` performs this end to end on every CI
run: ingest fixtures, publish two releases, encrypted backup, restore into a new
database, identical logical manifests, identical as-of answers at three cutoffs
through the `reva_api` login, immutability triggers and grants still enforced,
`PUBLIC` without `CONNECT`. `tests/integration/test_backup_restore_roundtrip.py`
restores into a fresh database **and a fresh, empty object root** from a backup
taken with `--include-objects`, checks artifacts, sealed releases, `reva verify`,
the non-empty/`--force` rules and that no credential appears in any backup file;
it skips with the reason when `pg_dump`/`pg_restore` ≥ the server version are not
available. Commands and measured results: `docs/dev/backup_check.md`.

## Alerts (§20.3)

| Alert | Condition | Source |
|---|---|---|
| Interactive queue age | oldest queued interactive job > 15 min | `ops.job` / `/metrics` |
| Source late | > 2 expected cadences late (cadence-aware; annual sources are not "stale" after a day) | `ops.connector_health`, schedules |
| Worker missing | no worker heartbeat > 5 min | job heartbeats / worker log |
| DB disk | > 75 % of plan disk | Render metrics |
| Prohibited write | any permission-denied or `Immutable scientific record` error from a runtime role | PostgreSQL log, `ops.audit` |
| Conservation failure | any simulation conservation/numerical check failure | `sim.run` status, worker log |
| Budget | > 80 % of daily request/byte or monthly LLM budget | `ops.budget_ledger` |
| Backup | no successful backup in 26 h; any restore-drill mismatch | backup job log |

Logs are JSON lines with request/job/activity/release/run correlation ids;
secrets and unbounded identifiers never appear in metric labels.

## Garbage collection

Orphan objects (uploaded but never committed) are collected by the `gc` job
(`reva.operations.gc`): two-pass reference scan, minimum age 7 days, audited
deletions confined to the configured prefix. Published history is never
collected. Run inventory before and after large GC passes.

## Kill switch and degradation

* `REVA_AUTONOMY_KILL_SWITCH=true` (env group `reva-common`) blocks every autonomy
  action and discovery job submission; `REVA_AUTONOMY_MODE=disabled` disables the
  subsystem. Redeploy (or restart the services) to apply.
* `REVA_LLM_ENABLED=false` / budget 0 is the default; enabling requires a positive
  monthly budget (validated at startup).
* A failing connector is quarantined; the last accepted release stays readable and
  visibly stale; no fabricated replacement is published.

## Upgrades and pinned versions

* Migrations run only in the API pre-deploy under the advisory lock; runtime
  never performs DDL. Migrations are expand-compatible with the previous release;
  the worker tolerates a newer schema head and waits for an older one.
* Base images are pinned by digest in the `Dockerfile` and `compose.yaml`; CI
  actions by commit SHA. To refresh: `docker pull <tag>`,
  `docker inspect --format '{{index .RepoDigests 0}}' <tag>`, update the digest
  and comment, rebuild, run CI. For actions: `git ls-remote
  https://github.com/<owner>/<action> refs/tags/<tag>`.
* The Render schema cache is refreshed with
  `uv run python scripts/validate_blueprint.py --refresh` (review the diff;
  `live-smoke.yml` also validates against the live schema daily).

## Schema contract

Until release, migrations are edited in place under the same revision ids, so
`alembic_version` cannot tell whether a database really has the current schema.
`migrations/schema_contract.json` holds a fingerprint of every catalog object the
application relies on (schemas, tables and columns, constraints, indexes,
triggers, functions with a hash of their bodies, RLS policies, grants, runtime
role attributes). It is generated from a fresh bootstrap of a throwaway database.

* Check a database (read-only, exit 1 on drift, lists the differing objects):
  `uv run reva db verify-schema` (`--role worker|api` to check as a runtime role).
* After any change to `migrations/versions/*.py` or
  `src/reva/adapters/postgres/ddl.py`, regenerate it and commit it with the
  migration: `uv run python scripts/update_schema_contract.py`
  (`--check` exits 1 when the committed contract is stale). This creates and drops
  a `reva_contract_<random>` database; the database in the URL is not touched.
  `tests/integration/test_schema_contract.py` fails while the contract is stale.
* A drifted database (for example a dev database migrated by an earlier draft of
  the migrations) is not repaired: recreate it from the migrations.
* Startup guard mode `REVA_SCHEMA_CONTRACT=enforce|warn|off`: the default is
  `enforce`; `warn` applies under `REVA_ENV=test`; production always enforces.
