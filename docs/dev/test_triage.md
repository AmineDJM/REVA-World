# Test triage (agent T)

Round 1 — 2026-10-14 23:33–23:45 UTC (HEAD `0648f6b` + dirty working tree, many in-flight edits)

## Commands and counts

| Check | Command | Result |
|---|---|---|
| pytest (offline) | `uv run pytest tests -q -p no:cacheprovider --ignore=tests/e2e -m "not live and not perf" -rfE --maxfail=50` | **15 failed, 4 errors**, 1147 passed, 12 xfailed, 14 deselected (458 s) |
| ruff | `uv run ruff check src tests scripts` | **15 errors** (7 auto-fixable) |
| import-linter | `uv run lint-imports` | 5 kept, 0 broken |
| mypy (kernel strict) | `uv run mypy` | **1 error** |
| UI typecheck | `npm --prefix ui run typecheck` | pass |
| UI tests | `npm --prefix ui run test -- --run` | 6 files / 39 tests pass |

After re-running failures in isolation (23:41–23:44): 4 settings-related failures were already fixed in flight
(`tests/unit/test_settings.py` updated 23:40, `test_storage_s3` updated), and the 8 connection-limit / lock-timeout
failures pass when run alone. **Genuine outstanding failures: 7** (below).

## Outstanding failures (deterministic)

| Test | Owner | Root cause | Exact suggested fix |
|---|---|---|---|
| `tests/contracts/test_registry_components.py::test_published_versions_match_pinned_digests` | **FX3** (caused by its `kernel/ids.py` change; file `src/reva/ontology/components.py` is agent K's, K inactive) | `CANONICAL_HASH_VERSION` bumped `reva-cjson-1` → `reva-cjson-2` (intended, R-22). Version is part of every `logical_hash`, so all 4 pinned manifest digests in `PUBLISHED_COMPONENT_DIGESTS` changed although no component schema changed (verified: `registry/components/*` untouched since c8696e5). | Re-pin in `src/reva/ontology/components.py` lines 43-48: `demographics@1.0.0: 825984b83315a58138d0ea861fe5e32ae0a21b65da7460f51025e5ef6f16d075`, `display_geometry@1.0.0: e5e67661bf2dd912bfeb701aa001c93a84b7ed70d2a8ccb278813e3b5148873b`, `economy@1.0.0: be5896ab0a9a810c4bd62e1921071dfe3e3ecb6c3202106cc6157938ae2bbebe`, `trade_crude@1.0.0: 028c9431dd51ad617088d2123f27b69ffa5e960ad226a5e4d073d1073f41d081` (with a comment "re-pinned for reva-cjson-2"). |
| `tests/unit/test_ontology_components.py::test_verify_published_detects_in_place_edit` | **FX3** | Same as above (fails before reaching the in-place edit check). | Same re-pin. |
| `tests/integration/test_revisions.py::test_two_revisions_yield_different_as_of_answers` | **FX2** (intended behaviour change in `belief/selection.py`; test owner H1 inactive) | Since ef1d09d `ELIGIBLE_SQL` passes the support year to `evidence.is_eligible(..., ref_year)` (R-07 year-scoped eligibility). Seeded WDI eligibility is `years: [2000, 2024]` (`registry/mappings/worldbank.yaml`), but the vintage fixtures use years 1950–1999 (`pop_fra_1990_1991_two_vintages`). Those observations are now ineligible → isolated run: `ValidationProblem: no eligible observations at this cutoff`; in the full run other rows exist so the release publishes but has no output for `wb:SP.POP.TOTL:FRA:1991`. | In `tests/fixtures/vintages/vintage_loader.py::ingest_vintage` (or a session helper), record an extra **year-scoped** test eligibility decision for the WDI connector version(s) and POP concept: `applicability={"subjects":["FRA"],"subject_ids":[str(entity_id("FRA"))],"years":[1950,1999],"mapping_key":"wb.SP.POP.TOTL"}`, `policy_version="test-vintages"`. Because `is_eligible` filters by scope before "latest wins", this does not affect 2000–2024 rows used by other tests. (Alternative: move vintages into 2000–2024 — rejected, collides with captured live fixture years.) |
| `tests/replay/test_replay_job.py::test_operational_replay_job_writes_evaluation_context_and_manifest` | **FX2** (same cause; test owner H1 inactive) | Same year-scoped eligibility: vintage `pop_fra_1960_1961_job` rows (`wb:SP.POP.TOTL:FRA:1960/1961`) are not eligible, so they are absent from the release inputs (`Extra items in the left set`). | Same vintage-loader eligibility fix (years range must cover 1950–1999). |
| `tests/integration/test_queue_reliability.py::test_missing_handler_is_a_typed_failure_never_success` | **L** (its new real handler made the test premise false; test file is agent D's, D inactive) | Test assumes `evaluation` has no handler. `reva.evaluation.tournament_service:handle_evaluation` now exists and rejects payload `{"probe": "unavailable"}` → `last_error.code == "validation_error"` (correct behaviour), test expects `handler_unavailable`. | In the test, also `monkeypatch.setitem(jobs.HANDLERS, "evaluation", "reva.evaluation.no_such_module:handle_evaluation")`, or iterate only over job types monkeypatched to a missing module. |
| `tests/integration/test_queue_reliability.py::test_worker_refuses_migration_url_in_production` | **FX3** (settings change; test file D's, D inactive) | `Settings` now requires `REVA_DB_SSLMODE ∈ {require, verify-ca, verify-full}` in production; `_production_settings()` (line 458) lacks it, so `Settings(...)` raises `ValidationError` before `check_environment` is exercised. | Add `"reva_db_sslmode": "verify-full",` to the `data` dict in `_production_settings()`. |
| `tests/security/test_start_env.py::test_api_production_validation_redacts` | **I** | Same new sslmode rule: the `PROD` env dict lacks `REVA_DB_SSLMODE`, so `start_api.validate(PROD)` raises `ConfigError`. | Add `"REVA_DB_SSLMODE": "verify-full"` to `PROD` in `tests/security/test_start_env.py` (check `summary["db"]` assertion still holds with `?sslmode=verify-full`). |

## Environment / flaky / order-dependent

| Test(s) | Classification | Evidence | Suggested fix / owner |
|---|---|---|---|
| `test_queue_invalidation.py` (4 setup ERRORs), `test_evidence_fixes.py::test_r03_release_waits_for_inflight_ingest`, `test_queue_reliability.py::test_concurrent_claims_never_return_the_same_job` | **Environment: role connection limit** | `FATAL: too many connections for role "reva_worker"`. `reva_worker`/`reva_api`/`reva_sim` have `CONNECTION LIMIT 10` cluster-wide; `pg_stat_activity` showed reva_worker at 10/10 (all idle) at 23:34, spread over ~6 concurrent agents' `reva_test_*` DBs plus a `reva_live_*` DB. One suite run alone holds up to 6 worker connections (session `db.worker` pool 2 + module `FreshDB` worker pool 4). All pass in isolation. | **FX1** (bootstrap): make the runtime-role connection limit configurable, e.g. `REVA_DB_ROLE_CONNECTION_LIMIT` (10 in production, ≥40 for `REVA_ENV in {local,test}`). **lead** (conftest) / test owners: use `pool_size=1` for the session `db.worker` or dispose it, and `pool_size=2` for `FreshDB`. Agents: avoid running the full DB suite concurrently. |
| `test_evidence_fixes.py::test_r03_future_cutoff_rejected_and_none_means_db_now`, `::test_r04_live_ingest_identical_to_fixture_creates_live_lineage` | **Cascade / order-dependent** | `LockNotAvailable` on `ops.dirty_node` (`belief:demography...`): the preceding `test_r03_release_waits_for_inflight_ingest` failed mid-way (connection limit) while its in-flight ingest held the row lock. Pass when the file runs alone. | **FX2** (test file owner): make r03's in-flight ingest thread release in a `finally` (rollback + join) so a failure cannot leak a held lock into later tests. |
| `test_settings.py` ×3, `test_storage_s3.py::test_factory_builds_s3_store_for_production_settings` | **In-flight edit** (fixed) | sslmode rule; test files updated 23:40; pass on rerun. | none |

Leftover test databases: 61–79 `reva_test_*` databases exist on the cluster (only ~5–8 have sessions). Not a failure
cause, but sessions killed mid-run leak them; FX1/I may want a `scripts/` cleanup for `reva_test_%` older than 1 h.

## Lint / type failures by owner

| Owner | Location | Issue | Fix |
|---|---|---|---|
| FX3 | `src/reva/adapters/http/safe_client.py:172` | E501 (121) | wrap line |
| FX3 | `src/reva/adapters/http/safe_client.py:210` | RUF100 unused `noqa: SLF001` | drop the directive |
| FX3 | `src/reva/adapters/http/safe_client.py:480` | UP041 aliased timeout error | use builtin `TimeoutError` |
| FX3 | `src/reva/adapters/objects/s3.py:114` | RUF100 unused `noqa: BLE001` | drop directive |
| FX3 (kernel) | `src/reva/kernel/uncertainty.py:102` (mypy strict) | `_dec` annotated `-> object`, so `lo > hi` is untyped | change signature to `def _dec(v: str \| None, name: str) -> Decimal \| None:` (import `Decimal` at module level) |
| FX1 | `src/reva/cli/bootstrap.py:128` | RUF100 unused `noqa: BLE001` | drop directive |
| FX1 | `tests/security/test_db_hardening.py:319,345` | RUF043 regex metachar in `match=` | use raw strings `r"check constraint\|does not match"`, `r"duplicate key\|unique"` |
| FX2 | `src/reva/population/service.py:82,94` | E501 | wrap |
| FX2 | `src/reva/adapters/connectors/worldbank.py:290` | E501 (124) | wrap |
| lead | `src/reva/adapters/postgres/repositories.py:215` | E501 (121) | wrap |
| lead | `src/reva/ports/autonomy.py:11,14` | UP035 (`collections.abc`), UP040 (`type JSON = ...`) | `from collections.abc import Mapping, Sequence`; `type JSON = ...` |
| lead | `tests/conftest.py:58,75` | RUF100 unused `noqa: BLE001` | drop directives |

## Known xfails (strict, documented contract mismatches — not counted as failures)
`tests/contracts/test_json_schemas.py::test_kernel_uncertainty_kinds_match_observation_contract`,
`::test_exported_observation_records_validate_against_contract` and 10 others (settings/kernel/security).

## Trend
Round 1 — baseline.
