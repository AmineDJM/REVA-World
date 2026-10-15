# Test triage (agent T)

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
