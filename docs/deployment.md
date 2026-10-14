# Deployment (Render)

Status: **not deployed.** Everything below is prepared and locally verified
(image build, non-root smoke, pre-deploy bootstrap in the image against a fresh
PostgreSQL 17/PostGIS 3.5 database, Blueprint schema validation). A deployment
needs a Render account, an S3-compatible bucket with two scoped credentials and
an admin password hash, which are external inputs.

## What the Blueprint creates

`render.yaml` (the specification's Blueprint, unchanged; validated against Render's
official schema — `make blueprint`, or `render blueprints validate render.yaml`
with the Render CLI):

| Resource | Plan | Notes |
|---|---|---|
| `reva-db` PostgreSQL 17 | `0.1c-256mb`, 5 GB, Frankfurt | `ipAllowList: []` (private network only) |
| `reva-api` web (Docker) | `0.5c-512mb` | pre-deploy `python scripts/bootstrap_runtime.py`; start `python scripts/start_api.py`; health `/health/ready`; `autoDeployTrigger: checksPass` |
| `reva-worker` background worker (Docker) | `1c-2g` | start `python scripts/start_worker.py`; never receives the owner URL |
| env group `reva-common` | — | production mode, s3 backend, budgets, thread variables = 1 |

Generated on `reva-api` (`generateValue: true`): `REVA_API_DB_PASSWORD`,
`REVA_WORKER_DB_PASSWORD`, `REVA_SIM_DB_PASSWORD`, `REVA_SESSION_SECRET`. The worker
references the worker/sim passwords from the API service. `REVA_MIGRATION_DATABASE_URL`
is the database `connectionString` and exists only on `reva-api`; `start_api.py`
removes it (and the worker/sim passwords) before Uvicorn starts.

## Exact provisioning inputs (external to the Blueprint)

1. **S3-compatible private bucket** (any provider; versioning recommended, public
   access blocked) and its endpoint/region:
   * `REVA_S3_BUCKET`, `REVA_S3_REGION`, `REVA_S3_ENDPOINT_URL` (omit the endpoint
     for AWS S3) — set on `reva-api`; the worker references them.
2. **API credential (read-only)** — set as `REVA_S3_ACCESS_KEY_ID` /
   `REVA_S3_SECRET_ACCESS_KEY` on **reva-api**. Policy: `s3:GetObject` and
   `s3:ListBucket` limited to prefix `reva/` (`raw/ normalised/ belief/ models/
   scenarios/ evaluation/`); no `PutObject`, `DeleteObject` or bucket-admin actions.
3. **Worker credential (prefix-scoped write)** — set as `REVA_S3_ACCESS_KEY_ID` /
   `REVA_S3_SECRET_ACCESS_KEY` on **reva-worker** (its own values, not a reference):
   `s3:GetObject`, `s3:PutObject` (conditional writes), `s3:ListBucket` and
   `s3:DeleteObject` on `reva/*` only; no bucket policy/ACL/lifecycle/versioning
   rights. `DeleteObject` is needed by the orphan GC (`reva.operations.gc`: only
   objects unreferenced by any committed metadata, older than 7 days, after a
   second reference scan; published history is never collected). Because
   content-addressed keys are final keys, the delete right cannot be narrowed to a
   staging prefix; mitigate with **bucket versioning and a noncurrent-version
   retention of at least 30 days** (or object lock in governance mode), managed by
   a separate admin credential that REVA never holds.
   Verify before go-live: `PutObject` and `DeleteObject` with the API key must fail
   with AccessDenied; any write outside `reva/` with the worker key must fail.
4. **Admin password hash** — on a trusted machine:
   `uv run reva auth hash-password` (prompts twice, prints an Argon2id hash).
   Paste into `REVA_ADMIN_PASSWORD_HASH` on `reva-api`. The API refuses to start in
   production unless the value starts with `$argon2id$`.
5. **Contact e-mail** for provider user agents: `REVA_HTTP_CONTACT_EMAIL` on
   `reva-api` (the worker references it). The worker refuses to start in
   production without it.
6. Optional: `REVA_COMTRADE_API_KEY` (worker only; the no-key bounded preview
   works without it).

## First deploy order

1. Create the bucket and both credentials; test the access-denied cases above.
2. Generate the admin hash.
3. Render → New → Blueprint → select the repository; fill every `sync: false`
   value when prompted (items 1–5).
4. Render creates `reva-db`, builds the image once per service and runs the API
   **pre-deploy**: advisory lock → PostgreSQL ≥ 17 / PostGIS / CREATEROLE checks →
   Alembic upgrade → runtime role passwords → ownership verification. Its stdout is
   one JSON line with `schema_version` and no secrets. A failure aborts the deploy.
5. The worker may start before the pre-deploy has finished: `start_worker.py`
   probes with the worker and simulation logins (schema head, queue function
   privileges) with exponential backoff up to
   `REVA_WORKER_STARTUP_DEADLINE_SECONDS` (default 300 s), then exits with status 75
   and a clear message; Render restarts it.
6. Check `/health/ready`, log in, and let the worker's source-aware scheduler run
   the first live acquisitions (or run `reva ingest --profile mvp --live` from a
   Render shell on the worker service); then submit the demo scenario.

## Known blocker to resolve before the first Render deploy

Managed PostgreSQL owners are `CREATEROLE` but not superusers. PostgreSQL 16+
refuses `ALTER ROLE … NOSUPERUSER/NOCREATEDB/NOBYPASSRLS` from such a role (we
reproduced this on PostgreSQL 17.5). `reva.cli.bootstrap.provision_roles` and the
"role exists" branch of migration `0007` currently issue those clauses, so the
first pre-deploy can fail closed. The requested fix: alter only `LOGIN NOINHERIT
CONNECTION LIMIT … PASSWORD …` and verify the dangerous attributes are false from
`pg_roles`. Whether Render's owner has `CREATEROLE` and may `CREATE EXTENSION
postgis` must also be confirmed on the first deploy; bootstrap checks both and
reports a redacted error if not.

## Image

`Dockerfile`: `ui` stage (Node 22, `npm ci && npm run build`, requires
`ui/package-lock.json`), `builder` (uv 0.11.32, `uv sync --frozen --no-dev`),
`runtime` (python:3.12-slim-bookworm, uid 10001, no compilers, read-only
application files, writable `/app/var` only). Base images are pinned by digest
(see the Dockerfile comments). Behind a TLS-intercepting proxy, pass the proxy CA
as a BuildKit secret: `docker build --secret id=build_ca,src=ca.crt .` (it is never
written to a layer). Render ignores the optional secret.

## Local equivalent

```bash
make env up sync bootstrap seed fixtures   # PostGIS in Docker, API/worker on the host
make api        # terminal 1
make worker     # terminal 2
make ui         # terminal 3 (Vite dev server)
make image smoke
```
