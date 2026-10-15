# Test triage (agent T)

Round 2 — 2026-10-14 23:53–00:03 UTC (HEAD `4f604bb`-era working tree, in-flight edits by several agents)

## Commands and counts

| Check | Command | Result |
|---|---|---|
| pytest (offline) | `uv run pytest tests -q -p no:cacheprovider --ignore=tests/e2e -m "not live and not perf" -rfE --maxfail=50` | **3 failed**, 1411 passed, 3 xfailed, 14 deselected, 0 errors (391 s) |
| ruff | `uv run ruff check src tests scripts` | **11 errors** (6 auto-fixable) |
| import-linter | `uv run lint-imports` | 5 kept, 0 broken |
| mypy (kernel strict) | `uv run mypy` | pass (9 files) |
| UI typecheck | `npm --prefix ui run typecheck` | pass |
| UI tests | `npm --prefix ui run test -- --run` | 7 files / 41 tests pass |

No connection-limit errors this round (`pg_stat_activity` before the run: reva_worker 4/10, reva_api 4/10, reva_sim 1/10).

## Outstanding failures (all deterministic)

| Test | Owner | Root cause | Exact suggested fix |
|---|---|---|---|
| `tests/integration/test_revisions.py::test_two_revisions_yield_different_as_of_answers` | **FX2** (intended change in `belief/selection.py`; test/fixture owner H1 inactive) | Since ef1d09d `ELIGIBLE_SQL` passes the support year to `evidence.is_eligible(..., ref_year)` (R-07 year-scoped eligibility). Seeded WDI eligibility is `years: [2000, 2024]` (`registry/mappings/worldbank.yaml`), but vintage fixtures use 1950–1999 (`pop_fra_1990_1991_two_vintages`). The vintage observations are therefore ineligible: in isolation `publish_release` raises `ValidationProblem: no eligible observations at this cutoff`; in the full run the release publishes (other tests' rows) but has no output for `wb:SP.POP.TOTL:FRA:1991` → `AssertionError: ('wb:SP.POP.TOTL:FRA:1991', [])`. | In `tests/fixtures/vintages/vintage_loader.py` (`ingest_vintage`, once per connector version), insert an additional **year-scoped** test eligibility decision for the WDI POP mapping, e.g. via `record_mapping_and_eligibility(conn, {**m, "applicability": {"subjects": ["FRA"], "years": [1950, 1999]}}, act.id, policy_version="test-vintages")` for the `wb.SP.POP.TOTL` mapping (note: the existing `have` short-circuit keys on `policy_version`, so the distinct policy version is required). `is_eligible` filters decisions by scope before "latest wins", so 2000–2024 rows used by other tests are unaffected. |
| `tests/replay/test_replay_job.py::test_operational_replay_job_writes_evaluation_context_and_manifest` | **FX2** (same cause; H1 inactive) | Vintage `pop_fra_1960_1961_job` rows (`wb:SP.POP.TOTL:FRA:1960/1961`) are not eligible → missing from release inputs (`Extra items in the left set`). | Same vintage-loader fix (years range covering 1950–1999). |
| `tests/integration/test_autonomy_contracts.py::test_model_evaluator_proper_metrics` | **lead** (autonomy test, agent G inactive; behaviour change came from `src/reva/models/metrics.py`) | New `MIN_SCALE_POINTS = 3` in `models/metrics.py` (5d76cc6, R3 science review: MASE scale undefined with < 3 training points). The test's training window at origin 2001 is `[10, 12]` (2 points) → `mase_scale` returns `value=None` → `mase is None`, test expects 1.5. Behaviour is correct; test fixture is too short. | In the test, prepend a year to the target series: `{"series": {"a": [[1999, 8], [2000, 10], [2001, 12], [2002, 14], [2003, 16]]}}`. Training = `[8, 10, 12]`, scale 2; errors 2 and 4 → MAE 3.0, MASE 1.5, coverage 1.0 — all existing assertions unchanged. |

## Environment / flaky / order-dependent

| Test(s) | Classification | Status |
|---|---|---|
| `test_queue_invalidation.py` (4 setup errors), `test_evidence_fixes.py::test_r03_release_waits_for_inflight_ingest`, `test_queue_reliability.py::test_concurrent_claims_never_return_the_same_job` | Environment: `FATAL: too many connections for role "reva_worker"` (runtime roles `CONNECTION LIMIT 10`, cluster-wide, shared by every agent's concurrent `reva_test_*` session; one suite run alone holds up to 6 worker connections: session `db.worker` pool 2 + module `FreshDB` worker pool 4) | Round 1 only; passed in round 2 and in isolation. **Still latent.** Suggested: **FX1** make the limit configurable in bootstrap (e.g. `REVA_DB_ROLE_CONNECTION_LIMIT`, 10 in production, ≥40 when `REVA_ENV` is local/test); **lead** (conftest) / FreshDB fixtures: smaller pools (`pool_size=1` session worker, `2` for FreshDB). |
| `test_evidence_fixes.py::test_r03_future_cutoff_rejected_and_none_means_db_now`, `::test_r04_live_ingest_identical_to_fixture_creates_live_lineage` | Order-dependent cascade: `LockNotAvailable` on `ops.dirty_node` after `test_r03_release_waits_for_inflight_ingest` failed mid-way with its in-flight ingest still holding the row lock | Round 1 only. Suggested (**FX2**, test file owner): wrap r03's in-flight ingest thread in `try/finally` (rollback + join) so a failure cannot leak a held lock. |

Leftover test databases: ~76 `reva_test_*` databases on the cluster (few with sessions); sessions killed mid-run leak them.

## Lint / type failures by owner

| Owner | Location | Issue | Fix |
|---|---|---|---|
| FX1 | `src/reva/cli/bootstrap.py:210` | RUF100 unused `noqa: BLE001` | drop directive |
| FX1 | `src/reva/cli/bootstrap.py:216` | RUF100 unused `noqa: S311` | drop directive |
| FX1 | `tests/security/test_db_hardening.py:319,345` | RUF043 regex metachars in `match=` | `match=r"check constraint\|does not match"`, `match=r"duplicate key\|unique"` |
| FX2 | `src/reva/adapters/connectors/worldbank.py:290` | E501 (124 > 120) | wrap line |
| FX3 (kernel) | `src/reva/kernel/values.py:20` | I001 import block (extra blank line before `_DECIMAL_RE`) | `uv run ruff check --fix src/reva/kernel/values.py` (removes one blank line) |
| lead | `src/reva/adapters/postgres/repositories.py:222` | E501 (121) | wrap line |
| lead | `src/reva/ports/autonomy.py:11,14` | UP035, UP040 | `from collections.abc import Mapping, Sequence`; `type JSON = ...` |
| lead | `tests/conftest.py:58,75` | RUF100 unused `noqa: BLE001` | drop directives |

## Trend vs round 1

| | Round 1 | Round 2 |
|---|---|---|
| pytest failed / errors | 15 / 4 | **3 / 0** |
| passed | 1147 | 1411 (+264 new tests) |
| xfailed | 12 | 3 (strict contract xfails resolved) |
| ruff | 15 | 11 |
| mypy | 1 | 0 |
| UI | green | green |

Fixed since round 1: component digest re-pin (FX3), `REVA_DB_SSLMODE` in `test_start_env.py` (I) and
`test_queue_reliability._production_settings` (FX3), `test_missing_handler_is_a_typed_failure_never_success`
(now passes), `kernel/uncertainty.py` mypy, `safe_client.py`/`s3.py`/`population/service.py` lint.
New this round: `test_model_evaluator_proper_metrics` (MASE min-points change), `kernel/values.py` I001,
`bootstrap.py:216` RUF100.
Still open from round 1: vintage year-eligibility (2 tests), lint in bootstrap/conftest/ports/test_db_hardening/worldbank/repositories.
