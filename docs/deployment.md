# Deployment (Render)

Status: **not deployed.** Everything below is prepared and locally verified
(image build, pre-deploy bootstrap, API and worker started from the image in
production mode against a throwaway TLS PostgreSQL 17/PostGIS 3.5 database,
Blueprint schema validation; see `docs/dev/docker_check.md`). A deployment
needs a Render account, an S3-compatible bucket with two scoped credentials and
an admin password hash, which are external inputs.

## Blockers that need the owner's credentials

Nothing below can be done or verified from the repository:

1. **Render account / API key** — to create the Blueprint (dashboard or
   `render blueprints` CLI) and observe the first deploy (internal TLS, owner
   `CREATEROLE`/PostGIS, health check passing).
2. **S3-compatible bucket and two scoped key pairs** (read-only for `reva-api`,
   prefix-scoped write for `reva-worker`), plus endpoint and region. Object storage
   was not exercised by any container check.
3. **Admin password** — its Argon2id hash (`uv run reva auth hash-password`).
4. **Contact e-mail** for provider user agents (`REVA_HTTP_CONTACT_EMAIL`).
5. Optional: `REVA_COMTRADE_API_KEY`; a custom domain (then extend
   `REVA_ALLOWED_HOSTS`, see below).

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
   * `REVA_S3_BUCKET`, `REVA_S3_REGION`, `REVA_S3_ENDPOINT_URL` — set on
     `reva-api`; the worker references them. Do not leave the endpoint blank: an
     empty value fails in boto3; for AWS give the regional endpoint
     (`https://s3.<region>.amazonaws.com`).
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

## Verified in a container (2026-10-15) — no Render account involved

Measured with the production image against a throwaway PostgreSQL 17.5/PostGIS
3.5.2 container with TLS on, using the environment `render.yaml` gives each
service (commands and raw output: `docs/dev/docker_check.md`):

* The pre-deploy (`python scripts/bootstrap_runtime.py`) succeeds as a
  **non-superuser `CREATEROLE` owner** (the managed-PostgreSQL shape), applies
  migrations to head `0007_security`, provisions `reva_api`/`reva_worker`/`reva_sim`
  without SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS, and is idempotent on re-run
  (≈ 2–4 s each). The former blockers are fixed in code: the https
  public-URL rule applies only to the `api` process role (`reva/settings.py`), and
  bootstrap no longer sets privileged role attributes (it needs only `rolsuper OR
  rolcreaterole`; `reva/cli/bootstrap.py`, migration `0007`).
* `start_api.py` with `REVA_ENV=production`, `REVA_PROCESS_ROLE=api` starts
  Uvicorn on `$PORT` under a 0.5 CPU / 512 MB limit; `/health/ready` returned 200
  ≈ 6–7 s after container start (database, migrations, runtime role
  `reva_api` 8/8 schemas). RSS ≈ 86 MiB. The owner URL and worker/sim passwords are
  absent from the Uvicorn process environment. The built UI is served at `/`
  (SPA deep links fall back to `index.html`; hashed assets are immutable-cached).
* `start_worker.py` with `REVA_PROCESS_ROLE=worker` and **no**
  `REVA_PUBLIC_BASE_URL`, session secret or admin hash starts, verifies the schema
  head with both runtime logins and registers a row in `ops.worker_presence` within
  ≈ 3 s. RSS ≈ 70–90 MiB under a 1 CPU / 2 GB limit. SIGTERM drains (`drained=True`,
  exit 0).
* All runtime connections were TLS 1.3 (`pg_stat_ssl`) with `REVA_DB_SSLMODE=require`.
* Fail-closed checks (exit 78 unless noted): http public URL on the API; missing
  admin hash; owner URL on the worker; worker command with `REVA_PROCESS_ROLE=api`;
  `REVA_DB_SSLMODE=disable`; a `.env` file in production; worker before bootstrap
  → bounded retries, then exit 75.

## Open questions only a real deploy can settle

* **Internal TLS.** Render's documentation states internal connections
  "optionally support TLS using self-signed certificates" and that
  `sslmode=require` is the way to refuse plaintext (verify-ca/verify-full are not
  supported internally). This is consistent with `REVA_DB_SSLMODE=require`, but it
  is **unverified on Render**; the services fail closed with a redacted error if it
  does not hold. The pre-deploy's owner URL is Render's `connectionString` without
  `sslmode`, so libpq's default (`prefer`) applies to the migration connection only.
* **Owner capabilities.** Whether Render's database user has `CREATEROLE` and may
  `CREATE EXTENSION postgis` is not documented explicitly (Render lists PostGIS as
  supported and says to enable extensions from a psql session). Bootstrap checks
  both and reports a redacted error if not.
* **Health-check Host header.** `TrustedHostMiddleware` rejects any Host not in
  `REVA_ALLOWED_HOSTS` with 400 (verified: Host `10.0.0.5:10000` → 400). Render's
  documentation says HTTP health checks use the service's `onrender.com` subdomain,
  or a verified custom domain when one exists. `REVA_ALLOWED_HOSTS` is
  `RENDER_EXTERNAL_HOSTNAME`, so a **custom domain must be added to
  `REVA_ALLOWED_HOSTS`** (and `REVA_PUBLIC_BASE_URL`) before it is verified, or
  health checks fail.
* **Object storage.** `/health/ready` does not touch S3, so a wrong bucket or key
  surfaces only on the first object read/write; S3 was not exercised in the
  container check (no bucket available). Test the access-denied cases below.
* **Blank S3 endpoint.** An empty `REVA_S3_ENDPOINT_URL` is passed to boto3,
  which raises `ValueError: Invalid endpoint` (verified in the image). For AWS, set
  the regional endpoint explicitly (e.g. `https://s3.eu-central-1.amazonaws.com`)
  rather than leaving the prompt blank.
* **Registry seed.** Bootstrap currently provisions schema and roles only; the
  registry (concepts, entities, sources, schedules) is seeded by a separate step.
  Check `reva/cli/bootstrap.py` for whether seeding has moved into the pre-deploy;
  otherwise run `reva seed --profile mvp` once from a Render shell before the first
  ingestion.

## Image

`Dockerfile`: `ui` stage (Node 22, `npm ci && npm run build`, requires
`ui/package-lock.json`), `builder` (uv 0.11.32, `uv sync --frozen --no-dev`;
removes uv's world-writable `/opt/venv/.lock` and third-party `tests/`
directories), `runtime` (python:3.12-slim-bookworm, uid/gid 10001, no compilers,
node or uv, root-owned read-only application files, writable `/app/var` only).
Base images are pinned by digest (see the Dockerfile comments). Behind a
TLS-intercepting proxy, pass the proxy CA as a BuildKit secret:
`docker build --secret id=build_ca,src=ca.crt .` (it is never written to a layer).
Render ignores the optional secret.

Measured 2026-10-15 (4 vCPU build host; details in `docs/dev/docker_check.md`):
uncompressed image **1.33 GB** (content size 315 MB; before the cleanup 1.50 GB /
349 MB), 15 layers, of which the virtual environment layer is 856 MB. Cold build
(`--no-cache`, base images present) 93–135 s; the dependency layer and image
export dominate. The image has no Docker `HEALTHCHECK`: Render uses
`healthCheckPath: /health/ready`, and the same image runs the worker.
`polars` and `duckdb` (~250 MB installed) are declared dependencies that no module
in `src/reva` imports.

## Local equivalent

```bash
make env up sync bootstrap seed fixtures   # PostGIS in Docker, API/worker on the host
make api        # terminal 1
make worker     # terminal 2
make ui-dev     # terminal 3 (Vite dev server; `make ui` = npm ci + typecheck + vitest + build)
make image smoke
make ci         # offline CI equivalent (see `make help` for e2e, live-check, format-check)
```


## Object storage (S3) — verified against MinIO

Verified against a real MinIO server with these exact policies (commands, policy JSON and results in
[docs/dev/s3_check.md](dev/s3_check.md)); not yet verified on AWS itself (403-vs-404 on prefix-limited
listing, 409 conflicts, versioning/object lock, throttling).

* API key: `s3:GetObject` on `<bucket>/reva/{raw,normalised,belief,models,scenarios,evaluation}/*` and
  `s3:ListBucket` on `<bucket>` conditioned on `StringLike s3:prefix reva/*`.
* Worker key: `s3:GetObject,PutObject,DeleteObject` on `<bucket>/reva/*` (keep the trailing slash:
  `reva*` would also match `revax/`) plus the same conditioned `ListBucket`.
* Backup key: a dedicated read-only key (`s3:GetObject` on `reva/*` plus conditioned `ListBucket`) — the
  API key cannot read `quarantine/`, which backups include. Restore needs `PutObject`.
* A blank `REVA_S3_ENDPOINT_URL`/`REVA_S3_REGION` is treated as unset (AWS default endpoint); an
  `http://` endpoint is refused in production. TLS verification is on; for a private CA set
  `AWS_CA_BUNDLE` (it replaces the default trust store). `REVA_S3_ADDRESSING_STYLE` is
  `auto` (default; path-style for custom endpoints), `path` or `virtual`.
* Uploads are a single PUT: one object is limited to 5 GiB.
* `/health/ready` reports an `object_store` check; it is informational and does not make the API
  not-ready.
