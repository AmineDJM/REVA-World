# Docker / Render readiness check (P7) — 2026-10-15

Measured, not claimed. Host: Docker Engine 29.8.2 (buildx 0.37.1, overlayfs,
cgroup v1), 4 vCPU, 15.7 GiB. Repository at `b0d2d499` plus the uncommitted
working tree of that moment (other agents were editing concurrently). The dev
database `reva` (container `reva-db`) was **not** touched: every runtime check
used a separate throwaway PostGIS container on its own Docker network.

## 1. Build

```bash
docker build --no-cache --progress=plain \
  --secret id=build_ca,src=/root/.ccr/ca-bundle.crt \
  --build-arg REVA_GIT_COMMIT=$(git rev-parse HEAD) -t reva-world:p7check .
```

| Build | Result | Wall time | Image (uncompressed / content) | Context |
|---|---|---|---|---|
| original Dockerfile, `--no-cache` | rc=0 | 92.7 s | 1,497.6 MB / 349 MB | 2.14 MB |
| after fixes (§4), `--no-cache` | rc=0 | 134.6 s | **1,330.9 MB / 315 MB** | 0.56 MB |
| after fixes, warm (only concurrently edited sources changed) | rc=0 | 64.0 s | same | |

Base images were already present locally; timings vary with host load (the
`npm run build` step took 8.5 s in the first build and 35.6 s in the second).
Largest steps (second cold build): `uv sync --frozen --no-dev --no-install-project`
66.1 s (74 packages), image export 48.4 s, UI `tsc --noEmit && vite build` 35.6 s,
`npm ci` 15.2 s (171 packages).

Layers (runtime, `docker history`): base 85 + 9.7 + 54.5 MB; venv copy 989 MB
before / 856 MB after the cleanup; src 2.3 MB; static UI 2.7 MB; contracts 0.5 MB;
registry 0.3 MB; migrations 0.1 MB; compileall layer 3.9 MB. 15 layers total.

Largest installed packages (MB): `_polars_runtime_32` 179, `pyarrow` 167,
`scipy` 109 (+30 libs), `pandas` 72, `statsmodels` 64, `duckdb` 58, `numpy` 43
(+27 libs), `botocore` 31, `sqlalchemy` 28. **`polars` and `duckdb` are declared
in `pyproject.toml` but imported nowhere in `src/reva` or `scripts`** (grep for
`polars|duckdb` → no hits outside thread/memory env names): ~250 MB (~19 % of the
image) removable by the pyproject owner if they are not planned for V1.

### Static checks of the image

| Check | Evidence | Result |
|---|---|---|
| non-root | `Config.User=10001:10001`; `id` → `uid=10001(reva) gid=10001(reva)` | pass |
| no compilers/node/uv in runtime | `command -v gcc cc node npm uv` → none | pass |
| lockfile respected | `uv sync --frozen` in both builder steps; `uv lock --check` → consistent; 75 installed distributions, 0 version mismatches vs `/app/uv.lock` | pass |
| no secrets baked | image `env`: only PYTHON*/PATH/HOME/LANG/GPG_KEY/REVA_GIT_COMMIT; no `.env`, `*.pem` (except CA bundles of certifi/botocore/pip), `*.key` in the filesystem; build CA passed as BuildKit secret mount only | pass |
| read-only app files | `/app` root-owned 755/644; `touch /app/src/x` → Permission denied; writable: `/app/var`, `/tmp` | pass |
| world-writable files | **`/opt/venv/.lock` was 0666** (uv lock file) | fixed (§4) |
| UI built and served | `ui` stage runs `npm ci` + `npm run build`, fails if `index.html` absent; `/app/src/reva/api/static/{index.html,assets/}` present; served at `/` (§2) | pass |
| HEALTHCHECK | none (`Config.Healthcheck=null`) — by design: Render uses `healthCheckPath`, and the worker shares the image | documented |
| `.dockerignore` | build-context probe (`COPY . /ctx` into a scratch image): no `.git`, `.env`, `var/`, `node_modules`, `tests/`, `test-results`, `__pycache__`; but `.grimp_cache/` and `.import_linter_cache/` were in the context, and secret patterns (`*.pem`, `*.key`) were root-anchored only | fixed (§4) |
| dev-only script | `scripts/dev_reset_db.sh` (drops the local DB, hardcodes the local owner password) was shipped in `/app/scripts` | excluded (§4) |

## 2. Runtime against a throwaway PostgreSQL (Render-equivalent environment)

```bash
docker network create reva-p7
docker run -d --name reva-p7-db --network reva-p7 -p 127.0.0.1:55432:5432 \
  -e POSTGRES_DB=reva -e POSTGRES_USER=reva_owner -e POSTGRES_PASSWORD=<throwaway> \
  postgis/postgis:17-3.5@sha256:01a6a70e… \
  -c ssl=on -c ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem \
  -c ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key
# managed-PostgreSQL shape: non-superuser CREATEROLE owner, PostGIS pre-created
psql … -c "CREATE ROLE render_owner LOGIN CREATEROLE PASSWORD '<throwaway>'" \
       -c "CREATE DATABASE reva_r OWNER render_owner"
psql -d reva_r … -c "CREATE EXTENSION postgis"     # → render_owner: rolsuper=f rolcreaterole=t
```

Env files (`api.env`, `worker.env`, throwaway values only) materialise exactly
what `render.yaml` gives each service: the `reva-common` group,
`REVA_PROCESS_ROLE`, DB host/port/name, generated passwords (deliberately with
`/ + @ : ? # % & =` to exercise URL encoding), an Argon2id admin hash generated in
the image, `REVA_PUBLIC_BASE_URL=https://reva-api-p7.onrender.com`,
`REVA_ALLOWED_HOSTS=reva-api-p7.onrender.com`, `PORT=10000`, dummy S3 values
(endpoint `http://reva-p7-s3.invalid:9000` — no S3 was available: Docker Hub
denied `minio/minio`, and moto's server extra is not installed). The API env also
holds `REVA_MIGRATION_DATABASE_URL` (as Render's pre-deploy and API service do);
the worker env has no owner URL, API password, session secret, admin hash or
public URL.

### Pre-deploy (`preDeployCommand`)

```text
$ docker run --rm --network reva-p7 --env-file api.env reva-world:p7check2 python scripts/bootstrap_runtime.py
{"capabilities": {"postgis_default_version": "3.5.2", "server_version_num": 170005}, "duration_seconds": 1.588, "event": "reva.bootstrap", "roles": ["reva_api", "reva_worker", "reva_sim"], "schema_version": "0007_security", "status": "ok"}
boot_rc=0 t=2.53 s
```
Re-run on the same database (second deploy): rc=0, same report (2.96 s).
Resulting roles: `reva_api|reva_worker|reva_sim` → super=f createrole=f
createdb=f bypassrls=f login=t inherit=f. No stderr output, no secrets in stdout.

### API (`dockerCommand: python scripts/start_api.py`), 0.5 CPU / 512 MB

```text
$ docker run -d --name reva-p7-api --network reva-p7 --memory=512m --cpus=0.5 -p 127.0.0.1:18000:10000 --env-file api.env reva-world:p7check2 python scripts/start_api.py
{"argv": [… "uvicorn", "reva.api.app:app", "--host", "0.0.0.0", "--port", "10000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10", "--timeout-graceful-shutdown", "50", "--no-server-header"], "db": "postgresql+psycopg://reva_api:***@reva-p7-db:5432/reva_r?sslmode=require", "env": "production", "process_role": "api", "removed_env": [… "REVA_MIGRATION_DATABASE_URL", "REVA_SIM_DB_PASSWORD", "REVA_WORKER_DB_PASSWORD"], "status": "exec", "threads": "1"}
ready after 6.34 s code=200
GET /health/ready (Host: reva-api-p7.onrender.com)
{"status":"ready","checks":[{"name":"database","ok":true,"detail":null},{"name":"migrations","ok":true,"detail":"0007_security"},{"name":"runtime_role","ok":true,"detail":"role=reva_api schemas=8/8"}]}
```

| Request | Result |
|---|---|
| `/health/live`, `/health/ready` | 200 (configured `healthCheckPath` is `/health/ready`; `/health` itself is 404 by design) |
| `/` | 200 `text/html`, CSP `default-src 'self'…`, HSTS, `X-Frame-Options: DENY`, `nosniff` |
| `/assets/index-*.js` | 200 `text/javascript`, 287,040 B |
| `/world/FRA` (SPA deep link) | 200 `text/html` (index fallback) |
| `POST /v1/session` (admin, throwaway password) | 200, `Secure; HttpOnly; SameSite=strict` session cookie |
| any path with `Host: 10.0.0.5:10000` or `127.0.0.1:18000` | **400 Invalid host header** (TrustedHostMiddleware) |

Uvicorn process environment (`/proc/1/environ` keys): no
`REVA_MIGRATION_DATABASE_URL`, `REVA_WORKER_DB_PASSWORD`, `REVA_SIM_DB_PASSWORD`.
`docker stats`: 86 MiB / 512 MiB. `pg_stat_ssl`: `reva_api | t | TLSv1.3`.
`docker stop -t 60` → 0.6 s, exit 0 (`Application shutdown complete`).

### Worker (`dockerCommand: python scripts/start_worker.py`), 1 CPU / 2 GB

```text
$ docker run -d --name reva-p7-worker --network reva-p7 --memory=2g --cpus=1 --env-file worker.env reva-world:p7check2 python scripts/start_worker.py
{"argv": ["/opt/venv/bin/python", "-m", "reva.operations.worker"], "component": "worker", "env": "production", "event": "reva.start", "process_role": "worker", "schema": {"attempts": 1, "expected_head": "0007_security", "schema_version": "0007_security", "sim_ok": true, "worker_ok": true}, "status": "exec", "stripped_owner_keys": [], "threads": "1"}
{… "msg": "schema ready: 0007_security"}
{… "msg": "worker b9fe582edbda:1:047cd7be started (concurrency=1, lease=120s)"}
$ psql -d reva_r -c "select worker_id, started_at from ops.worker_presence"
b9fe582edbda:1:047cd7be|2026-10-15 00:31:48.167963+00
```
Presence row after ≈ 3 s. Worker process env has none of the API secrets or
`REVA_PUBLIC_BASE_URL` (the https rule applies to the `api` role only, so the
worker starts without it). RSS 70–92 MiB. `pg_stat_ssl`: `reva_worker | t |
TLSv1.3`. `docker stop` → 0.5 s, exit 0, log `worker exited (SIGTERM);
drained=True`.

### Fail-closed checks (original image; start scripts unchanged since)

| Scenario | Output (abridged) | Exit |
|---|---|---|
| API with `REVA_PUBLIC_BASE_URL=http://…` | `REVA_PUBLIC_BASE_URL must be https in production` | 78 |
| API without `REVA_ADMIN_PASSWORD_HASH` | `REVA_ADMIN_PASSWORD_HASH (Argon2id; …)` | 78 |
| worker with `REVA_MIGRATION_DATABASE_URL` | `worker refuses to start: migration-owner credential variable(s) present` | 78 |
| worker command with `REVA_PROCESS_ROLE=api` | `REVA_PROCESS_ROLE=api but this entrypoint starts the worker process` | 78 |
| worker with `REVA_DB_SSLMODE=disable` | `REVA_DB_SSLMODE must be one of require/verify-ca/verify-full in production` | 78 |
| worker against an empty database, deadline 5 s | 2× `waiting` (`relation "public.alembic_version" does not exist`), then `schema/roles not ready after 3 attempts in 5s` | 75 |
| `.env` in the working directory, production | `.env file present in the working directory in production; refusing to start` | 78 |

Additional in-image probe: `boto3.client('s3', endpoint_url='')` →
`ValueError: Invalid endpoint:`; `Settings(reva_s3_endpoint_url='')` keeps `''`.
A blank `REVA_S3_ENDPOINT_URL` therefore breaks the S3 store.

## 3. Blueprint cross-check

```text
$ uv run python scripts/validate_blueprint.py
{"blueprint": ".../render.yaml", "schema": "cached contracts/render.schema.json retrieved 2026-10-14T22:58:21Z", "valid": true, "problems": []}
$ uv run python scripts/validate_blueprint.py --live
{"schema": "https://render.com/schema/render.yaml.json (downloaded)", "valid": true, "problems": []}
$ uv run pytest -q tests/security/test_start_env.py
20 passed in 10.95s
```

* Every setting a role requires (`Settings.required_for`) is declared for that
  service. Undeclared settings fall back to defaults: `PORT` (Render supplies it),
  `REVA_CORS_ORIGINS` (empty), `REVA_HTTP_PROXY_MODE`, `REVA_LOGIN_RATE_LIMIT_PER_MINUTE`,
  `REVA_OBJECT_ROOT` (unused with s3), the LLM/sandbox fields (disabled) and the
  optional `REVA_COMTRADE_API_KEY`.
* Secrets: generated on `reva-api` (`REVA_API/WORKER/SIM_DB_PASSWORD`,
  `REVA_SESSION_SECRET`; Render documents generated values as base64 256-bit, i.e.
  44 chars ≥ 32 and possibly `+/=` — URL encoding verified above); `sync: false`
  for the admin hash, S3 endpoint/region/bucket/keys and contact e-mail on
  `reva-api`, and the worker's own S3 keys. The owner URL comes only from
  `fromDatabase.connectionString` and only on `reva-api`.
* `healthCheckPath: /health/ready` matches the route; plans exist in the schema
  enum (`0.5c-512mb`, `1c-2g`, Postgres `0.1c-256mb`, 5 GB, PostgreSQL 17); all in
  `frankfurt`; one instance each; `maxShutdownDelaySeconds` 60 (API graceful
  shutdown 50 s) / 120 (worker).
* Self-references `fromService reva-api → RENDER_EXTERNAL_URL/HOSTNAME`: Render's
  Blueprint spec documents self-referencing default variables ("provide the
  service's own `name`").
* Internal DB TLS: Render documents that internal connections "optionally support
  TLS using self-signed certificates" and to use `sslmode=require` (verify-ca/full
  unsupported internally). Consistent with the Blueprint, **unverified on Render**.
  The pre-deploy's `connectionString` carries no `sslmode` → libpq `prefer`.
* Health-check Host: Render documents the `onrender.com` subdomain (or a verified
  custom domain) as Host. Matches `REVA_ALLOWED_HOSTS`; a custom domain must be
  added to it first (else 400 → failed health check, verified locally).

No change to `render.yaml` was necessary.

## 4. Fixes made

* `Dockerfile` (builder): after `uv sync --frozen --no-dev`, remove
  `/opt/venv/.lock` (was 0666) and third-party `site-packages/*/tests`
  directories, then fail the build if anything in `/opt/venv` is world-writable.
  Effect: 1,497.6 → 1,330.9 MB (−167 MB), content 349 → 315 MB. Re-verified:
  imports of `reva.api.app`, `reva.operations.worker`, `scipy.stats`,
  `statsmodels.api`, `pyarrow`, `networkx`; no world-writable file outside
  /tmp, /proc, /dev, /run; full pre-deploy → API → worker run above was done on
  this image (`reva-world:p7check2`).
* `.dockerignore`: secret patterns made recursive (`**/.env`, `**/.env.*`,
  `**/*.pem`, `**/*.key`, `**/*.p12`, `**/id_rsa*`, `**/id_ed25519*`); added
  `.grimp_cache/`, `.import_linter_cache/`, coverage outputs, `*.log`, `.claude/`,
  `**/node_modules/`, and `scripts/dev_reset_db.sh`. Context 2.14 MB → 0.56 MB.
* `docs/deployment.md`: replaced the stale "Known blocker" section with the
  verified state, open questions and credential blockers; image measurements;
  `make ui-dev` for the dev server.

## 5. Remaining blockers / requested changes (owners)

Needs the user's credentials (cannot be done here):
1. Render account / API key — Blueprint creation and observing the first deploy
   (internal TLS, owner `CREATEROLE` + `CREATE EXTENSION postgis`, health check).
2. S3-compatible bucket + two scoped key pairs (API read-only, worker
   prefix-scoped write), endpoint and region. S3 was not exercised at all.
3. Admin password → Argon2id hash; provider contact e-mail.

Requested code changes (outside this agent's files):
* storage owner (`src/reva/adapters/objects/s3.py` or `reva/settings.py`): treat
  an empty `REVA_S3_ENDPOINT_URL`/`REVA_S3_REGION` as unset (boto3 rejects `''`).
* lead (`reva/settings.py` `migration_url`): apply `REVA_DB_SSLMODE` to the owner
  URL when it has no `sslmode`, so the pre-deploy cannot fall back to plaintext.
* lead (`pyproject.toml`): drop `polars` and `duckdb` if not needed in V1 (~250 MB).
* API owner (optional): `/health/ready` does not check object storage, so S3
  misconfiguration surfaces only on first use; consider a cheap, cached HEAD on
  the bucket prefix as a readiness detail (non-fatal) or a startup log line.
* Registry seed in production: bootstrap (at the measured commit) provisions
  schema and roles only; `reva seed` is a separate step that needs S3. Being
  changed by another agent (seed as migration owner inside bootstrap) — re-check
  `reva/cli/bootstrap.py` and re-run §2 once it lands.

Cleanup: containers `reva-p7-db/api/worker`, network `reva-p7` and the
`reva-world:p7check` image were removed after the run; `reva-world:p7check2`
(the fixed image) was kept.
