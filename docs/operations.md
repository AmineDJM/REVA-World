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

## Promotion gate (`reva gate`)

Autonomous connector promotion is decided by a separate process, the independent
promotion gate, which logs in as `reva_gate`. That login is the only one that may
record gate decisions (`ops.record_gate_result`), and the approval functions accept a
gate pass only if `reva_gate` recorded it. The worker files a gate request
(`ops.gate_request`) and waits; it cannot decide for itself. See
`docs/dev/gate_separation.md` for the design and its residual risks.

**Locally**, run the gate in its own terminal next to the worker:

```sh
make worker          # uv run reva worker
make gate            # uv run reva gate     (or: uv run reva gate --once)
```

`reva gate` needs `REVA_GATE_DB_PASSWORD` (in `.env`, see `.env.example`) and refuses
any login other than `reva_gate`. `reva verify` checks that the gate login connects and
holds exactly its expected privileges. If `REVA_GATE_DB_PASSWORD` is not configured,
it skips that check with a note outside production, and fails in production. Locally
the separation is a credential convention, not an isolation boundary: the same OS user
and the same `.env` hold every password. On Render, `reva-gate` is its own service (see
`docs/deployment.md`).

**`gate_pending`.** A discovery step waits up to `REVA_GATE_WAIT_SECONDS` (default
120 s) for the gate's decision. If none arrives, for example because the gate is not
running, the step reports `status: gate_pending` and promotes nothing. The request
stays queued, and the gate decides it once it runs. Then the `gate_followup` worker
job (schedule `gate_followup:promotion`, every 5 minutes) promotes each late pass:

- it reads the stored proposal and manifest, so no provider data is fetched again;
- it uses the same checked path (`promote_connector` → `evidence.approve_eligibility`);
- it is idempotent: one gate pass is spent on at most one approval, and each pass
  gets at most one recorded outcome (`ops.outbox` event `autonomy.gate_followup`);
- it is fenced by the job lease.

Passes older than 7 days, or refused (policy changed, content rolled back, binding
mismatch), are not retried. Re-run discovery in that case. Nothing is ever promoted
without a recorded `reva_gate` pass.

**Rollback.** `rollback_promotion` retires the version and restores the newest earlier
version in its supersession chain. It skips versions that were themselves rolled back,
and restores only through `evidence.restore_eligibility`, so the restored version must
have had an independent approval. Rolling back a version that is no longer the
eligible one restores nothing.

## Kill switch and degradation

* `REVA_AUTONOMY_KILL_SWITCH=true` (env group `reva-common`) blocks every autonomy
  action and discovery job submission; `REVA_AUTONOMY_MODE=disabled` disables the
  subsystem. Redeploy (or restart the services) to apply. With
  `REVA_AUTONOMY_KILL_FILE` set, creating that file (`touch <file>`) blocks autonomy in
  the running processes without a restart, and removing the file re-enables it.
  * Under the kill switch the gate records no decisions: requests stay pending.
  * `gate_followup` promotes nothing and records nothing, so its candidates stay.
  * Promoted template connectors are not loaded.
  * The switch is re-read before every step and inside the promotion transaction.
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

## V3 link store (`links_materialise`)

The materialised link store (`kg.link_materialisation` / `kg.link_row`, migration 0012) is rebuilt by the
worker job kind `links_materialise` (`reva.links.jobs.handle_links_materialise` →
`reva.links.store.materialise`; allowed through `ops.submit_job` since migration 0013_links_ops). The seeded
schedule `links_materialise:live` runs daily, its first slot two hours after `atlas_snapshot:live`, with
payload `{"atlas_origin": "live", "kg_origin": "live", "top_k": 10, "skip_unchanged": true}` — production
serves only materialisations built from a live snapshot and the live KG. With `skip_unchanged` (default) the
job resolves its inputs first (latest atlas snapshot, KG watermark = highest `kg.load` id, taxonomy sha256,
method version, top-k) and records nothing when a materialisation of exactly those inputs exists
(`{"reused": true, ...}`); the store is append-only (~1.1 KB per link, ~400 MB for a full planet run), so
old materialisations are kept until a retention policy is decided. Manual run (worker credentials):

```bash
uv run reva links materialise --atlas-origin live --kg-origin live [--top-k 10] [--atlas-snapshot SHA] [--no-atlas]
```

(alias of `uv run python -m reva.links.store materialise …`).

## V3 end-to-end stack (`--v3-fixtures`)

The V3 screens (Neurones, Chemin, Frise, Atlas, Dossier pays, Demander) are checked against the
real `/v1/kg`, `/v1/links`, `/v1/atlas`, `/v1/ask` and `/v1/locations` routes by
`ui/e2e/v3_live_stack.spec.ts` (no route interception):

```bash
uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v3_live_stack.spec.ts
uv run python scripts/e2e_stack.py --v3-fixtures            # serve only (Ctrl-C tears down)
```

On the disposable `reva_e2e_<random>` database (never the dev database `reva`), after the usual
base fixtures and the `test_fixture` belief release, `--v3-fixtures`:

* loads the real-Wikidata links fixture (`tests/unit/links/fixtures/wikidata_links_fixture.json`,
  76 entities / 544 statements) into `kg.*` through `reva.kg.model.load_batch`, source key
  `wikidata`, origin `test_fixture` (the allow-listed properties are registered first);
* replays the atlas sources of `V3_ATLAS_SOURCES` in fixture mode
  (`reva.atlas.ingest.run_atlas_ingest(..., mode="fixture")`, recorded responses, never the network,
  never charged): IMF, ILOSTAT, Eurostat minimum wages, OECD earnings, WHO GHO, UNESCO UIS, UNDP HDR,
  WHR, OWID, Natural Earth 10m (a derived excerpt of the real layers: Ireland+UK, Hispaniola, Cyprus,
  Saint-Martin; rows flagged `ne_excerpt`), GeoNames country info, World Bank, FAO.
  `--v3-atlas-source NAME` (repeatable) picks others. A failing source is recorded with its error under
  `v3.atlas` in the stack file and the stack still starts; the spec requires at least 8 completed sources;
  The link-bearing sources of `V3_LINK_ATLAS_SOURCES` are replayed too (UN DESA migrant stock, UNGA votes,
  CShapes, UCDP, BACI, UN Treaty Collection);
* records a `test_fixture` atlas snapshot (`reva.atlas.snapshot.take_snapshot`);
* materialises the link store of that snapshot + the fixture KG (`reva.links.store.materialise`, top-k 10;
  ~18k links: KG kinds, IMTS/BACI trade, migrant stock, CShapes, treaty co-parties) — reported under
  `v3.links` in the stack file (a failure is recorded, never hidden), so `/v1/links/pair` and the
  "Pays ↔ Pays" screen have pair links (Brésil–Portugal: KG kinds, treaty co-parties, predecessor states;
  Algérie–France: trade both ways, migrant stocks, CShapes colonial rule);
* (all modes) bootstraps as the owner, then confirms the seed as `reva_worker` in the same process: both
  use one registry snapshot (`reva.cli.seed.registry_snapshot`), so concurrent edits under `registry/` no
  longer make the confirmation fail; a failed bootstrap attempt is logged, and a refusal names both
  fingerprints;
* keeps `POST /v1/ask` on the deterministic keyword planner (no LLM planner is wired, whatever
  `.env` says).

`v3_live_stack.spec.ts` skips itself unless the stack file has the `v3` section; UI3's
`v3_planet.spec.ts` (fixtures served by route interception) still runs standalone or on any stack.
The modes combine (`--v2-fixtures --recorded-drafter --v3-fixtures`); without `--v3-fixtures` the
stack is unchanged.
