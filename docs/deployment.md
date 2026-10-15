# Deployment (Render)

Status: **not deployed.** Everything below is prepared and locally verified
(image build, non-root smoke, pre-deploy bootstrap in the image against a fresh
PostgreSQL 17/PostGIS 3.5 database, Blueprint schema validation). A deployment
needs a Render account, an S3-compatible bucket with two scoped credentials and
an admin password hash, which are external inputs.

## What the Blueprint creates

`render.yaml` (the specification's Blueprint plus one minimal reviewed compatibility
update, below; validated against Render's
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

## Minimal reviewed compatibility update to render.yaml (2026-10-14)

The supplied Blueprint is kept except for three added variables (9 lines; the
provider schema did not change — both versions validate against Render's schema):

| Added | Where | Reason |
|---|---|---|
| `REVA_DB_SSLMODE=require` | `reva-common` | `reva.settings` refuses production configuration unless PostgreSQL connections use TLS (`require`, `verify-ca` or `verify-full`). Without it neither service starts. Render's internal connections must accept TLS; confirm on the first deploy (startup fails closed with a redacted error otherwise). |
| `REVA_PROCESS_ROLE=api` | `reva-api` | enables the per-process production checks (`Settings.required_for("api")`): API password, session secret ≥ 32 chars, Argon2id admin hash, public allowed hosts, and **no owner URL in the runtime process**. `start_api.py` validates the scrubbed child environment, from which the owner URL is removed; `bootstrap_runtime.py` overrides the role to `bootstrap` for the pre-deploy, the only process allowed to hold the owner URL. |
| `REVA_PROCESS_ROLE=worker` | `reva-worker` | without it, production validation expects the API-only session secret, which the worker (correctly) does not have. |

The start scripts set `REVA_PROCESS_ROLE` in the child environment themselves and
refuse a conflicting value (e.g. the worker command started with the API's role),
so the Blueprint values are a declaration that the scripts verify, not the only
source. `scripts/validate_blueprint.py` now requires all three entries; the
unmodified appendix Blueprint fails those checks with explicit messages.

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

**Worker startup in production (R4-02, verified 2026-10-15).** With `render.yaml`
as written the worker does **not** start: `Settings` applies the rule
"`REVA_PUBLIC_BASE_URL` must be https" to every production process, and the
worker (correctly) has no public URL, so the default `http://localhost:8000`
fails validation. The API side passes. Regression test:
`tests/security/test_start_env.py::test_blueprint_as_written_starts_both_processes_in_production`
materialises each service's environment from `render.yaml` and runs the real
start-script preparation; it fails until the rule is limited to the API process
(`reva_process_role in (None, "api")`, change requested in `reva/settings.py`).


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
