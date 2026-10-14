# Operations

## Targets

| Target | Value | Basis |
|---|---|---|
| RPO | ≤ 24 h | nightly encrypted logical export + managed DB recovery (verify the plan's capabilities) |
| RTO | ≤ 4 h | restore into a new database + bootstrap + verification, measured below |
| Restore drill | monthly, clean environment | `scripts/restore.sh` into a new empty database |
| Object inventory | weekly | `scripts/backup_restore.py inventory` / `backup.sh` |

Measured locally (PostgreSQL 17.5 / PostGIS 3.5 in Docker, MVP fixture data:
55 tables, 4,166 rows, 62 objects, 0.57 MB custom-format dump), from
`tests/integration/test_backup_restore.py` on 2026-10-14:

| Step | Wall time |
|---|---|
| `backup.sh` end to end (snapshot, logical manifest, `pg_dump`, inventory with sha256 of every object, age encryption) | 1.21–1.31 s |
| of which `pg_dump` | 0.37–0.38 s |
| `restore.sh` end to end (checksum, decrypt, `pg_restore`, grants, bootstrap, manifest re-computation, object check) | 1.73–1.78 s |
| of which `pg_restore` | 0.84–0.86 s |
| of which bootstrap | 0.04–0.05 s |

These are small-data numbers; they do not extrapolate to production volumes.
Re-measure in each restore drill and record the result here.

## Backup

```bash
# Owner credential used read-only inside a REPEATABLE READ snapshot.
export REVA_MIGRATION_DATABASE_URL=...           # Render: the reva-db external connection string
export REVA_BACKUP_AGE_RECIPIENT=age1...         # REQUIRED when REVA_ENV=production
export REVA_OBJECT_BACKEND=s3 REVA_S3_BUCKET=... REVA_S3_ACCESS_KEY_ID=... REVA_S3_SECRET_ACCESS_KEY=...
scripts/backup.sh --out /secure/backups          # or: make backup BACKUP_DIR=...
```

What it does (`scripts/backup_restore.py backup`):

1. opens a `REPEATABLE READ READ ONLY` transaction and exports its snapshot;
2. computes the **logical manifest** in that snapshot: per table, row count and
   sha256 over sorted per-row sha256 of the row text (fixed UTC/ISO/hex session
   settings), the artifact sha256 list digest and the release list
   (id, manifest sha256, cutoff, publication sequence);
3. runs `pg_dump --format=custom --snapshot=<id>`, i.e. the dump contains exactly
   the manifested state; the password is passed as `PGPASSWORD`, never on argv;
4. inventories the object store (key, sha256 of the bytes, size) and checks every
   `meta.artifact_location` exists with matching address/size — any missing,
   mismatched or corrupt object fails the backup;
5. encrypts the dump with `age` (plaintext deleted) and writes `backup.json`
   (dump sha256, sizes, versions, timings, logical manifest) and `objects.jsonl`,
   all with mode 0600 in a 0700 directory.

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
```

Steps: verify dump sha256 → decrypt → refuse a non-empty target → ensure the
runtime roles exist (`NOLOGIN`) → `pg_restore --no-owner --no-comments
--single-transaction --exit-on-error` → re-apply database-level grants
(`REVOKE ALL … FROM PUBLIC`, `GRANT CONNECT` to the three runtime roles) →
bootstrap (role passwords, ownership/deletion-role verification) → recompute the
logical manifest and compare with the one in `backup.json` (any difference exits
1) → optionally confirm every artifact location exists in the object store.

Roles are cluster-global: the role passwords supplied to restore are applied to
the cluster. Use the target environment's values (on Render: the API service's
generated values) or `--skip-bootstrap` and let the next deploy's pre-deploy set
them. To cut over, point `reva-db` at the restored database (or restore into the
Render database after recreating it empty) and redeploy.

`tests/integration/test_backup_restore.py` performs this end to end on every CI
run: ingest fixtures, publish two releases, encrypted backup, restore into a new
database, identical logical manifests, identical as-of answers at three cutoffs
through the `reva_api` login, immutability triggers and grants still enforced,
`PUBLIC` without `CONNECT`.

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
