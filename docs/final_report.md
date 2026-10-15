# REVA World — completion report (implementation prompt §14)

Date: 2026-10-15 (UTC). Branch: `claude/kind-darwin-kyy8re`. Every number below comes from a run made on the
final code in this session; where something was not done or could not be verified, it says so.

## 1. What was implemented and how to run it

**V1 / MVP (P0–P7).** A versioned federation of evidence, beliefs and models with the crude-oil border-value
exposure stress test as the first use case:

* Kernel (typed values and missing values, uncertainty kinds, time extents, canonical hashing `reva-cjson-2`),
  ontology registry, append-only PostgreSQL 17 schema (migrations 0001–0008), provenance to raw bytes,
  filesystem/S3 object store, safe HTTP client with allowlists and budgets.
* Connectors: World Bank WDI, UN Comtrade (exact and batch), Natural Earth (display only), Crossref,
  Eurostat (autonomy template, demography, labour/pensions), Eurostat FIGARO IO tables. Live responses are
  archived content-addressed; drift is quarantined; self-healing proposals pause publication of affected
  concepts.
* Beliefs: source-selected releases sealed with their manifests, release-pinned relations, `release_as_of`
  replay, typed gaps (Algeria stays missing, never zero).
* Simulation: plugin platform; crude exposure L0 (cache), L1 (fixed quantities), L2 (labelled elasticity
  sensitivity range, no probability level).
* Forecasting tournament (DM/HLN + Holm, locked holdout), synthetic population recipe-2, autonomy T0–T3 with an
  independent gate process (`reva_gate` login), API (FastAPI) with sessions, scoped tokens, CSRF, rate limits,
  React UI.

**V2 (approved by the owner on 2026-10-15; `docs/roadmap/v2_design.md`)**, all models *experimental,
maintainer review pending*:

1. Crude scenario for every country with data (`countries: ["ALL"]`, `skip_missing_countries`).
2. Natural-language → draft typed scenario (OpenAI `gpt-4.1` through the proxy; server validation and
   cross-checks; nothing runs before the user confirms).
3. Cohort-component population projection (31 EU/EFTA countries) and multi-region input-output price model
   (FIGARO 2024).
4. Retirement-age model (France law in force incl. LFSS 2026; Germany; Netherlands to 2025; others refused with
   typed gaps), household margins.
5. Back-test API, V2 UI (world map with typed gaps, mirror view, composer, multi-period run views, IO heatmap,
   command palette).

**How to run locally** (details in `README.md`, `docs/operations.md`):

```bash
make up                         # PostgreSQL 17 + PostGIS (compose)
uv run reva db bootstrap        # migrations, runtime roles, registry seed (owner)
uv run reva ingest --live       # or --fixtures
uv run reva beliefs publish --origin live
uv run reva worker & uv run reva gate & uv run reva api
uv run reva demo --fidelity L2  # +25 % crude border-price stress test
make e2e                        # disposable stack + Playwright
```

## 2. Git summary and migrations

* 146 commits on `claude/kind-darwin-kyy8re` (2026-10-14/15); 744 files changed against the initial commit
  (+247,956 / −467 lines); 212 Python modules in `src/`, 153 test modules, 85 UI source files.
* Migrations: `0001_kernel` … `0007_security`, `0008_v2`. 0001–0007 were edited in place before release (the
  pre-release policy); a schema contract (`migrations/schema_contract.json`, fingerprint `be8cadc331511a1b`,
  1296 objects) detects any database that drifted from them, and the API, worker, gate and bootstrap refuse to
  start against one in production. The local dev database `reva` was rebuilt from the migrations for this run.

## 3. Actual test results (final code)

| Check | Result |
|---|---|
| `uv run pytest -m "not live and not perf"` | **2,439 passed, 0 failed**, 30 skipped, 21 deselected (23 min) |
| `uv run pytest -m live` (real provider requests) | see §4 |
| `ruff check .` / `ruff format --check .` / `lint-imports` / `mypy -p reva.kernel -p reva.ports` | all pass |
| UI typecheck / vitest / build | pass / 144 tests in 22 files / pass |
| UI Playwright (`scripts/e2e_stack.py --run --recorded-drafter --v2-fixtures`) | **53 passed**, 1 skipped (applies only when drafting is disabled) |
| Live acceptance workflow (`scripts/demo_workflow.py --live`) | **13/13 steps pass** (`docs/dev/final_run/demo_live_summary.json`) |
| Docker build (`reva-world:final`) | builds in 89 s; 1.34 GB uncompressed; runs as uid 10001 |

Headline live results on the rebuilt dev database (release #1, 482 estimates, cutoff 2026-10-15 03:13 UTC),
identical to the independent recomputation in `docs/dev/numeric_audit.md`:

| Country | L1 exposure, % of 2023 GDP | L2 sensitivity range |
|---|---|---|
| France | 0.2576 | 0.1213 – 0.2576 |
| China | 0.4609 | 0.2166 – 0.4609 |
| United States | 0.0497 | −0.0623 – 0.0497 |
| Algeria | refused: `missing_input` (no Comtrade rows 2020–2023) | — |

The live acceptance run also showed 65 release-pinned trade relations and a T1 promotion of the Eurostat
connector decided by the separate gate process.

## 4. Live source checks and missing coverage

* Live ingest (dev DB): World Bank 5 acquisitions / 200 observations, Comtrade 48 / 282, Natural Earth 1.
* All-countries crude scenario (live run by agent ALLC): 2023 — 85 countries computed, 144 skipped with typed
  reasons (62 report no crude rows at all, e.g. RUS, VEN, LBY, DZA, TUR; 34 no exports, e.g. IND, JPN; 14 no
  imports, e.g. SAU, NGA, MEX). Iraq's exports are reported under HS edition H2 and stay ineligible until a
  human reviews that mapping.
* Population: 31 EU/EFTA countries; retirement: FRA, DEU, NLD (to 2025); ITA, BEL, POL, SWE refused (no
  archivable official rule text); ESP refused by its own data.
* Live-marked test run: PLACEHOLDER — filled in below once the run in progress completes.

## 5. Deployment readiness and credential blockers

**Not deployed.** Ready: `render.yaml` (validated, API + worker + gate + pre-deploy bootstrap), Docker image,
start scripts that fail closed, backup/restore proven (filesystem and MinIO S3), S3 credential split proven on
MinIO. Blocked on credentials only the owner can provide:

1. Render account / API key allowed to create paid resources.
2. A private S3-compatible bucket and **three** scoped key pairs (API read-only, worker prefix-write, gate
   prefix-scoped), plus a dedicated read-only backup key.
3. The admin password (to generate `REVA_ADMIN_PASSWORD_HASH`) and a contact e-mail for provider requests.

Unverified until a real deploy: TLS on Render's internal database connection, AWS-specific S3 behaviour
(403 vs 404, 409, object lock), performance on Render hardware, minisign release signing.

## 6. Cost and resource measurements (local, `docs/dev/final_run/perf_results.json`)

| Measure | Result | Target |
|---|---|---|
| L1 four-country calculation | 2.9 ms | < 2 s |
| Ordinary API p95, 5 concurrent users, 1 CPU / 0.5 CPU (emulated) | 147 ms / 281 ms | < 1 s |
| Cached metadata p95 (1 CPU / 0.5 CPU) | 32 ms / 80 ms | < 500 ms |
| Queue acknowledgement p95 | 249 ms | < 1 s |
| 100,000 synthetic records | 1.6 s, 183 MiB peak | < 60 s, < 1 GiB |
| FIGARO ingest / IO L2 simulation (agent IO) | 77.5 s, 371 MB / 26.3 s, 1,055 MB | worker 2 GB |
| NL draft (gpt-4.1) | 1.4–3.7 s, ~7.5–11k tokens per draft | 200 req and 2M tokens per day cap |

Measured on the session container, not on Render plans.

## 7. Scientific limits (summary; full text in `docs/limitations.md`)

* Outputs are assumption-conditioned stress tests and conditional projections, never forecasts, GDP losses or
  inflation forecasts. Sensitivity ranges carry no probability; the crude L2 range is one-sided (L1 is its
  upper end).
* Missing data stays missing (Algeria, 144 countries in 2023); partner-reported mirror values are shown as a
  separate lineage and never fill a country.
* Cohort projection: constant mortality; census-break years are excluded from residual migration with a typed
  warning; a constant-growth benchmark beats the model on totals in 18 of 31 countries in the 2013→2023
  back-test (the model beats no-change in 23 of 31).
* IO model: first-round cost push, full pass-through on a θ grid, basic prices (a purchasers'-price reading is
  reported separately); the shocked industry B "Mining and quarrying" is broader than crude oil.
* Retirement model: the 2010-reform back-test fits within ~0.5–1.2 pp, but the fit partly comes from two errors
  that cancel; the employment response is transferred from foreign studies; outputs are labelled qualified.
* NL drafts can still map a request to a valid but different scenario; server cross-checks are lexical; the
  user's confirmation is the last defence. The Gemini fallback is not verified live.
* Governance: approvals are enforced in the database and the gate runs as its own login and process; the worker
  still chooses what the gate evaluates, and local separation is only a convention (shared `.env`).

## 8. Phase / gate ledger

Full ledger: `docs/implementation_status.md`. P0–P7: done except the credential-blocked deployment rows; P8–P12:
interface-only or not started (nothing claimed). V2 steps 1–5: done as experimental models pending maintainer
review. Independent reviews and their fixes: `docs/dev/review_*.md`, `docs/dev/review_science_v2.md`,
`docs/dev/numeric_audit.md`.
