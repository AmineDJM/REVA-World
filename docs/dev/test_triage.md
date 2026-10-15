# Test triage (agent T)

Round 3 — 2026-10-15 00:07–00:16 UTC (HEAD `186f0f5`/`8c9d47b`, heavy in-flight editing: FX1 DB hardening,
E2 API, FX2 registry widening, autonomy/repair and invariants work)

## Commands and counts

| Check | Command | Result |
|---|---|---|
| pytest (offline) attempt 1 (00:07) | `uv run pytest tests -q -p no:cacheprovider --ignore=tests/e2e -m "not live and not perf" -rfE --maxfail=50` | **collection error**: `tests/invariants/coverage.yaml` line 612 YAML `ScannerError` (unquoted `owner:` value containing `: `). Fixed in flight at 00:07:49 (value now quoted). |
| pytest attempt 2 (00:08–00:12) | same | **20 failed**, 1614 passed, 1 xfailed, 14 deselected, 0 errors (261 s) |
| targeted reruns (00:12, 00:14) | failing files only | 20 → **3 still failing**; 17 were in-flight edits already fixed (see below) |
| ruff | `uv run ruff check src tests scripts` | **14 errors** (11 auto-fixable) |
| import-linter | `uv run lint-imports` | 5 kept, 0 broken |
| mypy (kernel strict) | `uv run mypy` | pass |
| UI typecheck / tests | `npm --prefix ui run typecheck`, `npm --prefix ui run test -- --run` | pass / 8 files, 47 tests pass |

## Outstanding failures (after reruns at 00:14)

| Test | Owner | Root cause | Exact suggested fix |
|---|---|---|---|
| `tests/unit/test_ontology_mappings.py::test_exact_mapping_is_identity_only_within_applicability` | **FX2** (caused by its registry change; test file is agent K's, K inactive) | `registry/mappings/worldbank.yaml` bumped to mapping 1.1.0 with `years: [1960, 2024]` (was `[2000, 2024]`, intended: covers the vintage fixtures). The test still expects `require_identity(subject="FRA", year=1999)` to raise `IncompatibleSemantics("does not apply")`; 1999 is now inside applicability → `DID NOT RAISE`. | In the test change `year=1999` to `year=1959` (first year outside the new range). |
| `tests/integration/test_eurostat_discovery.py::test_end_to_end_promotion_ingestion_and_belief_separation`, `::test_rollback_retires_eligibility` (and `test_evidence_fixes.py::test_population_from_promoted_eurostat_connector` in the full run) | **autonomy/repair author** (new `tests/integration/test_connector_repair.py`, not in the ownership table) — **shared-session-DB order dependence** | `registry/policies/budgets.yaml` `max_open_proposals: 20`. All tests share one session DB; the new `test_connector_repair.py` (alphabetically before `test_eurostat_discovery.py`) persists ~10 repair proposals and never closes them, so with the autonomy-gate tests the open backlog reaches 20–21 and Eurostat discovery returns `{'status': 'blocked', 'reason': 'proposal backlog is full (21 open >= 20)'}`. Eurostat tests passed in round 2 (before the repair tests existed). | Make `test_connector_repair.py` self-cleaning: a module-scoped autouse fixture that, on teardown, closes the proposals it created (e.g. via `ops.review_proposal(id, 'reject', 'human:test-cleanup', ...)` or the module's own withdraw path), **or** run it on an isolated DB (`tests/fixtures/adversarial/isolated_db.py`). Do not raise the policy limit (it is pinned, T3). |
| (rerun only) `test_eurostat_discovery.py` both tests at 00:14 | in-flight (autonomy author) | `NameError: name 'take_evidence_write_lock' is not defined` at `src/reva/autonomy/jobs.py:330` — import was being added; present at 00:16 (`jobs.py:65`). | none (re-check next round) |

## In-flight failures seen in the full run, already fixed by rerun time

| Test(s) | Cause | Fixed by |
|---|---|---|
| `test_autonomy_gates.py::test_repository_policy_matches_pin`, `test_evaluation_service.py::test_full_panel_stores_evaluations_promotions_and_label`, `test_forecast_tournament.py::test_long_geometric_panel_promotes_log_drift`, `::test_policy_defaults_and_validation` | `registry/policies/promotion.yaml` bumped to `promotion-1.1.0` (re-pinned in `autonomy/policy.py`); tests expected 1.0.0 | tests updated in flight (~00:13) |
| `test_autonomy_contracts.py::test_model_evaluator_proper_metrics` (round 2 item) | MASE `MIN_SCALE_POINTS = 3` | test extended to 1998–2003 series (fixed) |
| `test_autonomy_contracts.py::test_model_registry_requires_approval_and_is_immutable` (rerun 1 only) | `PolicyDenied: no recorded human approval` during FX1 governance-trigger change | passes at 00:14 |
| `test_connector_repair.py::test_payload_drift_is_classified_proposed_and_gated[*]` ×8 | pinned proposal digests changed | test updated in flight |
| `test_invariant_34_no_stub_success.py::test_stub_detector_positive_control` | in-flight edit | passes on rerun |
| `test_db_hardening.py::test_years_outside_the_decision_scope_are_ineligible`, `::test_input_trigger_enforces_the_support_year_scope` | WDI years widened to 1960–2024 | FX1 updated tests in flight |
| `test_revisions.py::test_two_revisions_yield_different_as_of_answers`, `test_replay_job.py::...manifest` (round 1–2 items) | year-scoped eligibility vs 1950–1999 vintages | **resolved** by FX2 widening WDI applicability to 1960–2024 (vintages used by these tests are ≥1960) |

## Environment / flaky / order-dependent (watch list)

- **Connection limit** (round 1): `too many connections for role "reva_worker"` (runtime roles `CONNECTION LIMIT 10`,
  cluster-wide, shared by all agents' concurrent `reva_test_*` sessions; one suite run holds up to 6 worker
  connections). Not seen in rounds 2–3 (reva_worker 1–4/10 before runs). Suggested: **FX1** configurable
  `REVA_DB_ROLE_CONNECTION_LIMIT` (10 production, ≥40 local/test); **lead** conftest/FreshDB smaller pools.
- **Lock cascade** (round 1): `test_evidence_fixes.py` r03 failure leaked an `ops.dirty_node` row lock →
  `LockNotAvailable` in the next two tests. Suggested (**FX2**): `try/finally` rollback+join in r03's ingest thread.
- **Proposal backlog** (round 3): see table above — any new test that persists open proposals in the shared
  session DB pushes the Eurostat tests over `max_open_proposals`.
- ~75 leaked `reva_test_*` databases on the cluster.

## Lint / type failures by owner

| Owner | Location | Issue | Fix |
|---|---|---|---|
| FX2 | `src/reva/adapters/connectors/worldbank.py:290` | E501 (124) | wrap line |
| FX3 (kernel) | `src/reva/kernel/values.py:8` | I001 (extra blank line after imports) | `ruff check --fix` |
| lead | `src/reva/adapters/postgres/repositories.py:222` | E501 (121) | wrap line |
| lead / autonomy author | `src/reva/ports/autonomy.py:11,14` | UP035, UP040 | `from collections.abc import Mapping, Sequence`; `type JSON = ...` |
| autonomy author | `src/reva/autonomy/generator.py:10`, `src/reva/autonomy/sandbox_child.py:19` | I001 import order | `ruff check --fix` |
| autonomy/repair author | `tests/fixtures/repair/derive.py:13` | I001 | `ruff check --fix` |
| invariants author | `tests/invariants/test_coverage_map.py:185` (RUF100 `T201`), `test_invariant_04_source_reports.py:35` (RUF100 `BLE001`), `test_invariant_34_no_stub_success.py:53` (RUF100 `ARG001`), `test_invariant_30_budgets.py:18` (I001) | unused noqa / import order | drop the directives; `ruff check --fix` |
| lead | `tests/conftest.py:58,75` | RUF100 unused `noqa: BLE001` | drop directives |

Resolved since round 2: `cli/bootstrap.py` RUF100 ×2 and `tests/security/test_db_hardening.py` RUF043 ×2 (FX1).

## Trend

| | R1 | R2 | R3 (full run) | R3 (after reruns) |
|---|---|---|---|---|
| pytest failed / errors | 15 / 4 | 3 / 0 | 20 / 0 (+1 collection error, attempt 1) | 3 genuine |
| passed | 1147 | 1411 | 1614 | — |
| ruff | 15 | 11 | 14 | — |
| mypy | 1 | 0 | 0 | — |
| UI | green | green | green | — |
