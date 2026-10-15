# CI check (GitHub Actions vs. local), 2026-10-15

I could not run GitHub Actions from here. I checked the workflows statically and ran the
local equivalents of every offline job. Files: `.github/workflows/{ci,live-smoke,release}.yml`,
`Makefile`, `pyproject.toml` (`[tool.ruff]` exclude, `e2e` marker text).

## Static validation

- `actionlint 1.7.12`, with `shellcheck` and `pyflakes` on PATH, reports **0 findings** on all three
  workflows. Before the fix it reported one real bug: `ci.yml` used `${{ job.services.postgres.id }}`
  in job-level `env:`, where the `job` context is not available. `REVA_PG_CONTAINER` therefore came
  out empty, and the backup/restore test would have been **skipped**, because the runner's
  pg_dump 16 cannot dump PostgreSQL 17. The variable is now set on the two steps that need it.
- Action pins: I re-resolved all 9 SHAs with `git ls-remote` and each one matches its tag comment.
  The tags are checkout v7.0.1, setup-python v7.0.0, setup-uv v10.3.0, upload-artifact v7.0.2,
  setup-node v7.1.0, setup-buildx v4.4.1, login v4.6.0, metadata v6.2.0 and build-push v7.4.0.
  PostGIS is pinned to `postgis/postgis:17-3.5@sha256:01a6a70e…`, the same digest as `compose.yaml`.
- Permissions: the top-level `permissions` is `contents: read` in all three workflows. Only
  `release.yml` adds `packages: write`, and only for its job, so that it can push to GHCR on tags.
  Every checkout uses `persist-credentials: false`. No workflow uses `pull_request_target`. Values
  from `inputs`, `github.*` and `steps.*` reach shell scripts through `env:` and are never
  interpolated into the script text.
- Concurrency:
  - `ci`: one group per PR or ref. Superseded PR runs are cancelled. Runs on main are never
    cancelled, because Render's `autoDeployTrigger: checksPass` waits on them.
  - `live-smoke`: a single group, never cancelled.
  - `release`: one group per ref, never cancelled.
- Every job has a `timeout-minutes` (15–45).

## ci.yml (pull_request, push to main, manual)

| Job | What it runs | Local equivalent |
|---|---|---|
| static | `uv sync --frozen --all-extras`, `ruff check .`, ruff format (**advisory**), mypy strict on kernel + ports, import-linter, contracts, cached Blueprint | `make lint typecheck registry blueprint` (+ `make format-check`) |
| tests | PostGIS 17 service, `.env.example` → `.env`, apt `age`, 0006→head upgrade through `scripts/bootstrap_runtime.py`, `pytest -m "not live and not perf"` | `make test` |
| ui | `npm ci`, typecheck, vitest, build, `npm audit --omit=dev`, licence scan | `make ui` (`make ui-check` without `npm ci`) |
| e2e | PostGIS service, `npx playwright install --with-deps chromium` into `$RUNNER_TEMP/pw-browsers`, `scripts/e2e_stack.py --run` | `make e2e` (preinstalled `/opt/pw-browsers`) |
| audit | pip-audit on the exported runtime lock, pip-licenses | — |
| image | `docker build`, non-root smoke with `--network none` | `make image smoke` |

The DB environment matches what the tests expect:

- The service uses `POSTGRES_USER=reva_owner`, `POSTGRES_PASSWORD=local_reva_owner_only` and
  `POSTGRES_DB=reva`. These are the values in `.env.example`'s `REVA_MIGRATION_DATABASE_URL`.
- `tests/conftest.py` reads `.env`. It creates a fresh `reva_test_<hex>` database, then bootstraps
  and seeds it.
- The runtime role passwords come from `.env.example`.
- `REVA_ROLE_CONNECTION_LIMIT` is left unset. That gives -1 (unlimited) outside production, as
  defined in `reva.cli.bootstrap.role_connection_limit`.
- The superuser is needed for `CREATE EXTENSION postgis` and role DDL.

Changes I made to `ci.yml`:

- **Playwright browsers.** `scripts/e2e_stack.py` sets `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`
  as a default, which matches the dev container. On a runner, the browsers that
  `playwright install` puts in `~/.cache/ms-playwright` would not have been found. The job now
  exports `PLAYWRIGHT_BROWSERS_PATH` before installing, and the script keeps that value through
  `setdefault`. Failure screenshots are uploaded.
- **Network guard.**
  - The pytest and e2e steps run with `HTTP(S)_PROXY`/`ALL_PROXY=http://127.0.0.1:9`,
    `NO_PROXY=127.0.0.1,localhost` and `uv run --offline`. A provider request that is accidentally
    left unmarked fails instead of quietly going live. The connectors' default local egress mode
    is `env`, so they honour the proxy.
  - Live tests are `@pytest.mark.live`: `tests/live/test_live_sources.py` and one test in
    `tests/integration/test_eurostat_discovery.py`. CI deselects them explicitly with
    `-m "not live and not perf"`, the same as the `addopts` in `pyproject.toml`.
- **`ruff format --check` is advisory** (`continue-on-error`). `uv run ruff format --check src tests`
  reports **147 files would be reformatted, 122 already formatted**; over the whole repo it is
  153 of 325. I reformatted nothing. To make the check blocking, run `make format` once (the
  owners need to agree, because it touches every agent's files) and then delete `continue-on-error`.
- `e2e` and `image` no longer have `needs: static`, so each job reports its own result.
- The upgrade step now also asserts that `alembic current` equals `alembic heads` after bootstrap.
- JUnit output uses `junit_family=xunit1`. The backup test calls `record_property`, which xunit2
  does not support (pytest warned about it).
- The `tests/e2e` fixture demo (`-m e2e`, offline, origin `test_fixture`) already runs inside the
  `tests` job. It is not deselected, and it took about 17 s locally.

## live-smoke.yml (schedule 05:17 UTC daily + manual)

- `scripts/live_source_checks.py` runs into a fresh `reva_live_<hex>` database on the service
  container. A gate step then fails the run if any gate is not `true`/`pass`, and it appends
  `var/live/live_checks.md` to the job summary.
- After that come `pytest -m live` (it can be turned off on manual runs) and the live Render
  schema drift check. The JSON/MD/JUnit evidence is uploaded for 30 days.
- Manual inputs: `connectors` (space-separated) and `skip_eurostat`. They reach the script only
  through `env:`.
- **No secrets are required.** World Bank, Natural Earth and Crossref are keyless, and Comtrade
  uses the keyless public endpoint.
  - The optional `vars.REVA_HTTP_CONTACT_EMAIL` sets the Crossref `mailto`.
  - The optional `secrets.REVA_COMTRADE_API_KEY` is exposed only to the two steps that make live
    requests.
  - Note: no connector reads `reva_comtrade_api_key` at present. The key is reserved.
- It never runs on push or PR, so provider outages cannot block merges or Render deploys.

## release.yml (tag `v*.*.*` + manual)

- On a tag, the workflow first validates the Blueprint against the cached schema. It then builds
  with buildx and pushes to GHCR with an SBOM and `provenance: mode=max`. Finally it smoke-tests
  the pushed digest with `--network none`.
- A manual run builds and smoke-tests a locally loaded image. It skips the login and pushes
  nothing; attestations are off because the docker exporter cannot carry them.
- **It never deploys to Render and holds no Render credentials.** Deployment stays with the owner:
  - Render's Git integration builds after the owner has created the Blueprint in their own
    account (`autoDeployTrigger: checksPass` on main).
  - Or the owner deploys manually from the Render dashboard.

## Makefile

- `lint`: ruff check + import-linter. `format-check` is a separate target.
- `test`: `pytest -q -m "$(PYTEST_MARKERS)"`, with `PYTEST_MARKERS` defaulting to
  `not live and not perf`.
- `ui`: `ui-ci` (`npm ci`) followed by `ui-check`, which runs typecheck, vitest and a build into
  `var/ui-build`, so `src/reva/api/static` is left alone.
- `e2e`: `scripts/e2e_stack.py --run`. `PLAYWRIGHT_BROWSERS_PATH` defaults to `/opt/pw-browsers`.
- `live-check`: `scripts/live_source_checks.py`.
- `ci`: `check ui-check e2e`.
- The Vite dev server moved from `ui` to **`ui-dev`**.

## Local results (2026-10-15, dev container; dev DB `reva` never touched)

| Command | Result |
|---|---|
| `actionlint` (+shellcheck, pyflakes) | 0 findings |
| `make lint` | **fails, 4 findings**, all in `scripts/backup_restore.py`, which is being edited in flight (E501 ×3 at lines 397/776/815, S112 at 425). The other lint step, import-linter, passes with 5 contracts kept. `contracts/autonomy_interfaces.py`, a verbatim spec copy, is now excluded from ruff. |
| `make typecheck` | pass (mypy: no issues in 13 files) |
| `make registry`, `make blueprint` | pass (offline, cached schema) |
| `pytest -q -m "not live and not perf"` (= `make test`) behind the black-hole proxy, `--offline` | **1672 passed, 6 skipped, 14 deselected in 338 s**. The skips are parametrised cases that do not apply, not missing infrastructure. |
| CI upgrade step (0006 → bootstrap → head) on a throwaway DB | pass (`0006_operations` → `0007_security` = heads) |
| `make ui-check` | pass: typecheck, 8 files / 50 vitest tests, build |
| `make e2e` behind the black-hole proxy | **34 Playwright tests passed** (83 s total) |
| `make live-check`, `make ui` (`npm ci`), `make image` | not run here: they need the network or Docker builds |

## Open items for owners

1. Fix the 4 ruff findings in `scripts/backup_restore.py` (agent I). Until that is done, the
   `static` job is red.
2. `docs/deployment.md:137` says `make ui` starts the Vite dev server. It should now say
   `make ui-dev` (agent I owns docs/).
3. Decide whether to run `make format` across the tree so the format check can become blocking.
4. Optional: block non-localhost requests in the Playwright specs with `page.route`, for defence in
   depth (agent F). The UI has no external URLs today.
