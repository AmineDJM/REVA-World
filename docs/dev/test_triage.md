# Test triage (agent T)

## Round 7 (agent FIX2, 2026-10-15 02:15–02:55 UTC)

Command: `uv run pytest -m "not live and not perf" -q -p no:cacheprovider -rfE` (GATE had renamed the
security copy to `test_gate_separation_security.py`, so no `--ignore` was needed).

| Run | Result |
|---|---|
| 02:26–02:46 | **2 failed, 2370 passed, 30 skipped, 21 deselected** (1211 s) |
| reruns 02:48–02:55 of both failures + every Round 6 target (`tests/unit/social`, PEN pipeline, `test_start_env`, ALLC module, `tests/contracts` schema contract) | all pass (10 + 125 passed) |
| ruff on touched files | clean |

### Round 6 items
- **PEN** (`tests/unit/social/test_retirement_age_model.py`, `test_labour_pensions_retirement_pipeline.py`): no
  fixture work needed. The DE `demo_pjan` fixture exists (`tests/fixtures/social/eurostat.demography`, plain
  `sinceTimePeriod=2000` URLs that `fixture_inputs.py` requests with `window_years=None`). Run C caught POP editing
  `eurostat_demography.py` (mtime 02:01:43, inside run C 01:47–02:07). Both modules pass now, alone and in the
  full run.
- **ALLC** (`test_all_country_fixture_ingest_keeps_gaps_typed`): order-dependent. **The connector is correct.**
  An offline probe shows that `un.comtrade.public_batch` emits the DZA 2023 M/X typed Missing
  (`provider_returned_no_rows`). The row has the same semantic hash as the one `un.comtrade.public_exact` emits.
  A lineage is (source, origin, logical key), and both connectors share the source `un.comtrade.public_preview`.
  So when an earlier module has ingested the exact connector's fixtures (e.g. `test_autonomy_contracts.py`),
  the batch row is "unchanged" and is not inserted again under the batch connector version. The test filtered
  on `cv.name = 'un.comtrade.public_batch'`. Reproduced with
  `pytest tests/integration/test_autonomy_contracts.py tests/integration/test_comtrade_worldbank_all_countries.py`.
  Fix (test): read the lineage heads of the source, whichever connector wrote them
  (`DISTINCT ON (logical_key) … ORDER BY revision_no DESC`). The test passes in both orders.
- **POP** (`test_start_env::test_bootstrap_runtime_reports_schema_without_secrets`): the build is
  deterministic. Spec sha and code digest of every built-in connector are identical across two calls in one
  process and across `PYTHONHASHSEED` 1/2/3. `module_digest` hashes the module source *on disk*: `inspect`
  re-reads the file when its mtime changes. The session DB was seeded at the start of run C. POP's edit at 02:01
  changed the file, so the bootstrap subprocess near the end of the run computed a new digest. That is a
  legitimate content change (pre-release, no bump needed), not a nondeterminism. Passes now.
  Optional hardening (shared `adapters/connectors/base.py`, not edited): memoise `module_digest` per module,
  so one long-lived process never reports two digests for the code it loaded.

### Remaining failures (02:26 run), both transient from concurrent edits
| Owner | Test | Root cause |
|---|---|---|
| governance migration author (0006 edited 02:30–02:47) | `test_backup_restore_roundtrip::test_backup_restore_round_trip…` | The restore's `verify-schema` found 6 function definitions that differ from the contract: `evidence.approve_eligibility`, `restore_eligibility`, `model.approve_version`, `ontology.approve_mapping`, `ops.consume_approval`, `ops.require_review_activity`. `0006_operations.py` changed while the run was going (latest mtime 02:47). Passes on rerun, schema-contract test included. Separately, the test env does not pass `REVA_GATE_DB_PASSWORD`, so restore warns `runtime role reva_gate not provisioned`. **BKP/GATE**: add it as `test_start_env` does. |
| PEN / SFX1 (rules, retirement_age, backtest edited 02:35–02:47) | `test_retirement_backtest::test_backtest_is_reproducible_and_matches_the_committed_artifact` | `docs/dev/v2_pen_backtest.json` was stale while `rules.py`/`retirement_age.py` were being edited. It was regenerated at 02:37 and passes on rerun. |

## Round 6 (agent STAB, 2026-10-15 01:00–02:10 UTC)

Command (as CI; `-n` is not available, pytest-xdist is not installed):
`uv run pytest -m "not live and not perf" -q -p no:cacheprovider -rfE` (includes `tests/chaos`).

| Run | Result |
|---|---|
| A 01:21–01:41 (mid-flight GOV `reva_gate` work) | 85 failed, 2122 passed, 30 skipped, 17 errors |
| B 01:46 | collection error: `tests/integration/test_gate_separation.py` and `tests/security/test_gate_separation.py` share a basename (rootdir import mode) — **GOV**: rename one; the whole suite does not collect until then |
| C 01:47–02:07 (`--ignore=tests/security/test_gate_separation.py`) | **14 failed, 2299 passed, 30 skipped, 7 errors** (1219 s) |
| ruff `src tests scripts` | 1 error: `src/reva/autonomy/gate.py:131` E501 (**AFX**); tests and scripts clean |
| lint-imports / mypy | 5 kept, 0 broken / pass |

### Fixed this round (STAB)
- **CHAOS-1/2** (`src/reva/operations/worker.py`): local lease deadline — after 90 % of the lease without a
  successful heartbeat (basis = send time of the last successful extension, initially the claim) the lease
  is presumed lost, `StaleLease` stops the handler (also checked at rate-limited safe points); failed
  heartbeats are rate-limited by `last_attempt`. Strict xfail removed; the outage test now waits for the
  post-reconnect presence beat (it raced a 5-step resumed attempt). `tests/chaos` 11/11, then the queue
  module 9/9 twice; `test_worker_loop.py` passes (its two simulation failures in my first run were ENG's
  in-flight `plan_run`, fixed since).
- `test_redaction_r7::test_real_db_errors_are_scrubbed_in_job_errors_and_logs`: the code is right
  (`hide_parameters=True` is stricter); the test now requires parameters to be hidden *or* rendered and then
  masked, sensitivity kept through PostgreSQL's DETAIL echo of the key.
- `test_secrets_logs::test_captured_fixture_bodies_contain_no_credentials`: recursive scan over files only
  (ALLC's `probes/` directory).
- `test_evidence_fixes`: estimation-flag test sets `isQtyEstimated/isNetWgtEstimated=False` for the code-0
  case; `_ensure_reapproved` re-enables through `reva.autonomy.jobs.reapprove_promotion` on `db.owner`
  (newest retired version under the current template) and discovery may answer `rolled_back_hold`.
- `tests/replay/test_leakage_cases.py`: maintainer-attributed alias/mapping rows written through `db.owner`.
  (`test_revisions.py` was already fixed.)
- `test_world_graph.py`: the release-pinned and causal relations are written inside a *new* publication
  transaction (wrapping `pin_release_relations`), before `ops.publication` seals it.
- `tests/invariants/coverage.yaml`: inv21 cites `simulation/crude_plugin.py` (`_range_uncertainty`,
  `_unknown_uncertainty` moved there in V2); invariants 184 passed.
- `test_observation_export.py`: fixture scoped to the acquisitions of its own fixture bodies (raw digests).
- `test_evaluation_service.py` and `test_backup_restore.py`: module-scoped private database
  (`tests/fixtures/adversarial/isolated_db.py`), immune to other modules' rows and concurrent writes.
- Backup/restore (`scripts/backup_restore.py`): database-level grants are re-applied from the grants
  recorded in the backup's catalog manifest (was a hard-coded api/worker/sim list → `reva_gate CONNECT`
  missing after restore); missing `reva_*` grantee roles are created NOLOGIN before pg_restore. ACLs are
  kept (not `--no-acl`): bootstrap does not re-grant object privileges, so dropping ACLs would lose them
  (and the non-superuser restore, which skips bootstrap, would fail the catalog comparison). No
  `pg_default_acl` entries exist in current test databases, so the earlier "change default privileges"
  error does not reproduce; both backup tests pass (4/4) including the non-superuser restore.
- `publish_acquisition` (`src/reva/evidence/ingest.py`): every value is validated with
  `reva.kernel.values.parse_value`; any non-kernel value quarantines the item (finding
  `invalid_kernel_value`, keys and error location only, never the value). Test added. The R7 belief
  selection property test's synthetic value gained its required `basis`.
- `ingest.py` lineage locks striped into 256 buckets taken once per acquisition in ascending order (was one
  advisory lock per observation: large acquisitions exhausted the cluster-wide lock table — run A's
  `out of shared memory` errors in `test_evaluation_service` / `test_demography_cohort`). Test added.
- `test_queue_reliability::test_schema_wait…` accepts revisions ≥ 0007 (0008_v2 exists);
  `test_prop_r7_kernel::test_interval_whose_end_precedes…` guards date arithmetic below year 1 (Hypothesis
  found `date(10,1,1)` − 3288 days overflowing inside `assume`).
- Already fixed by others when checked: repair-module teardowns closing proposals through
  `ops.review_proposal` as `human:test-teardown` (API login); `test_roles_grants` messages; chaos lint.

### Remaining failures (run C) by owner
| Owner | Tests | Root cause |
|---|---|---|
| **GOV** | collection of `test_gate_separation.py` ×2 | duplicate basename (see run B) |
| **GOV** | `test_governance_r5::test_registry_seed_runs_as_owner…` | test still seeds through `reva_worker`; expects confirm-only behaviour the seed now refuses |
| **GOV** | `test_secrets_logs::test_non_test_files_have_no_credentials…` | `docs/dev/gate_separation.md` contains a literal `REVA_GATE_DB_PASSWORD` value |
| **migration owners (NLC/ENG 0008) + MIGR** | `test_schema_contract::test_committed_contract…`, `test_api_endpoints::test_r4_06…` (SchemaDriftError) | contract lacks `ops.reserve_llm_budget`, `ops.settle_llm_budget`: run `scripts/update_schema_contract.py` with the migration |
| **POP** | `test_start_env::test_bootstrap_runtime_reports…` | session-DB re-bootstrap: `eurostat.demography@1.0.0 is registered with different content; bump the version` (connector spec changed without a version bump); also pass `REVA_GATE_DB_PASSWORD` in that test's env once GOV settles which roles bootstrap reports |
| **PEN** | `tests/unit/social/test_retirement_age_model.py` 4 failed + 7 errors; `test_labour_pensions_retirement_pipeline.py` ×2 | no captured fixture for `demo_pjan … geo=DE` (fixture manifest missing the DE item); pipeline then `missing_input` |
| **ALLC** | `test_comtrade_worldbank_all_countries::test_all_country_fixture_ingest_keeps_gaps_typed` | DZA 2023 typed-Missing row `comtrade:HS2709:M:12:0:2023:primaryValue` not emitted by the batch connector (ingest outcomes are published/unchanged, not quarantined) |
| **NLC** | `test_capabilities_v2::test_implemented_capability_cites_existing_tests[nl.scenario_drafts]` | capability cites `test_nl_drafts.py::test_retirement_age_before_the_model_exists_is_a_capability_gap`, which is not a test function |
| **AFX** | ruff E501 `src/reva/autonomy/gate.py:131` | wrap |

Transient in run A only (resolved by the owners during the round): `permission denied for function
record_gate_result` ×~45 and `proposal backlog is full (32 open >= 20)` cascades (GOV moving gate results to
`reva_gate`), `budgets.yaml` pin mismatch, ENG `scenario.schema.json` stale, NL drafts schema, LI `demo_pjan`
fixture.


Round 5 (final for this session) — full run 2026-10-15 00:41:31–00:53:20 UTC, follow-up reruns to 00:58.
The run overlapped heavy in-flight work: FX1 governance/eligibility migrations (R5), CVAL Comtrade 1.2.0,
AF autonomy, HOOK repair hook-up and SEC API. Several clusters below are "intended change, callers/tests not yet
updated"; treat them as work items for the named owner, not flakes.

Ownership used (per coordinator updates): FX1 migrations/bootstrap/governance; FX2 evidence, belief (except
publication.py), seed, registry; FX3 http/objects/kernel/settings/gc/spec/pyproject; CVAL
`adapters/connectors/{worldbank,comtrade}.py`; HOOK `operations/jobs.py`, `belief/publication.py`,
`world/queries.py`, `autonomy/repair.py`; SEC `src/reva/api/**`; AF `src/reva/autonomy/**` (rest); E2 ui; L
evaluation service; I deploy/docs/scripts; H2 security/unit tests; lead conftest/shared files.

## Commands and counts

| Check | Command | Result |
|---|---|---|
| pytest (offline) | `uv run pytest tests -q -p no:cacheprovider --ignore=tests/e2e -m "not live and not perf" -rfE --maxfail=80` | **42 failed**, 1840 passed, 6 skipped, 6 xfailed, 14 deselected (704 s) |
| ruff | `uv run ruff check src tests scripts` | **25 errors** (all in new test files; src clean except none) |
| import-linter | `uv run lint-imports` | 5 kept, 0 broken |
| mypy (kernel strict) | `uv run mypy` | pass |
| UI | `npm --prefix ui run typecheck`; `npm --prefix ui run test -- --run` | pass; 10 files / 59 tests pass |

## Outstanding failures by root-cause cluster

### C1 — FX1 governance: runtime roles may not write human-attributed rows (intended R5 change) — 9 tests
`psycopg.errors.InsufficientPrivilege: governance: reva_worker may not record a human-attributed activity in
meta.activity` / `... audit row in ops.audit` (`meta.enforce_governance()`).

| Test | Owner | Fix |
|---|---|---|
| `test_evidence_fixes.py::test_r04_live_ingest_identical_to_fixture_creates_live_lineage`, `::test_r27_retracted_latest_revision_is_not_replaced_by_superseded_value`, `::test_r27_ineligible_latest_revision_falls_back_with_flag`, `_ensure_reapproved` helper | **FX2** | Record `human:*` review activities / retractions / eligibility re-approvals through `db.owner` (migration owner) or the reviewed definer function FX1 provides, not `db.worker`. AF also notes `_ensure_reapproved` should call `reva.autonomy.jobs.reapprove_promotion(...)` (or pick the version `promoted_connectors` loads) instead of a raw INSERT on the newest-recorded version, and accept `rolled_back_hold` from `handle_discovery`. |
| `test_revisions.py::test_replayed_raw_with_new_parser_is_distinguished_from_original_vintage`, `tests/replay/test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode` | **FX2** (H1 inactive) | same: human review rows via owner engine / definer function (`tests/fixtures/vintages/vintage_loader.py` uses actor `maintainer:test-vintages` through the worker). |
| `test_connector_repair.py::test_only_gate_promotion_or_named_human_resumes_publication`, `test_repair_hookup.py::test_quarantine_to_repair_to_withheld_release_to_stale_read_to_resume` | **HOOK** | named-human resume writes `ops.audit` with a `human:` actor through the worker: route through the API/owner path FX1 sanctions. |

### C2 — FX1 eligibility applicability format `years: {from,to} | {in:[...]}` (intended R5-11) — 7 tests
`InvalidParameterValue: applicability.years must be {"from": y0, "to": y1} or {"in": [y, ...]} (got [2023])` from
`evidence.approve_eligibility(...)`.

| Test | Owner | Fix |
|---|---|---|
| `test_eurostat_discovery.py` ×5 (`end_to_end…`, `rollback_retires_eligibility`, `r4_04_rediscovery…`, `r4_03_changed_content…`, `r4_03_template_change…` — last one is a cascade `KeyError: 'eurostat.demo_pjan.fr'`), `test_evidence_fixes.py::test_population_from_promoted_eurostat_connector` | **AF** | Eurostat promotion (`src/reva/autonomy/promotion.py`/`binding.py`) still builds `{"years": [2023]}`; emit `{"years": {"in": [2023]}}` (or `{"from": y0, "to": y1}`). Check every applicability writer: `grep -rn '"years"' src/reva/autonomy`. |
| proposal backlog (`max_open_proposals: 20`) | **HOOK / repair author** | Seen in rounds 3–4 and reported by AC: repair tests leave ~10 open proposals in the shared session DB. Add a module-scoped teardown that closes them via `ops.review_proposal(id, 'reject', 'human:test-cleanup', …)` (as the API/human principal), or run those modules on `tests/fixtures/adversarial/isolated_db.py`. Do not raise the pinned limit. |

### C3 — DZA typed-Missing crude beliefs disappear (classification scope) — 6 tests
`test_api_endpoints.py::test_dza_crude_import_belief_is_typed_missing`, `::test_beliefs_are_release_pinned_and_as_of`
(`crude missing 0 >= 4`), `::test_coverage_and_gaps`, `::test_query_spec` (`KeyError: '2023'`),
`::test_r4_06_discovery_request_is_validated_and_passed_through`;
`test_autonomy_contracts.py::test_gap_detector_against_database_finds_dza_crude_missing`.

Root cause: new `evidence.is_eligible(..., classification)` requires a classification when the decision has a
`classifications` scope; a provider-no-rows Missing (`source_flags = {"provider_returned_no_rows": true}`) has
no `classificationCode` → ineligible → no DZA beliefs. Verified the connector still emits the Missing rows
(offline probe of `un.comtrade.public_exact` 1.2.0: DZA M/X 2020–2023 `missing`).
- **FX1/FX2**: being fixed — `registry/mappings/comtrade.yaml` now has `admit_unclassified_missing: true`; the
  migration's `is_eligible` must honour it (allowed-keys list in `is_eligible` currently rejects unknown
  applicability keys — add `admit_unclassified_missing` there and to `check_eligibility_applicability`).
- **AF**: `src/reva/autonomy/gaps.py:47` calls `evidence.is_eligible(cv, concept, subject, :cutoff)` with no
  `ref_year`; with year-scoped decisions that is always `false` (the gap test then reports "missing: no
  observation" instead of "typed Missing"). Use `evidence.observation_is_eligible(l.id, :cutoff)`.

### C4 — schema contract stale vs migrations — 3 tests
`test_schema_contract.py::test_committed_contract_matches_fresh_migrations`,
`test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of` (`SchemaDriftError`),
`test_start_env.py::test_bootstrap_runtime_reports_schema_without_secrets`.
**FX1**: after each migration edit run `uv run python scripts/update_schema_contract.py` and commit
`migrations/schema_contract.json` together with the migration.

### C5 — FX1 R5 grants/sealing vs older tests — 9 tests
| Test | Error | Owner / fix |
|---|---|---|
| `test_backup_restore_roundtrip.py::test_backup_restore_round_trip_fresh_db_and_object_store` | `pg_restore: permission denied to change default privileges` (non-superuser restore) and catalog grants on `meta.enforce_governance()` missing after restore | **I** (`scripts/backup_restore.py`: restore with `--no-privileges` for `ALTER DEFAULT PRIVILEGES` or run them as owner; re-apply function grants) with **FX1** (which grants are expected) |
| `test_db_hardening.py::test_sim_cannot_attach_provenance_to_source_activities`, `::test_sim_cannot_register_locations_for_other_artifacts`; `test_roles_grants.py::test_sim_object_locations_limited_to_scenario_namespaces` | `permission denied for table artifact` raised earlier than the trigger the test expects | **FX1** (db_hardening) / **H2** (roles_grants): accept `InsufficientPrivilege` as the refusal, or set up the artifact row as owner first |
| `test_roles_grants.py::test_scenario_rejects_unpublished_baseline_direct_insert`, `::test_scenario_spec_baseline_must_match_pinned_baseline` | `Regex pattern did not match` (refusal message changed) | **H2**: update `match=` to the new FX1 messages |
| `test_db_hardening.py::test_bootstrap_with_non_superuser_createrole_owner` | `role reva_registry exists but migration owner fx1_owner_… holds neither SET nor ADMIN on it` — cluster-global role created by another session's bootstrap | **FX1**: environment/order issue (roles are cluster-wide); test must create its own uniquely named roles or grant ADMIN in setup |
| `test_governance_r5.py::test_registry_seed_runs_as_owner_and_runtime_roles_only_confirm_it` | `ConfigurationError: registry seeding … must run as the migration owner` | **FX1** + **lead**: `tests/conftest.py` still calls `seed(d.worker, store)`; switch to `seed(d.owner, store)` (and in the `FreshDB` fixtures of `test_queue_*`/`test_worker_loop`) |
| `test_world_graph.py::test_release_pinned_context`, `::test_history_separates_sequence_from_causal_hypotheses` | `release … is published and sealed; publish a new release instead` | **HOOK** (world/queries.py owner; J inactive): tests insert estimates into an already-published release; build a new release instead |
| `tests/invariants/test_invariant_32_deletion_overrides_retention.py::test_reviewed_deletion_ticket_can_delete_an_immutable_record` | `Immutable scientific record meta.artifact` | invariants author + **FX1** (deletion-ticket bypass must cover the new trigger) |

### C6 — Comtrade 1.2.0 (CVAL, intended) — test updates
`test_evidence_fixes.py::test_r05_*` ×2, `::test_comtrade_estimation_flag_replaces_uninformative_derived_flag`,
`::test_comtrade_implausible_jump_is_a_review_signal_not_a_block`, `test_http_connectors.py::test_comtrade_hostile_strings_are_inert_data`.
Updated at the coordinator's request (see "Edits made" below). Rerun 00:56: r05 ×2, jump, hostile **pass**;
estimation-flag test **still fails** at the code-0 weight assertion
(`w.source_flags["provider_quantity_estimated"] is False` → True) because the captured CHN 2023 row carries
`isQtyEstimated`/`isNetWgtEstimated = true`. Fix (**FX2**): in that test also set
`"isQtyEstimated": False, "isNetWgtEstimated": False` in the `body["data"][0].update({...})` call.

### C7 — other
| Test | Owner | Root cause / fix |
|---|---|---|
| `test_connector_repair.py::test_payload_drift_is_classified_proposed_and_gated[wb_unit_text_and_scale]` (`fail` vs `pass`), `::test_unit_change_suggests_conditional_mapping_that_leaves_history_intact` (`['unit_drift'] == []`) | **HOOK** (repair) | expectations predate the WDI mapping 1.1.0 / CVAL unit-drift checks; re-derive expected outcomes from the current mapping |
| `test_coverage_map.py::test_every_cited_test_node_exists[inv18]`, `::test_every_invariant_test_is_cited` | invariants author | coverage.yaml cites removed `test_db_hardening.py::test_publication_requires_a_transaction_scoped_lock`; new `test_invariant_30_budgets.py` tests are uncited — update `tests/invariants/coverage.yaml` |

## Order-dependence (shared session DB) — root-caused this round

| Symptom | Root cause | Fix / owner |
|---|---|---|
| `tests/contracts/test_observation_export.py` 6 setup errors `stored value is not a valid kernel value` after `test_evidence_fixes.py` (AF report) | `_synthetic()` in `test_evidence_fixes.py` stored `{"kind":"decimal","value":…,"unit_ref":…}` **without the required `basis`**; the export fixture selects every WDI/Comtrade observation in the session DB. Also a genuine gap: `publish_acquisition` stores values without `reva.kernel.values.parse_value` validation. | Test value fixed (see Edits). **FX2**: validate every `ParsedObservation.value` with `parse_value` in `evidence/ingest.py` (block the batch) so non-kernel values can never be stored. **H2**: scope the export fixture to its own acquisitions. Verified: export module passes after `test_evidence_fixes` at 00:56. |
| `test_evaluation_service.py` `n_forecasts` 6/7 vs 8 after `test_evidence_fixes`/`test_api_endpoints` (AF report) | `wdi_release` publishes a release over all `test_fixture` rows in the session DB; synthetic FRA POP supports (`resolution={"fx2_test": tag}`) and the API module's revised DZA POP enter `load_release_panel`, so `panel_from_rows` drops/alters series. | **L**: build `wdi_release` on a dedicated DB (as `test_api_endpoints` does) or filter the panel to the canonical WDI support (`aggregation`/`resolution` of the WDI connector). |
| `test_autonomy_contracts.py::test_health_from_recorded_runs` (`scalar_one()` sees several `connector_version` rows) | other tests register versions under the real connector name | **AF**: `WHERE name=:n AND version=:v` with `c.describe().version`. |
| `test_backup_restore.py` manifest mismatch (round 4, full run only; passes alone) | any concurrent write between `_manifest(source_url)` and the backup snapshot changes the whole-DB digest (leaked background threads from earlier modules) | **I**: compute `manifest_before` inside the backup's snapshot or stop/join background workers first; watch. |
| Connection limit / lock cascade (round 1) | `CONNECTION LIMIT 10` per runtime role shared by all agents | **FX1** configurable limit for local/test; **lead** smaller test pools. Not seen since round 1. |

## Lint by owner (ruff, 25)
| Owner | Location | Fix |
|---|---|---|
| FX1 | `tests/security/test_governance_r5.py:8` I001; F811 ×20 (`w` imported from `test_db_hardening` then used as fixture parameter) | move shared fixtures (`w`, `tx`, …) into `tests/security/conftest.py` and drop the import |
| H2 | `tests/security/test_sim_integrity.py:107` E501, `:200` RUF100 (`BLE001`) | wrap; drop directive |
| chaos author (P?) | `tests/chaos/_harness.py:174` RUF100 (`SIM115`), `tests/chaos/test_chaos_queue.py:328` E501 | drop directive; wrap |
Resolved since round 3: worldbank E501, repositories E501, ports/autonomy, kernel/values, autonomy I001s, conftest RUF100, invariants lint.

## Edits made by agent T (at coordinator request, outside my no-edit brief — owners please review)
- `tests/integration/test_evidence_fixes.py`: r05 pins `1.1.0`→`1.2.0`, `comtrade-parse-2`→`-3`; r23 and
  implausible-jump bodies use numeric values; estimation-flag test asserts `provider_value_estimated is None` /
  `provider_quantity_estimated` (+ finding `provider_quantity_estimated`) — **code-0 case still needs the
  `isQtyEstimated/isNetWgtEstimated=False` override (C6)**; `_synthetic()` values now carry
  `"basis": {"scale_factor": "1"}`.
- `tests/security/test_http_connectors.py::test_comtrade_hostile_strings_are_inert_data`: expects blockers
  `== {"classification_unreviewed"}` instead of `not blocking`.

## Trend
| | R1 | R2 | R3 | R4 | R5 |
|---|---|---|---|---|---|
| failed / errors | 15 / 4 | 3 / 0 | 20 / 0 (3 genuine) | 1 / 0 | **42 / 0** |
| passed | 1147 | 1411 | 1614 | 1667 | 1840 |
| ruff | 15 | 11 | 14 | 1 | 25 |
| mypy | 1 | 0 | 0 | 0 | 0 |
| UI | green | green | green | green | green |
R4 was nearly green (only an order-dependent backup manifest mismatch); R5's jump is driven by the FX1 R5
governance/eligibility migrations, Comtrade 1.2.0 and AF/HOOK work landing mid-run. The suite was never green
twice in a row.
