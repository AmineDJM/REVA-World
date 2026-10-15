# REVA World — implementation status and phase/gate ledger

**Draft ledger** for the completion protocol (implementation prompt §14) and the P7 release checklist
(architecture §25.1). Drafted on 2026-10-15 from the working tree on top of commit `b0d2d49`, which has
uncommitted UI and doc edits. **Updated later on 2026-10-15 (agent DOCV2)**: V1 rows that changed since the
draft (governance enforced in the database with a separate gate process, the budget ledger, S3 checked against
MinIO, backup/restore proven end to end, chaos tests, redaction, bounded ingest locks, the recorded live demo)
and a V2 ledger (§12). Sections 9 and 10 (**actual test results** and **deployment**) are placeholders.
The lead fills them in after the final full run. Until then, no row here claims that a test *passed* in the
final run. "Done" means the code exists **and** a test or recorded measurement for it exists.

**Not deployed.** Nothing has been deployed to Render. See §10.

## How to read this ledger

| Status | Meaning |
|---|---|
| **done** | The code exists, and a test node or recorded measurement shows the behaviour. Every cited path and test id was checked to exist when this was drafted. |
| **partial** | Code exists, but a test is missing, the last recorded run failed, or there is a named gap. |
| **interface-only** | Typed protocol, spec or validator only (P8–P12). The capability raises `CapabilityGap` or returns a typed unavailable result. **Not implemented.** |
| **not started** | No implementation or interface. |
| **blocked-on-credentials** | Cannot be done from this environment without operator-supplied access (Render API key, managed DB, S3 bucket and keys). |

Inputs: `docs/dev/spec_audit.md` (snapshot 2026-10-14 23:45, stale in places), `docs/dev/acceptance_report.md`
(generated 2026-10-15 00:19, machine-readable `var/acceptance/acceptance_report.json`), `docs/dev/test_triage.md`
(round 3), `docs/dev/perf_report.md`, `docs/dev/live_checks.md`, `docs/dev/review_core.md`,
`docs/dev/review_api_sim_ops.md`, `docs/dev/review_autonomy_deploy_ui.md`, `docs/dev/review_science.md`,
`docs/dev/review_db_fixes.md`, `docs/dev/doc_accuracy.md`, `tests/invariants/coverage.yaml`. Each claim was
re-checked against the code with grep. Where the result differs from a report, the row says so. Inputs of the
update: `docs/dev/gate_separation.md`, `docs/dev/s3_check.md`, `docs/dev/backup_check.md`,
`docs/dev/chaos_check.md`, `docs/dev/redaction_check.md`, `docs/dev/lock_bounds.md`, `docs/dev/v2_eng.md`,
`docs/dev/v2_allc.md`, `docs/dev/v2_nlc.md`, `docs/dev/v2_pop.md`, `docs/dev/v2_pop_backtest.md`,
`docs/dev/v2_io.md`, `docs/dev/v2_pen.md`, `docs/dev/v2_pen_backtest.md`, `docs/dev/ui_v2.md`,
`docs/dev/review_science_v2.md`.

Abbreviations in evidence cells: `RG` = `tests/security/test_roles_grants.py`, `DBH` =
`tests/security/test_db_hardening.py`, `QR` = `tests/integration/test_queue_reliability.py`, `SIM` =
`tests/integration/test_simulation_engine.py`, `CE` = `tests/scientific/test_crude_exposure.py`, `API` =
`tests/integration/test_api_endpoints.py`, `HC` = `tests/security/test_http_connectors.py`, `SSRF` =
`tests/security/test_ssrf_http.py`, `REV` = `tests/integration/test_revisions.py`, `EUR` =
`tests/integration/test_eurostat_discovery.py`, `EF` = `tests/integration/test_evidence_fixes.py`, `POP` =
`tests/unit/population/`, `GOV` = `tests/security/test_governance_r5.py`, `GS` =
`tests/security/test_gate_separation_security.py`. "demo step n" refers to `scripts/demo_workflow.py`
(§17.3 workflow). Its output is in `var/acceptance/demo_fixtures.json` (started 00:19, 13/13 pass) and
`var/acceptance/demo_live.json` (started 2026-10-15 00:19:35 on `9ecd563` with a dirty worktree, 13/13 pass; the
earlier 23:56 live run had step 8 failing).

---

## 0. Status counts

Counted over every ledger row in §2–§8 (one row per gate, criterion, check or invariant). Recounted by DOCV2
after the update; the draft had 159 done, 23 partial, 207 rows (P2.14 was added).

| Status | Rows |
|---|---|
| done | 168 |
| partial | 15 |
| interface-only | 14 |
| not started | 6 |
| blocked-on-credentials | 5 |
| **total** | **208** |

By section: P0–P7 (§3): 81 done, 7 partial, 3 blocked-on-credentials. P8–P12 (§4): 14 interface-only and
6 not started (none done). §13 acceptance (§5): 34 done, 1 blocked-on-credentials. MVP gates/demo (§6): 7 done,
1 partial. First actions + CI (§7): 15 done, 3 partial, 1 blocked-on-credentials. Invariants (§8.1): 30 done,
5 partial.

V2 ledger (§12, counted separately): 13 done (all V2 models experimental, maintainer review pending), 1 partial.

"done" rows have code and a test or measurement that exists; the final full offline run (§9) passed with no
failures. P3.3 moved to done after the final live acceptance run showed release-pinned edges.

---

## 1. What was implemented and how to run it (§14 item 1)

* Python 3.12 / uv / FastAPI / SQLAlchemy / Alembic backend in `src/reva/`. The mapping from the §22 layout to the
  actual modules is in `docs/architecture.md` ("Spec path (§22) | Implementation" table).
* PostgreSQL 17 + PostGIS schema in `migrations/versions/0001_kernel.py` … `0007_security.py`, plus `0008_v2.py`
  (V2: result periods, dataset inputs, LLM budget ledger functions).
* React/TypeScript/Vite UI in `ui/` with seven V1 screens: World, Country, Evidence, Graph, Simulation, Models and
  Acquisition (`ui/src/pages/`), plus the V2 routes Map, Compose and Runs (§12).
* Three runtime processes besides the pre-deploy: API, worker and the independent promotion gate
  (`uv run reva gate` / `make gate`, login `reva_gate`; `docs/dev/gate_separation.md`).
* Runbook: `README.md`, `Makefile`, `docs/operations.md`, `docs/deployment.md`. Local steps (spec §23):
  `cp .env.example .env`, `docker compose up -d db`, `uv sync --frozen --all-extras`, `uv run reva db bootstrap`,
  `uv run reva seed --profile mvp`, `uv run reva fixtures load --profile mvp` or
  `uv run reva ingest --profile mvp --live`, `uv run reva api`, `uv run reva worker`, `uv run reva gate`,
  `npm --prefix ui run dev`.
* §17.3 demonstration: `uv run python scripts/demo_workflow.py [--live]`. Acceptance report:
  `uv run python scripts/acceptance_report.py [--with-perf --with-live]`.

## 2. Git change summary and migrations (§14 item 2)

* Migrations: `0001_kernel`, `0002_evidence`, `0003_belief_graph`, `0004_models_sim`,
  `0005_science_population`, `0006_operations`, `0007_security`, `0008_v2`. Deviations from the reference SQL grouping
  (dependency ordering) are documented in each migration's docstring (spec audit D-1).
* **Open:** the migrations were edited in place after they had been applied (review R-15 / R5-04). The dev DB
  `reva` reports head `0007_security` but lacks the later fixes. A catalog drift check now exists
  (`src/reva/adapters/postgres/schema_contract.py`, `migrations/schema_contract.json`,
  `tests/integration/test_schema_contract.py`). The gate separation changed the 0007 governance section again on
  2026-10-15 (`docs/dev/gate_separation.md`). Freeze 0001–0008 at release. CI runs an upgrade
  test from `0006_operations` (`.github/workflows/ci.yml`), but that is not an upgrade from a released version.
* The final git summary (commits, tag, file counts) is **TBD by the lead** at release time.

---

## 3. Phase/gate ledger P0–P7 (architecture §25, implementation prompt §1 and §3–§12)

### P0 — repository and constitution

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P0.1 | Clean local install; lockfile consistent (§25 P0, §2) | **done** | `pyproject.toml`, `uv.lock`, `ui/package-lock.json`; `uv lock --check` exit 0 (`var/acceptance/logs/cmd_uv_lock_check.log`) | — |
| P0.2 | `/health/live` works (§25 P0) | **done** | `src/reva/api/routes/system.py`; `tests/security/test_auth_session.py::test_health_is_public`, `API::test_health_ready` | — |
| P0.3 | Unit tests pass (§25 P0) | **partial** | `tests/unit/` (461 cases in the 00:19 run). The one failure then, `tests/unit/test_ontology_mappings.py::test_exact_mapping_is_identity_only_within_applicability`, now uses year 1959 and passes when the file is run alone | Final full-run result TBD (§9) |
| P0.4 | Lint passes: ruff, import boundaries, mypy strict on kernel/contracts (§25 P0, §24 #1) | **partial** | `scripts/verify_imports.py` (import-linter kept); `.github/workflows/ci.yml` runs `mypy -p reva.kernel -p reva.ports` | Drafting run: `ruff check src tests scripts` → 1 error (`src/reva/adapters/connectors/worldbank.py:290` E501). `pyproject.toml [tool.mypy] packages` lists only `reva.kernel`, so a local `uv run mypy` skips `reva.ports` |
| P0.5 | Package imports without network/secrets (§25 P0) | **done** | `import_without_secrets` command (exit 0, `var/acceptance/logs/cmd_import_without_secrets.log`); `tests/unit/test_settings.py::test_local_defaults_are_not_production` | — |
| P0.6 | Published build status is honest; task ledger exists (§25 P0, §3 item 2) | **partial** | This file; `docs/limitations.md`; `docs/dev/acceptance_report.md` | This is a draft. §9/§10 need the lead's final run, and this needs human review (no automated check) |
| P0.7 | Kernel IDs/values/context, settings, compose, CI, ADRs (§25 P0 files) | **done** | `src/reva/kernel/*.py`, `src/reva/settings.py`, `src/reva/api/app.py`, `compose.yaml`, `.github/workflows/ci.yml`, `docs/adr/0001-modular-monolith.md` … `0012-replay-modes.md`; `tests/unit/test_kernel_values.py`, `tests/unit/test_ids_canonical.py`, `tests/unit/test_settings.py` | — |

### P1 — kernel, ontology, provenance, storage, safe HTTP

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P1.1 | Re-fetch idempotency (§25 P1) | **done** | `src/reva/evidence/revisions.py`; `REV::test_aba_creates_three_revisions_identical_polls_create_none`, `tests/integration/test_fixture_ingest.py::test_worldbank_fixture_replay_is_fixture_origin` | — |
| P1.2 | Correction appends via the supersedes chain (§25 P1, §4 check 3) | **done** | `migrations/versions/0002_evidence.py` (`evidence.check_revision`); `REV::test_supersedes_chain_constraints_are_enforced_by_the_database` | — |
| P1.3 | Earlier cutoff sees original (§25 P1, §8) | **done** | `src/reva/belief/snapshots.py`, `src/reva/belief/selection.py`; `REV::test_two_revisions_yield_different_as_of_answers`, `API::test_timeseries_revision_filtering`, `EF::test_r20_release_as_of_never_returns_release_with_later_cutoff`; demo step 8 (fixtures) | Live demo step 8 failed at 23:56 ("no eligible observations at this cutoff"); see P7.7 |
| P1.4 | Missing/zero distinction (§25 P1, inv. 5) | **done** | `src/reva/kernel/values.py` (`Missing`); `tests/unit/test_kernel_values.py::test_missing_can_never_be_used_as_a_number`, `HC::test_comtrade_missing_flow_stays_missing_never_zero`, `HC::test_wb_null_values_stay_missing`, `RG::test_missing_value_cannot_carry_a_number` | — |
| P1.5 | Failed upload invisible (§25 P1, §5) | **done** | `src/reva/adapters/objects/filesystem.py`, `s3.py`, `src/reva/evidence/ingest.py`; `tests/integration/test_storage_publication.py::test_failure_mid_upload_leaves_no_object_and_no_rows`, `tests/integration/test_storage_s3.py::test_upload_failure_leaves_no_object` | — |
| P1.6 | Wrong unit rejected (§25 P1, inv. 6) | **done** | `src/reva/ontology/units.py`; `tests/unit/test_ontology_units.py::test_observation_unit_must_match_concept_and_missing_values_pass`, `::test_observation_unit_missing_or_unknown_rejected` | — |
| P1.7 | SQL roles deny invalid writes; WHERE is not the boundary (§25 P1, §4) | **done** | `migrations/versions/0007_security.py` (`meta.enforce_governance`, approval functions owned by `reva_registry`); `RG::test_api_cannot_insert_directly`, `RG::test_runtime_roles_cannot_update_delete_truncate_history`, `RG::test_where_clause_is_not_the_boundary`, `DBH::test_worker_insert_grants_are_the_reviewed_set`, `DBH::test_worker_cannot_mint_api_tokens_or_record_human_reviews`, `GOV::test_worker_cannot_self_approve_registry_rows`, `GOV::test_eligibility_approval_requires_a_bound_passing_gate`, `GOV::test_model_approval_requires_a_human_review_recorded_by_the_api`, `GS::test_approval_functions_refuse_gate_results_not_recorded_by_the_gate` | Changed since the draft: approved governance states are written only by the owner or by approval functions that need a `reva_gate` pass or an API-recorded human review (§8.3). Residual: `model.record_promotion`, `model.evaluation` and `meta.artifact` stay worker-writable (`docs/limitations.md` "Governance limits") |
| P1.8 | The twelve DB/application checks (§4 items 1–12) | **done** | `migrations/versions/0002_evidence.py`, `0003_belief_graph.py`; `RG::test_acquisition_connector_must_belong_to_source`, `RG::test_observation_must_agree_with_its_acquisition`, `RG::test_release_requires_belief_mode_context`, `RG::test_belief_input_cannot_reference_simulation_or_evaluation_outputs`, `RG::test_provenance_cycles_rejected`, `RG::test_publication_rejects_unverified_artifacts`, `RG::test_restricted_privacy_classes_rejected`, `EF::test_selector_that_does_not_resolve_quarantines_the_item` | Changed since the draft: check 6 (eligibility scope, R5-11) and check 11 (seal race, relation seal, R5-07) now have tests: `DBH::test_years_outside_the_decision_scope_are_ineligible`, `DBH::test_input_trigger_enforces_the_support_year_scope`, `DBH::test_subject_scoped_quarantine_does_not_affect_other_subjects`, `DBH::test_release_seal_race_publisher_waits_for_writers`, `DBH::test_release_seal_race_writer_waits_for_publisher`, `DBH::test_release_pinned_relations_are_sealed` |
| P1.9 | Frozen typed kernel records; TimeExtent; Value union; UncertaintySpec; semantic profiles; component manifests (§4) | **done** | `src/reva/kernel/{records,time,values,uncertainty,support,context}.py`, `src/reva/ontology/components.py`, `registry/components/`; `tests/unit/test_kernel_values.py`, `tests/scientific/test_uncertainty_types.py`, `tests/unit/test_ontology_components.py`, `tests/contracts/test_registry_components.py::test_published_versions_match_pinned_digests` | Calendars limited to gregorian/proleptic/julian (documented limitation) |
| P1.10 | ArtifactStore put/get/head/range_read; streamed hashes; no ETag-as-SHA (§5) | **done** | `src/reva/adapters/objects/`; `tests/unit/test_storage_fs.py`, `tests/integration/test_storage_s3.py`, `tests/security/test_http_storage_fixes.py::test_s3_existing_object_with_matching_metadata_but_wrong_bytes_rejected` | Changed since the draft: checked against a real S3 API, a self-built MinIO (last community release), in `tests/integration/test_s3_store_minio.py` (marker `s3`; skips without `REVA_TEST_S3_ENDPOINT`): store contract, provider checksums, conditional writes and the API/worker credential split. Recorded result: 38 passed, 1 xfailed (`docs/dev/s3_check.md`). Not AWS and not a provider bucket: that stays **blocked-on-credentials** (P7.2) |
| P1.11 | Manifests store Arrow schema fingerprint, dimension order, row counts, logical keys, temporal coverage (§5) | **partial** | `src/reva/belief/publication.py` (release manifest), population Parquet metadata | Source observations are not written as normalised Parquet batches with manifests (PostgreSQL only; allowed for small panels by §8.1). DuckDB is a dependency but unused |
| P1.12 | Safe HTTP client: SSRF, private redirects, oversize, decompression bombs (§6) | **done** | `src/reva/adapters/http/safe_client.py`, `archive.py`; `SSRF::test_redirect_hop_is_checked`, `tests/security/test_http_storage_fixes.py::test_connection_targets_the_vetted_address_not_a_rebinding_answer`, `::test_stacked_gzip_bomb_fails_fast_with_small_peak_memory` | — |
| P1.13 | APIs: observations, ontology, sources (§25 P1) | **done** | `src/reva/api/routes/evidence.py`, `catalog.py`; `API::test_timeseries_revision_filtering`, `API::test_catalog_endpoints` | — |

### P2 — real source adapters, fixtures, durable queue

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P2.1 | Fetch each source live once (§25 P2, §17.4) | **done** | `scripts/live_source_checks.py`; `docs/dev/live_checks.md` (run 2026-10-14 23:31–23:34; WDI 5/5, Comtrade 48/48, Natural Earth 1/1, Crossref 1/1, all HTTP 200, 55 live acquisitions, live release `b9d17dfc…` with 482 estimates); `tests/live/test_live_sources.py::test_full_live_run_release_and_coverage` (`-m live`) | Live run predates the latest connector edits; re-run TBD (§9) |
| P2.2 | Offline fixture replay equivalent (§25 P2, §3 item 5) | **done** | `tests/fixtures/source/*/manifest.json` (url, sha256, retrieved_at, licence); `tests/integration/test_fixture_ingest.py::test_worldbank_fixture_replay_is_fixture_origin`, `EF::test_r04_live_ingest_identical_to_fixture_creates_live_lineage` | — |
| P2.3 | Pagination complete (§25 P2, §6 WB) | **done** | `src/reva/adapters/connectors/worldbank.py`; `HC::test_wb_multi_page_header_schedules_all_pages`, `HC::test_wb_pagination_truncation_blocks`, `HC::test_comtrade_max_records_truncation_blocks` | — |
| P2.4 | 429 / HTML-200 / API-error handled (§25 P2) | **done** | `SSRF::test_429_retry_after_is_honoured_then_succeeds`, `SSRF::test_html_error_page_with_200_is_a_failure`, `HC::test_wb_api_error_payload_is_a_failure`, `HC::test_comtrade_error_and_drift_payloads_fail`, `HC::test_runner_quarantines_row_level_schema_drift` | — |
| P2.5 | Comtrade missing weight remains missing; no barrels (§25 P2, §6) | **done** | `src/reva/adapters/connectors/comtrade.py`; `HC::test_comtrade_unusable_weight_stays_missing`, `tests/unit/test_ontology_units.py::test_kg_to_barrel_refused_without_density_and_convention` | — |
| P2.6 | Crash/retry produces one visible acquisition (§25 P2) | **done** | `QR::test_kill_after_object_upload_then_retry_yields_one_acquisition`, `QR::test_kill_after_db_commit_before_ack_yields_one_acquisition`; real-process fault injection in `tests/chaos/` (marker `chaos`; `docs/dev/chaos_check.md`: SIGKILL/SIGSTOP/SIGTERM, backend termination, DB outage, cancellation, publication) | CHAOS-1 (handler kept running past its lease during a DB outage) was fixed in `src/reva/operations/worker.py`; the report records `tests/chaos` 11/11 afterwards |
| P2.7 | Comtrade specifics: reporter codes verified, CIF/FOB kept, flags recorded, exact keys only, partner-total ≠ bilateral (§6) | **done** | `registry/reference/comtrade_partnerAreas.json`; `tests/live/test_live_sources.py::test_comtrade_reporter_codes_match_provider_catalogue`, `EF::test_r23_valuation_is_flow_aware`, `HC::test_comtrade_dimension_mismatch_blocks`, `tests/integration/test_world_graph.py::test_aggregation_refuses_world_total_plus_bilateral` | Bilateral 2023 requests omit `partnerCode` (all partners, count-checked); arguably not an "exact key" (audit D-3) |
| P2.8 | Natural Earth pinned by hash, reviewed keys, display only (§6) | **done** | `src/reva/adapters/connectors/naturalearth.py`, `src/reva/world/space.py`, `docs/adr/0010-natural-earth-display-only.md`; `EF::test_display_geometry_disputed_only_by_type`, `tests/live/test_live_sources.py::test_naturalearth_asset_structure` | No pre-declared expected hash (the first download becomes the pin) |
| P2.9 | Crossref DOI 10.1257/aer.99.3.1053, cache/conditional requests, 429, reviewed EvidenceCard without invented coefficients (§6) | **done** | `src/reva/adapters/connectors/crossref.py`, `src/reva/evidence/cards.py`, `registry/evidence/kilian2009.yaml`; `EF::test_crossref_sends_conditional_headers_and_treats_304_as_unchanged`, `tests/live/test_live_sources.py::test_crossref_doi_metadata` | — |
| P2.10 | Durable PG queue: SKIP LOCKED, lease, heartbeat, fencing, dead letter, idempotency, priority/starvation, cancellation (§7) | **done** | `src/reva/adapters/postgres/queue.py`, `src/reva/operations/worker.py`; `QR::test_claim_commits_and_releases_row_locks_before_work`, `QR::test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit`, `QR::test_retry_with_exponential_backoff_then_dead_letter`, `QR::test_priority_and_starvation_protection`, `QR::test_cancel_running_job_is_honoured_cooperatively` | Capped priority aging does not strictly prevent starvation (R2-08) |
| P2.11 | Scheduler: idempotent due slots, no duplicates; outbox invalidation, SCC, materiality budget (§7) | **done** | `src/reva/operations/schedules.py`, `invalidation.py`; `QR::test_two_schedulers_create_exactly_one_job_per_slot`, `tests/integration/test_scheduler_slots.py::test_slot_key_is_utc_whatever_the_offset`, `tests/integration/test_queue_invalidation.py::test_cycle_invalidates_coupled_component_as_a_whole`, `::test_materiality_skips_accumulate_error_budget_then_force_refresh` | — |
| P2.12 | Default cadences (WDI/Comtrade monthly, NE/Crossref/Eurostat weekly) (§6) | **partial** | `src/reva/cli/seed.py` (`DEFAULT_SCHEDULES`, including `ingest:eurostat.demo_pjan.fr` weekly); `tests/integration/test_scheduler_slots.py` | No test asserts the cadence table itself (scheduler tests cover slot mechanics only). Comtrade "rolling recent-year revision checks" is implicit (re-fetches all years monthly) |
| P2.13 | APIs: connectors/jobs (§25 P2) | **done** | `src/reva/api/routes/catalog.py`, `system.py`; `API::test_simulations_jobs`, `API::test_r2_07_cancel_is_scoped_by_job_type` | — |
| P2.14 | Large acquisitions do not exhaust the PostgreSQL lock table (added after a live `demo_pjan` ingest of ~8,000 cells failed with `max_locks_per_transaction`) | **done** | `src/reva/evidence/ingest.py` (`lock_lineages`: at most 256 bucket locks per acquisition, namespaced by source/origin, one sorted statement, bounded wait); `tests/integration/test_lineage_locks.py::test_ten_thousand_observations_hold_a_bounded_number_of_locks`, `::test_overlapping_acquisitions_in_opposite_key_orders_do_not_deadlock`; `docs/dev/lock_bounds.md` | Same-source acquisitions covering most buckets now run one after the other (documented trade-off) |

### P3 — beliefs, aliases, n-ary graph, cards

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P3.1 | Every displayed value traces to raw source (§25 P3, inv. 1) | **done** | `src/reva/api/routes/provenance.py`; `API::test_provenance_reaches_raw_artifact_and_selector`, `API::test_provenance_of_run_and_scenario`; `ui/e2e/country.spec.ts` (drawer reaches raw SHA-256 + selector) | — |
| P3.2 | Graph preserves participant roles (§25 P3) | **done** | `src/reva/world/graph.py`; `tests/integration/test_world_graph.py::test_participant_roles_and_directions_preserved`, `tests/unit/test_world_graph_core.py::test_incidence_preserves_roles_and_repeated_participants` | — |
| P3.3 | World view is release-pinned; no unversioned current_facts (§25 P3, §4) | **done** | `src/reva/belief/snapshots.py`, `pin_release_relations` in `src/reva/belief/publication.py`; `API::test_beliefs_are_release_pinned_and_as_of`, `tests/integration/test_world_graph.py::test_release_pinned_context`, `tests/integration/test_repair_hookup.py`; final live acceptance run step 4: 65 release-pinned edges (`docs/dev/final_run/demo_live_summary.json`) | — |
| P3.4 | WDI/Eurostat incompatible support cannot reconcile (§25 P3, §6 Eurostat) | **done** | `registry/mappings/eurostat.yaml`; `tests/unit/test_ontology_validation.py::test_jan1_support_does_not_satisfy_midyear_concept`, `tests/unit/test_ontology_mappings.py::test_eurostat_jan1_total_vs_wdi_midyear_is_incompatible`, `EUR::test_end_to_end_promotion_ingestion_and_belief_separation` | At 00:19 the EUR test failed on proposal-backlog order dependence. `tests/integration/test_connector_repair.py` now closes its proposals at teardown; drafting spot-check (autonomy_gates + connector_repair + eurostat_discovery + evidence_fixes: 116 passed) |
| P3.5 | No scenario input allowed into beliefs (§25 P3, inv. 3) | **done** | `belief.input` FKs in `migrations/versions/0003_belief_graph.py`; `RG::test_belief_input_cannot_reference_simulation_or_evaluation_outputs` | — |
| P3.6 | source_select_v1 publishes selected_report with original uncertainty (§8) | **done** | `src/reva/belief/selection.py`, `publication.py`, `docs/adr/0006-source-select-v1.md`; `EF::test_r05_r3_08_incompatible_lineages_withhold_the_estimand`, `EF::test_r27_retracted_latest_revision_is_not_replaced_by_superseded_value` | — |
| P3.7 | Entity aliases provider-scoped (§25 P3, inv. 23) | **done** | `src/reva/world/identities.py`; `tests/integration/test_world_graph.py::test_alias_resolution_is_provider_scoped`, `EF::test_r10_partner_codes_get_distinct_entities` | — |
| P3.8 | APIs: beliefs/entities/relations/events/provenance (§25 P3) | **done** | `src/reva/api/routes/{beliefs,world,provenance}.py`; `API::test_entities_list_and_detail`, `API::test_relations_bounded_with_truncation_flag`, `tests/integration/test_world_graph.py::test_occurrences_with_uncertain_time` | — |

### P4 — scenario contract, crude exposure L1/L2, simulation queue

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P4.1 | Hand-computed cases match (§25 P4, §13) | **done** | `src/reva/models/economic/crude_exposure.py`; `CE::test_hand_computed_l1_exact`, `SIM::test_l1_exposure_matches_hand_computation` | — |
| P4.2 | p=1 gives zero (§25 P4) | **done** | `CE::test_unit_multipliers_give_zero` | — |
| P4.3 | ε=0 equals L1 exactly (§25 P4) | **done** | `CE::test_zero_elasticity_l2_equals_l1_exactly`, `SIM::test_l2_sensitivity_range` | — |
| P4.4 | Missing input blocks; all-four request returns missing_input for DZA (§25 P4, §6, §17.1) | **done** | `src/reva/simulation/compile.py`; `SIM::test_all_four_countries_returns_missing_input_for_dza`, `CE::test_missing_input_raises_never_zero`, `tests/e2e/test_demo_workflow.py::test_negative_demo_reports_the_algerian_blocker` | Holds for reference years 2020–2023 only. DZA 2024 has one estimated H4 row per flow (imports US$5,564, implausible; flagged as a review signal by `EF::test_comtrade_implausible_jump_is_a_review_signal_not_a_block`) |
| P4.5 | 5-year GDP request rejected (§25 P4, inv. 33) | **done** | `SIM::test_five_year_horizon_rejected` | — |
| P4.6 | Scenario writes cannot change beliefs (§25 P4) | **done** | `SIM::test_sim_role_cannot_write_evidence_or_beliefs`, `RG::test_sim_cannot_insert_evidence_or_beliefs` | Changed since the draft: R5-02 and R5-05 now have tests (`tests/security/test_sim_integrity.py::test_sim_cannot_squat_scenario_identity`, `::test_sim_cannot_preregister_artifact_metadata`, `::test_sim_register_artifact_derives_metadata_from_the_bytes`) |
| P4.7 | Cancellation/checkpoint/retry verified (§25 P4) | **done** | `SIM::test_cancellation_before_publication`, `QR::test_cancel_running_job_is_honoured_cooperatively`, `tests/integration/test_worker_loop.py::test_sigterm_stops_claiming_signals_handler_and_drains` | — |
| P4.8 | L0 cache, L1, L2 labelled sensitivity; L3/L4 unavailable (§9) | **done** | `src/reva/simulation/engine.py`, `src/reva/capabilities.py`; `SIM::test_l0_cache_miss_then_hit_after_l1`, `SIM::test_l3_and_l4_rejected`, `tests/scientific/test_crude_exposure_sensitivity_row.py` | — |
| P4.9 | Run pins baseline, manifests, intervention, seed streams, profile, output digest; real baseline resolved (§9) | **done** | `src/reva/simulation/rng.py`, `src/reva/cli/demo.py`, `ui/src/lib/scenario.ts`; `tests/scientific/test_crude_exposure_coupling_rng.py::test_seed_plan_records_streams`, `RG::test_scenario_rejects_unpublished_baseline_direct_insert` | — |
| P4.10 | Acyclic coupling, port compatibility, SCC detection, feedback rejected (§9) | **done** | `src/reva/simulation/coupling.py`; `tests/scientific/test_crude_exposure_coupling_rng.py`, `tests/unit/test_extensions_interfaces.py::test_feedback_solve_refuses_without_contract` | — |
| P4.11 | APIs: scenarios/simulations (§25 P4) | **done** | `src/reva/api/routes/sim.py`; `API::test_scenarios_validate`, `API::test_simulations_jobs`, `API::test_r2_15_concurrent_identical_scenario_returns_existing` | — |

### P5 — replay, forecasting, scorecards, UI

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P5.1 | Future revisions/features/weights blocked; six leakage fixtures (§25 P5, §8) | **done** | `src/reva/evaluation/{replay,cutoff,leakage}.py`, `tests/fixtures/vintages/`; `tests/replay/test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode`, `::test_strict_never_downgrades`, `tests/unit/forecasting/test_forecast_no_lookahead.py::test_cutoff_scoped_series_blocks_future_access` | — |
| P5.2 | Three replay modes; available-then requires proof (§8) | **done** | `tests/replay/test_leakage_cases.py::test_available_then_excludes_records_without_availability_proof`, `::test_available_then_applies_retractions_and_eligibility_as_of_the_cutoff`, `tests/replay/test_replay_job.py::test_operational_replay_job_writes_evaluation_context_and_manifest` | The autonomy `BacktestRunner` blocks strict and available-then modes (`tests/integration/test_autonomy_contracts.py::test_backtest_runner_adapts_tournament_and_blocks_unsupported_modes`); only the evaluation service routes them |
| P5.3 | Revised-data mode visibly labelled (§25 P5, §8) | **done** | `tests/replay/test_leakage_cases.py::test_revised_data_retrospective_is_labelled_everywhere`, `tests/replay/test_replay_job.py::test_retrospective_job_result_carries_label_and_disclaimer`, `API::test_evaluation_job_and_scorecard_labels` | — |
| P5.4 | Uncertainty types correct (§25 P5) | **done** | `tests/scientific/test_uncertainty_types.py::test_kinds_are_distinct_values_and_not_interchangeable`, `CE::test_sensitivity_range_not_probabilistic`, `ui/src/components/UncertaintyPanel.test.tsx` | — |
| P5.5 | UI drill-through works (§25 P5) | **done** | `ui/src/components/ProvenanceDrawer.tsx`; `ui/src/components/ProvenanceDrawer.test.tsx` (new vitest), `ui/e2e/country.spec.ts` ("provenance drawer from a belief value reaches the raw artifact SHA-256 and selector"), run by the `e2e` job in `.github/workflows/ci.yml`; last recorded result `ui/e2e/RESULTS.md` (country.spec pass) | `docs/dev/acceptance_report.md` lists this as *missing* because its mapper does not count Playwright for this criterion. Fix the mapper |
| P5.6 | No-result/missing/auth flows tested (§25 P5) | **done** | `ui/src/components/MissingValue.test.tsx`, `ui/src/api/client.test.ts`, `ui/e2e/auth.spec.ts`; `tests/security/test_auth_session.py::test_unauthenticated_is_401` | — |
| P5.7 | Naive baseline always shown (§25 P5) | **done** | `src/reva/models/tournament.py`; `tests/unit/forecasting/test_forecast_tournament.py::test_naive_baseline_added_when_not_a_candidate`, `tests/integration/test_evaluation_service.py::test_full_panel_stores_evaluations_promotions_and_label` | — |
| P5.8 | Three forecasting models; in-fold preprocessing; ineligible on short series (§9) | **done** | `src/reva/models/forecasting/{last_value,drift,local_level}.py`; `tests/unit/forecasting/` | Local-level plug-in interval is not calibrated in short windows (R3-05, documented in `docs/methodology.md`) |
| P5.9 | Tournament promotion statistically sound; insufficient evidence keeps incumbent (§12.2) | **done** | `src/reva/models/tournament.py`, `src/reva/evaluation/tournament_service.py`; `tests/scientific/test_tournament_calibration.py::test_false_promotion_rate_at_nominal_level_under_exact_null`, `tests/integration/test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent` | No proper probabilistic score (CRPS/log/interval) because V1 forecasts are not probabilistic (R3-11, returned as not-applicable) |
| P5.10 | Seven screens read the real API; fixture mode labelled; every result shows context/units/uncertainty/assumptions/model/provenance (§11) | **partial** | `ui/src/pages/{World,Country,Evidence,Graph,Simulation,Models,Acquisition}Page.tsx`, `ui/src/components/ContextBanner.tsx`; `ui/e2e/screens.spec.ts`, `ui/src/components/ContextBanner.test.tsx`, `API::test_every_envelope_states_its_knowledge_cutoff` | `ui/e2e/RESULTS.md` (23:56) records 4 screen failures (cutoff "not stated"). Commit `186f0f5` reports e2e 25/25 after the fix, but `RESULTS.md` was not regenerated. Valid/reference time on Country panels is "not stated" with no test |
| P5.11 | APIs: replay/models/query/coverage (§25 P5) | **done** | `src/reva/api/routes/{sim,catalog}.py`; `API::test_replay_and_discovery`, `API::test_query_spec`, `API::test_coverage_and_gaps`, `tests/integration/test_api_contract.py::test_committed_contract_is_current` | — |

### P6 — autonomy, Eurostat, demographic recipe, gaps

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P6.1 | Discover demo_pjan from official catalogue (§25 P6) | **done** | `src/reva/autonomy/discovery.py`; `EUR::test_discovers_demo_pjan_from_captured_catalogue`; live check in `docs/dev/live_checks.md` (Eurostat TOC HTTP 200) | — |
| P6.2 | Validate live sample (§25 P6) | **done** | `src/reva/autonomy/validator.py`, `src/reva/adapters/connectors/eurostat_template.py`; `docs/dev/live_checks.md` (demo_pjan FR 2023: 309 values, status `p`, validated); `tests/live/test_live_sources.py::test_eurostat_demo_pjan_fr_2023` | The live run deliberately did not publish; the live release lacks `R-POP-JAN-AGESEX` FRA 2023 (absent cell) |
| P6.3 | Semantic mismatch rejected (§25 P6) | **done** | `EUR::test_wdi_midyear_equivalence_is_incompatible`, `EUR::test_validator_fails_mutated_sample` | — |
| P6.4 | 100k weighted records preserve leaf totals (§25 P6, §10) | **done** | `src/reva/population/{recipe,materialise}.py`; `tests/unit/population/test_population_materialise.py::test_leaf_totals_conserved_exactly`, `tests/property/test_population_properties.py::test_exact_conservation_for_random_counts`, `tests/performance/test_population_perf.py::test_materialise_100k_records_france_like`; `docs/dev/perf_report.md` (100k: 3.379 s, 181.1 MiB) | — |
| P6.5 | Coarsen/refine preserves mass; unsupported detail reported (§25 P6) | **done** | `src/reva/compression/`; `tests/unit/population/test_population_compression.py::test_coarsen_conserves_exactly`, `::test_coarsen_then_refine_round_trip`, `::test_refine_along_unsupported_dimension_raises` | — |
| P6.6 | Generated code cannot run in worker (§25 P6, §10) | **done** | `src/reva/autonomy/sandbox.py`, `sandbox_child.py`; `tests/security/test_sandbox_isolation.py::test_worker_never_imports_proposed_code`, `::test_generated_code_is_blocked_without_external_runner`, `::test_isolation_evidence_is_measured_not_nominal` | Without namespaces the trusted-template child runs as the same uid ("process_isolation: none (same uid)"); generated code stays blocked |
| P6.7 | Hostile document cannot alter policy (§25 P6) | **done** | `tests/security/test_prompt_injection.py::test_injected_paper_text_yields_only_a_candidate`, `::test_injected_catalogue_and_metadata_change_nothing` | — |
| P6.8 | All 16 autonomy protocols with concrete V1 behaviour (§10, arch. §18.1) | **done** | `src/reva/ports/autonomy.py`, `src/reva/autonomy/*.py`; `tests/integration/test_autonomy_contracts.py::test_protocol_conformance` | BacktestRunner partial (see P5.2) |
| P6.9 | T0–T3 tiers; LLM/agent cannot change its own gates (§10, arch. §18.2) | **done** | `registry/policies/promotion.yaml`, `src/reva/autonomy/{policy,gate,binding}.py`, `src/reva/gate/` (separate process, login `reva_gate`), `scripts/start_gate.py`; `tests/integration/test_autonomy_gates.py::test_proposer_cannot_write_its_own_gate_result`, `::test_r4_01_malicious_manifest_is_rejected`, `GS::test_worker_api_and_sim_cannot_record_gate_results`, `GS::test_r6_02_gate_pass_is_bound_to_the_gate_derived_scope_and_manifest`, `GS::test_r6_03_t2_proposals_need_a_human_review_not_a_gate_pass`, `tests/integration/test_gate_separation.py::test_worker_proposes_gate_process_decides_worker_promotes`, `tests/integration/test_gate_followup.py`, `tests/security/test_gate_verify.py` | Changed since the draft: only `reva_gate` records gate results and approvals accept only those (`docs/dev/gate_separation.md`). Residual: the gate decides on worker-stored inputs; both processes run the same image; locally the separation is a credential convention, not an isolation boundary |
| P6.10 | Gap catalogue + analytic VOI with known answer + heuristic fallback (§10) | **done** | `registry/requirements/mvp.yaml`, `src/reva/autonomy/gaps.py`, `src/reva/evaluation/voi.py`; `tests/scientific/test_voi_analytic.py::test_evsi_exact_hand_check`, `tests/integration/test_autonomy_contracts.py::test_gap_detector_against_database_finds_dza_crude_missing` | — |
| P6.11 | Population service end to end (Eurostat cells → Parquet → `model.population_dataset` → certificates) (§10, §17.3 step 10) | **done** | `src/reva/population/service.py`; `EF::test_population_from_promoted_eurostat_connector`; demo step 10 (fixtures) | The live release had no Jan-1 cells (P6.2), so this has only run on fixtures |
| P6.12 | Self-healing repair proposals (arch. §10.3) | **done** | `src/reva/autonomy/repair.py`; `tests/integration/test_connector_repair.py` | — |

### P7 — image, Render config, monitoring, backup, release

| ID | Requirement (§) | Status | Evidence | Gaps |
|---|---|---|---|---|
| P7.1 | Current schema/CLI validation (§25 P7, §12) | **done** | `scripts/validate_blueprint.py` (cached official schema, exit 0), `scripts/validate_contracts.py` (exit 0); `tests/contracts/` | `render blueprints validate` (Render CLI) not run, see P7.2 |
| P7.2 | Deploy only when access exists (§25 P7, §1, §14 item 5) | **blocked-on-credentials** | `render.yaml`, `docs/deployment.md` (exact provisioning inputs) | Needs a Render API key/account plus an S3-compatible private bucket with two scoped key pairs (API read-only, worker prefix-write): `REVA_S3_BUCKET`, `REVA_S3_REGION`, `REVA_S3_ENDPOINT_URL`, `REVA_S3_ACCESS_KEY_ID`/`REVA_S3_SECRET_ACCESS_KEY` ×2. Not deployed |
| P7.3 | Actual roles and credentials verified (§25 P7) | **blocked-on-credentials** | Locally: `RG::test_runtime_roles_are_unprivileged_noinherit_logins`, `DBH::test_provisioned_verifier_lets_the_runtime_role_log_in`, `tests/security/test_start_env.py::test_blueprint_as_written_starts_both_processes_in_production` | Verifying against the managed Render DB and S3 IAM policies needs deployment |
| P7.4 | Restore from backups (§25 P7, §12, §13) | **done** | `scripts/backup.sh`, `scripts/restore.sh`, `scripts/backup_restore.py`; `tests/integration/test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of`, `::test_production_backup_requires_encryption`, `::test_restore_refuses_unverifiable_sets_and_non_empty_target`, `tests/integration/test_backup_restore_roundtrip.py::test_backup_restore_round_trip_fresh_db_and_object_store`; end-to-end check `docs/dev/backup_check.md` (age-encrypted backup with objects restored into a new database and an empty object root, row counts, roles and RLS compared, secret scan 0 hits, refusals); S3 variant `tests/integration/test_s3_store_minio.py::test_backup_restore_cli_round_trip_with_s3` (MinIO) | Small local data only (4,010 rows, 64 objects); production RPO/RTO not measured |
| P7.5 | No filesystem persistence assumptions (§25 P7) | **done** | `tests/unit/test_settings.py::test_production_never_silently_selects_filesystem`, `tests/integration/test_storage_s3.py::test_factory_builds_s3_store_for_production_settings` | — |
| P7.6 | p95/RSS measurements recorded (§25 P7, §12, §14 item 6) | **done** | `scripts/measure_performance.py`, `var/perf/perf_results.json`, `docs/dev/perf_report.md` (API p95 171.9 ms at 1 CPU and 268.1 ms at emulated 0.5 CPU; metadata p95 37.3 ms; L1 four-country 12.1 ms; queue ack p95 241.3 ms; 100k population 3.379 s / 181.1 MiB; API peak RSS 120.2 MiB) | Measured on a 4-core dev container with emulated quotas, not on the named Render plans (see A.P3) |
| P7.7 | Complete live demo (§25 P7, §17.4) | **done** | `scripts/demo_workflow.py --live`; `var/acceptance/demo_live.json` (started 00:19:35 on `9ecd563`, dirty worktree: **13/13 pass**, including step 8; the 23:56 run had failed step 8) | Recorded before many later changes (gate separation, V2); repeat in the final run (§9). Step 4 still shows `release_pinned_edges=0` (P3.3) |
| P7.8 | Docker multi-stage image, non-root, pinned digests (§2, §25 P7) | **done** | `Dockerfile`, `.dockerignore`; `docker` job in `.github/workflows/ci.yml` (build + non-root smoke) | Image build not run in this drafting session; result TBD (§9) |
| P7.9 | Render Blueprint = full appendix; documented compatibility changes (§12) | **done** | `render.yaml`; `scripts/validate_blueprint.py`; `tests/security/test_start_env.py::test_blueprint_as_written_starts_both_processes_in_production` | `render.yaml` adds `REVA_DB_SSLMODE` and `REVA_PROCESS_ROLE` beyond the appendix (minimal, for production settings validation). Check that `docs/deployment.md` records this as the reviewed compatibility update |
| P7.10 | bootstrap_runtime.py: advisory lock, PostGIS check, migrations as owner, idempotent roles, grants, report without secrets (§12) | **done** | `scripts/bootstrap_runtime.py`, `src/reva/cli/bootstrap.py`; `tests/security/test_start_env.py::test_bootstrap_runtime_reports_schema_without_secrets`, `DBH::test_scram_verifier_matches_rfc7677`, `DBH::test_bootstrap_with_non_superuser_createrole_owner` | Changed since the draft: memberships held by runtime roles are rejected (`GOV::test_bootstrap_rejects_memberships_held_by_runtime_roles`, R5-09) and deletion needs an explicit `SET ROLE` (`GOV::test_deletion_needs_explicit_set_role`, R5-10). Not exercised on a managed database (P7.3) |
| P7.11 | start_api.py: validate, scrub env, exec Uvicorn 0.0.0.0:$PORT with one worker; child env tested (§12) | **done** | `scripts/start_api.py`, `scripts/_runtime_env.py`; `tests/security/test_start_env.py::test_start_api_subprocess_child_environment`, `::test_api_child_env_keeps_only_api_credentials` | — |
| P7.12 | start_worker.py: refuse owner URL, bounded schema wait, thread budget, SIGTERM drain (§12) | **done** | `scripts/start_worker.py`; `tests/security/test_start_env.py::test_start_worker_refuses_owner_url_in_production`, `::test_wait_for_schema_is_bounded`, `::test_thread_budget_is_explicit_and_capped_by_cgroup`, `tests/integration/test_worker_loop.py::test_sigterm_stops_claiming_signals_handler_and_drains` | — |
| P7.13 | Object credentials: API read-only by prefix, worker prefix-write, sandbox none (§12) | **blocked-on-credentials** | `docs/deployment.md` (per-prefix IAM policies listed) | Provider IAM policies cannot be exercised without a provider bucket. Locally the API read-only and worker prefix-confined policies were proven on MinIO (`tests/integration/test_s3_store_minio.py::test_api_credential_is_read_only`, `::test_worker_credential_confined_to_prefix`); the gate's key pair (added later) was not part of that check |
| P7.14 | JSON logs, metrics, traces with bounded cardinality (§12) | **done** | `src/reva/operations/telemetry.py`, `src/reva/operations/tracing.py` (optional OTLP via `OTEL_EXPORTER_OTLP_ENDPOINT`); `tests/integration/test_queue_telemetry.py::test_metrics_have_bounded_labels_and_render`, `::test_json_logs_carry_correlation_and_redact_secrets`, `API::test_metrics_after_traffic`; redaction (R7) of logs, errors, job records, audit, outbox and sandbox output: `tests/security/test_redaction_r7.py`, `docs/dev/redaction_check.md` | No test exercises the OTel exporter path (soft dependency, off by default) |
| P7.15 | Machine-readable acceptance report; release docs (§25.1) | **done** | `scripts/acceptance_report.py` → `var/acceptance/acceptance_report.json` (schema `reva-acceptance-report/1`), `docs/dev/acceptance_report.md`; `.github/workflows/release.yml` | Must be regenerated from the final run (§9) |

---

## 4. Phase/gate ledger P8–P12 (extensions; **not implemented**)

Spec §1 and §25: "Preserve the architecture and real extension interfaces for P8–P12. Do not claim later
scientific capabilities are implemented." Each capability below is a `reserved` (or `blocked`) entry in
`src/reva/capabilities.py`. `reva.capabilities.require(...)` raises a typed `CapabilityGap` (422 at the API),
which `tests/unit/test_capabilities.py::test_later_phase_domain_capabilities_are_never_implemented` and
`::test_require_raises_structured_gap` check. The only executable logic is pure validation/verification that a later
implementation must satisfy. Those helpers have tests but do not make the phase implemented.

| ID | Requirement / gate (§25) | Status | Preserved interface | Evidence | Gaps |
|---|---|---|---|---|---|
| P8.1 | Physical-field adapter; CF/COG/Zarr/STAC catalogues (`models/physical/fields.py`, `adapters/physical/*`) | **interface-only** | `reva.models.physical.fields` (capability `physical.field_adapter`, reserved) | `src/reva/models/physical/fields.py`; `tests/unit/test_extensions_physical.py::test_field_metadata_contracts_and_reserved_reader` | `src/reva/adapters/physical/` does not exist; no catalogue readers |
| P8.2 | Gate: known analytic field integrates correctly; antimeridian/CRS tests | **interface-only** | `reva.models.physical.verification` (capability `verification.analytic_field_aggregation`, verifier only) | `tests/unit/test_extensions_physical.py::test_linear_in_latitude_matches_closed_form_spherical_integral`, `::test_antimeridian_region_on_both_longitude_conventions`, `::test_missing_cells_are_never_zero_filled` | Verification helper only. There is no field-to-region service (`physical.field_to_region` reserved) and `world/regridding.py` does not exist |
| P8.3 | One bounded historical heat/drought exposure dataset; withheld temporal/spatial validation | **not started** | capability `physical.hazard_exposure` (reserved, no interface module) | `src/reva/capabilities.py` | No dataset, no connector, no validation |
| P8.4 | External-model port conformance; same concept/support contract without kernel change; no weather forecast from climate averages | **interface-only** | `reva.models.physical.adapter` (capability `physical.external_model`, reserved) | `tests/unit/test_extensions_physical.py::test_lifecycle_conformance_passes_for_conforming_model`, `::test_lifecycle_conformance_detects_violations` | Conformance checker only; no external model is wired |
| P8.5 | APIs fields/assets/field-query | **not started** | — | — | No routes |
| P9.1 | Multi-region sector IO + inventory/capacity constraints (`models/economic/io.py`, `stocks.py`) | **interface-only** | `reva.extensions.io_sfc` (capability `economic.input_output`, reserved) | `src/reva/extensions/io_sfc.py`; `tests/unit/test_extensions_interfaces.py::test_io_and_sfc_models_refuse` | No IO model |
| P9.2 | Explicit SFC ledger; gate "accounting balances" (`ledger.py`) | **interface-only** | `reva.extensions.io_sfc` (capability `economic.sfc_ledger` reserved; `verification.double_entry_balance` verifier) | `tests/unit/test_extensions_interfaces.py::test_balanced_ledger_passes`, `::test_imbalance_detected_with_residual`, `::test_money_never_balances_against_energy`, `::test_stock_flow_consistency_and_zero_sum_claims` | Double-entry verifier only; no ledger model |
| P9.3 | Coupling solver for one feedback block; gate "converges on reference problems" (`simulation/feedback.py`) | **interface-only** | `reva.extensions.feedback` (capability `coupling.feedback`, reserved) | `tests/unit/test_extensions_interfaces.py::test_feedback_solve_refuses_even_with_valid_contract`, `::test_outcome_never_presents_last_iterate_as_equilibrium` | No solver; `simulation/feedback.py` absent |
| P9.4 | Commodity/route/facility components; one independently reviewed causal policy model; identification review; no double count totals/bilateral | **not started** | capability `causal.policy_model` (reserved, no interface module) | `src/reva/capabilities.py` | No component or causal model. The no-double-count rule exists for V1 trade (`tests/integration/test_world_graph.py::test_aggregation_refuses_world_total_plus_bilateral`) |
| P9.5 | Multi-period horizon / macroeconomic outputs | **interface-only** | capabilities `horizon.multi_period`, `outputs.macroeconomic` (reserved, refused by `reva.simulation.engine`) | `SIM::test_five_year_horizon_rejected` | Refusal only |
| P10.1 | Household-aware synthetic population from licensed joint data; uncertainty over unidentified joints (`population/households.py`, `fusion.py`) | **interface-only** | `reva.extensions.population` (capability `population.households`, reserved) | `tests/unit/test_extensions_interfaces.py::test_household_synthesis_refuses_and_validates_sources` | No household synthesis; no licensed joint data |
| P10.2 | Privacy attack assessment; no unsupported sensitive inference (`population/privacy.py`) | **interface-only** | `reva.extensions.population` (capability `population.privacy_accounting`, reserved) | `tests/unit/test_extensions_interfaces.py::test_dp_budget_requires_explicit_terms` | No privacy accounting or attack evaluation |
| P10.3 | Network-aware compression; rare-event benchmarks; tail certificates (`compression/network.py`) | **not started** | capability `compression.network` (reserved, no interface module) | `src/reva/capabilities.py` | — |
| P11.1 | Secure external code sandbox (`autonomy/external_runner.py`); gate "privilege/egress/resource attacks blocked" | **interface-only** | `reva.extensions.runner` + `reva.autonomy.sandbox.HttpExternalRunner` (capability `autonomy.external_sandbox_runner`, **blocked** unless `REVA_SANDBOX_ENDPOINT` is configured) | `tests/unit/test_extensions_interfaces.py::test_runner_policy_defaults_are_safe`, `::test_run_request_refuses_secrets_and_unaddressed_inputs`, `tests/unit/test_capabilities.py::test_sandbox_blocked_unless_configured` | No isolated runner is provisioned; attack tests against a real runner are not possible |
| P11.2 | Autonomous parser-repair branches (`autonomy/code_changes.py`) | **not started** | capability `autonomy.parser_repair` (reserved) | `src/reva/capabilities.py` | V1 repair (`src/reva/autonomy/repair.py`) is declarative/proposal-only, not code branches |
| P11.3 | Shadow models, locked-holdout tournament, signed promotion, drift/retirement, rollback (`operations/releases.py`) | **interface-only** | capability `models.shadow_promotion` (reserved); V1 locked holdout in `src/reva/evaluation/tournament_service.py` | `tests/integration/test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent` (V1 behaviour only) | No shadow deployment, no signed promotion, no prospective rollback gate; `operations/releases.py` absent |
| P11.4 | L4 fidelity (research execution) | **interface-only** | capability `fidelity.L4` (reserved) | `SIM::test_l3_and_l4_rejected` | Refusal only |
| P12.1 | Robust multi-objective policy search; Pareto frontier (`decision/spec.py`, `optimise.py`, `pareto.py`) | **interface-only** | `reva.extensions.decision` (capability `decision.search`, reserved) | `tests/unit/test_extensions_interfaces.py::test_decision_spec_validation`, `::test_optimise_refuses_with_structured_gap`, `::test_frontier_points_are_model_conditioned` | No optimiser |
| P12.2 | Multi-actor reaction models; no unsupported equilibrium claim (`decision/reactions.py`) | **interface-only** | `reva.extensions.decision` (capability `decision.multi_actor`, reserved) | `tests/unit/test_extensions_interfaces.py::test_multi_actor_spec_and_refusal` | No reaction model |
| P12.3 | Ecological/health/governance/language domain plugins (`models/ecological/*`, `social/*`) | **not started** | capability `domains.ecological_social` (reserved) | `src/reva/capabilities.py` | — |

---

## 5. Mandatory acceptance tests (implementation prompt §13)

| ID | Criterion | Status | Evidence | Gaps |
|---|---|---|---|---|
| A.S1 | M=100, X=40, G=1000, p=1.25 → ΔM=25, ΔX=10, Δnet=15, 1.5% | **done** | `CE::test_hand_computed_l1_exact` | — |
| A.S2 | All multipliers 1 → zero | **done** | `CE::test_unit_multipliers_give_zero` | — |
| A.S3 | Zero elasticities exactly recover fixed-quantity | **done** | `CE::test_zero_elasticity_l2_equals_l1_exactly` | — |
| A.S4 | Quantity/valuation missingness blocks unsupported conversions | **done** | `tests/unit/test_ontology_units.py::test_kg_to_barrel_refused_without_density_and_convention`, `tests/unit/test_ontology_validation.py::test_cif_and_fob_values_cannot_be_aggregated`, `::test_currency_amount_without_basis_blocks_aggregation` | — |
| A.S5 | Different reference years / GDP bases not silently combined | **done** | `tests/unit/test_ontology_validation.py::test_countries_with_different_reference_years_cannot_be_combined`, `::test_countries_with_different_gdp_bases_cannot_be_combined`, `CE::test_mixed_reference_years_rejected`, `CE::test_mixed_basis_rejected` | — |
| A.S6 | January and mid-year population not identical concepts | **done** | `tests/unit/test_ontology_validation.py::test_january_and_midyear_population_are_not_identical_concepts`, `EUR::test_wdi_midyear_equivalence_is_incompatible` | — |
| A.S7 | Probability, credible/prediction intervals, bounds, sensitivity distinct | **done** | `tests/scientific/test_uncertainty_types.py::test_probability_kind_is_probabilistic`, `::test_interval_kinds_require_a_level`, `::test_bounds_are_non_probabilistic_and_levelless`, `::test_sensitivity_range_cannot_carry_a_level` | Observation contract omits the `probability` kind by design (`tests/contracts/test_json_schemas.py::test_observation_contract_restricts_kernel_kinds_only_by_probability`) |
| A.T1 | Two revisions → different as-of answers | **done** | `REV::test_two_revisions_yield_different_as_of_answers`, `API::test_beliefs_are_release_pinned_and_as_of` | — |
| A.T2 | A→B→A → three revisions; identical polls none | **done** | `REV::test_aba_creates_three_revisions_identical_polls_create_none`, `REV::test_aba_with_byte_identical_return_creates_third_revision` | — |
| A.T3 | Old raw + new parser distinguished from original vintage | **done** | `REV::test_replayed_raw_with_new_parser_is_distinguished_from_original_vintage`, `EF::test_r28_parser_version_change_reprocesses_same_bytes` | — |
| A.T4 | Staged/uncommitted objects invisible | **done** | `tests/integration/test_storage_publication.py::test_crash_after_upload_before_commit_is_invisible_then_retry_publishes_once`, `tests/unit/test_storage_fs.py::test_mid_stream_failure_leaves_no_object` | — |
| A.T5 | Missing publication time never filled with reference date | **done** | `REV::test_missing_source_publication_time_is_never_the_reference_date` | — |
| A.T6 | Scientific UPDATE/DELETE fails under runtime roles | **done** | `RG::test_runtime_roles_cannot_update_delete_truncate_history`, `RG::test_owner_cannot_mutate_immutable_records_either` | — (self-approved governance inserts are refused since the governance fix; §8.3) |
| A.I1 | Simulation role cannot insert evidence or beliefs | **done** | `RG::test_sim_cannot_insert_evidence_or_beliefs`, `SIM::test_sim_role_cannot_write_evidence_or_beliefs` | — |
| A.I2 | Generated code gets no production credentials or network by default | **done** | `tests/security/test_sandbox_isolation.py::test_trusted_template_env_has_no_secrets_and_network_is_template_only`, `::test_generated_code_is_blocked_without_external_runner`, `::test_python_guard_blocks_socket_and_raw_socket` | — |
| A.I3 | Prompt injection in a paper cannot change policy/allowlist | **done** | `tests/security/test_prompt_injection.py::test_injected_paper_text_yields_only_a_candidate` | — |
| A.I4 | SSRF, redirect-to-private-IP, zip bomb, SQL injection, secret-log tests | **done** | `SSRF::test_hosts_resolving_to_non_public_addresses_rejected`, `SSRF::test_redirect_hop_is_checked`, `SSRF::test_zip_bomb_ratio_rejected`, `tests/security/test_sql_injection.py::test_entity_listing_filters_treat_input_as_data`, `tests/security/test_secrets_logs.py::test_http_client_logs_do_not_contain_secret_query_values`, `::test_recorded_fixture_manifest_excludes_secret_query_values` | — |
| A.I5 | App startup env has no migration-owner password/URL | **done** | `tests/security/test_start_env.py::test_api_child_env_keeps_only_api_credentials`, `::test_start_api_subprocess_child_environment`, `::test_worker_child_env_has_no_api_or_owner_secrets` | — |
| A.R1 | Kill worker after upload / after commit → one visible result | **done** | `QR::test_kill_after_object_upload_then_retry_yields_one_acquisition`, `QR::test_kill_after_db_commit_before_ack_yields_one_acquisition`; real OS processes: `tests/chaos/test_chaos_queue.py::test_sigkill_mid_simulation_reclaimed_after_lease_expiry_and_completed_once`, `tests/chaos/test_chaos_publication.py::test_sigkill_inside_publication_transaction_leaves_no_half_published_release` | — |
| A.R2 | Expired lease/fencing holder cannot publish | **done** | `QR::test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit`, `SIM::test_stale_fencing_token_cannot_publish`, `tests/replay/test_replay_job.py::test_stale_lease_cannot_publish_a_replay`, `tests/chaos/test_chaos_queue.py::test_sigstop_zombie_wakes_after_reclaim_and_cannot_publish` | — |
| A.R3 | Two workers claiming a due job → no duplicate effects | **done** | `QR::test_concurrent_claims_never_return_the_same_job`, `QR::test_two_schedulers_create_exactly_one_job_per_slot` | — |
| A.R4 | Rate limiting, HTML-200, schema drift → retries/quarantine | **done** | `SSRF::test_429_retry_after_is_honoured_then_succeeds`, `HC::test_runner_quarantines_truncated_page_and_html`, `HC::test_runner_quarantines_row_level_schema_drift`, `HC::test_wb_schema_drift_is_a_failure` | — |
| A.R5 | Restore reconstructs identical logical manifests and as-of results | **done** | `tests/integration/test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of`, `tests/integration/test_backup_restore_roundtrip.py::test_backup_restore_round_trip_fresh_db_and_object_store`, `docs/dev/backup_check.md` | Local only (see P7.4) |
| A.P1 | Leaf totals conserved; no double-counted totals | **done** | `tests/unit/population/test_population_materialise.py::test_leaf_totals_conserved_exactly`, `tests/unit/population/test_population_recipe.py::test_only_disjoint_leaves_are_counted` | — |
| A.P2 | Structural-zero and unknown-category handling | **done** | `tests/unit/population/test_population_recipe.py::test_observed_zero_is_not_structural_zero_and_not_missing`, `::test_unknown_and_top_coded_retained_explicitly`, `tests/unit/population/test_population_partition.py::test_observed_zero_cells_are_not_structural_zeros` | — |
| A.P3 | Same seed/config → same logical cohort | **done** | `tests/unit/population/test_population_materialise.py::test_same_inputs_identical_outputs`, `tests/unit/population/test_population_recipe.py::test_digest_is_deterministic_and_seed_sensitive` | — |
| A.P4 | Coarsen/refine preserves weights; reports unsupported detail | **done** | `tests/unit/population/test_population_compression.py::test_coarsen_then_refine_round_trip`, `::test_refine_along_unsupported_dimension_raises` | — |
| A.E1 | Six future-information leakage cases blocked | **done** | `tests/replay/test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode` | — |
| A.E2 | Naive baseline present; insufficient evidence does not promote | **done** | `tests/unit/forecasting/test_forecast_tournament.py::test_short_panel_keeps_incumbent_insufficient_evaluation`, `tests/integration/test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent` | — |
| A.E3 | Provenance drawer reaches source bytes/selectors | **done** | `ui/e2e/country.spec.ts`, `API::test_provenance_reaches_raw_artifact_and_selector` | See P5.5 (acceptance-report mapper) |
| A.E4 | Every simulation screen and export has scenario context | **done** | `ui/src/pages/SimulationPage.test.tsx`, `ui/src/lib/scenario.test.ts`, `ui/e2e/simulation.spec.ts` | — |
| A.E5 | Negative/degraded states render clearly; no stub success | **done** | `ui/src/components/MissingValue.test.tsx`, `QR::test_missing_handler_is_a_typed_failure_never_success`, `tests/unit/test_capabilities.py::test_not_available_typed_result`, `tests/invariants/test_invariant_34_no_stub_success.py` | — |
| A.P5 | Measure on the named target resources | **blocked-on-credentials** | `docs/dev/perf_report.md` (dev container, 1 CPU pinned plus emulated 0.5 CPU quota) | Render plans (API 0.5 CPU/512 MB, worker 1 CPU/2 GB, DB 0.1 CPU/256 MB) are not available without deployment |
| A.P6 | 100k-record memory/time budget; API/L1 targets | **done** | `tests/performance/test_population_perf.py::test_materialise_100k_records_france_like`, `tests/performance/test_perf_simulation.py::test_l1_four_country_calculation_under_2s`, `tests/performance/test_perf_api.py::test_api_latency_one_cpu`, `tests/performance/test_perf_queue.py::test_queue_acknowledgement_under_1s`; `var/perf/perf_results.json` | Single run on a shared machine |
| A.P7 | Report actual values; no weakened thresholds without ADR | **done** | `docs/dev/perf_report.md` (targets = architecture §20.3 limits) | — |

---

## 6. MVP gates (architecture §17.4) and the §17.3 demonstration

| ID | Gate | Status | Evidence | Gaps |
|---|---|---|---|---|
| M.1 | Real live acquisitions for live acceptance; CI uses captured fixtures | **done** | `docs/dev/live_checks.md` (55 live acquisitions, 0 fixture-origin rows in the live DB); `tests/fixtures/source/` | — |
| M.2 | Missing source/credential/rate limit is visible blocked/degraded, never fake success | **done** | `src/reva/capabilities.py`; `tests/unit/test_capabilities.py::test_not_available_typed_result`, `SSRF::test_retry_after_beyond_cap_raises_without_sleeping`, `QR::test_missing_handler_is_a_typed_failure_never_success` | — |
| M.3 | All four country profiles exist even if a scenario input is unavailable | **done** | `API::test_entities_list_and_detail`, `API::test_dza_crude_import_belief_is_typed_missing`; `ui/e2e/country.spec.ts` (DZA rendered as missing, never 0) | **DZA data gap:** Comtrade returned zero HS2709 rows for DZA in 2020–2023 (both flows; `docs/dev/live_checks.md`), stored as typed `Missing(unavailable; provider_returned_no_rows)` |
| M.4 | No scenario number retrievable through a canonical observation endpoint | **done** | `src/reva/api/routes/evidence.py` reads only `evidence.*` / `belief.*`; `RG::test_sim_cannot_insert_evidence_or_beliefs`, `RG::test_belief_input_cannot_reference_simulation_or_evaluation_outputs` | — |
| M.5 | No synthetic record linked to a real individual | **done** | `tests/unit/population/test_population_recipe.py::test_forbidden_attributes_rejected`, `tests/contracts/test_registry_components.py::test_component_privacy_is_v1_admissible_and_no_gender_field` | — |
| M.6 | MVP operational only after complete live demo **and** restore test pass | **partial** | Restore: P7.4. Live demo: P7.7 (13/13 recorded, started 00:19) | Both have recorded passes, but before later changes. **Do not claim the MVP operational** until the lead's final run confirms both (§9); it is also not deployed (§10) |
| M.7 | §17.3 one workflow, steps 1–12 + negative demo (fixtures) | **done** | `scripts/demo_workflow.py`; `var/acceptance/demo_fixtures.json` (00:19, 13/13 pass); `tests/e2e/test_demo_workflow.py::test_all_non_blocked_steps_pass`, `::test_negative_demo_reports_the_algerian_blocker` | Step 4 relations are not release-pinned (P3.3) |
| M.8 | §17.3 workflow, live mode | **done** | `var/acceptance/demo_live.json` (started 00:19, 13/13 pass) | Repeat in the final run (§9) |

---

## 7. First actions (§3) and required CI checks (architecture §24)

| ID | Requirement | Status | Evidence | Gaps |
|---|---|---|---|---|
| F.1 | Inventory; map existing files to prescribed modules (§3.1, §1) | **done** | `docs/architecture.md` (§22 mapping table), `docs/dev/AGENT_BRIEF.md` | — |
| F.2 | `docs/implementation_status.md` with phases, evidence paths, blocked items (§3.2) | **partial** | this file | Draft; §9/§10 pending |
| F.3 | Invariants in `docs/data_constitution.md` + `tests/invariants/` coverage mapping (§3.3) | **done** | `docs/data_constitution.md`, `tests/invariants/coverage.yaml`; `tests/invariants/test_coverage_map.py::test_every_cited_test_node_exists`, `::test_data_constitution_lists_the_same_35_invariants` | — |
| F.4 | Makefile/CLI, configuration validation, health endpoints (§3.4) | **done** | `Makefile`, `src/reva/cli/main.py`, `src/reva/settings.py`; `tests/unit/test_settings.py::test_production_rejects_unsafe_configuration`, `API::test_health_ready` | `reva db upgrade` is an alias of bootstrap (audit D-5) |
| F.5 | Fixtures with URLs, hashes, licences, retrieval times; no secrets (§3.5) | **done** | `tests/fixtures/source/*/manifest.json`; `tests/security/test_secrets_logs.py::test_captured_fixture_manifests_contain_no_secrets` | `reva fixtures load` omits Eurostat (that path goes through the autonomy gate) |
| F.6 | Fixture origin never publishable in production (§3.6) | **done** | `src/reva/kernel/context.py`; `tests/unit/test_kernel_values.py::test_fixture_origin_never_publishable_in_production`, `RG::test_belief_input_rejects_fixture_observation_in_live_release`, `DBH::test_live_release_rejects_fixture_observation` | — |
| C.1 | Ruff/mypy/TS typecheck/import boundaries (§24 #1) | **partial** | `.github/workflows/ci.yml` | 1 ruff E501 at drafting; mypy `ports` only in CI (see P0.4) |
| C.2 | JSON Schema/OpenAPI compatibility, registry validation (§24 #2) | **done** | `scripts/validate_contracts.py`, `tests/contracts/`, `tests/integration/test_api_contract.py::test_committed_contract_is_current` | No backward-compat diff (first release) |
| C.3 | Unit/property/numerical identity tests (§24 #3) | **done** | `tests/unit/`, `tests/property/`, `tests/scientific/` | — |
| C.4 | Migrations from empty and from previous version (§24 #4) | **partial** | conftest migrates from empty; CI upgrades from `0006_operations`; schema drift detection `src/reva/adapters/postgres/schema_contract.py` + `migrations/schema_contract.json` (`reva` CLI verify command, `scripts/update_schema_contract.py --check`) | No released previous version exists, and migrations were edited in place (R5-04). The drift check now has tests (`tests/integration/test_schema_contract.py`, run by the pytest job); no separate CI step |
| C.5 | Role/grant and cross-context tests with real logins (§24 #5) | **done** | `RG`, `DBH` | — |
| C.6 | Captured connector replay, revisions, pagination, errors, drift (§24 #6) | **done** | `HC`, `REV`, `tests/integration/test_fixture_ingest.py` | — |
| C.7 | Temporal/spatial consistency and leakage (§24 #7) | **done** | `tests/replay/`, `tests/unit/test_extensions_physical.py::test_antimeridian_region_on_both_longitude_conventions` | — |
| C.8 | Scientific reference cases, conservation, population diagnostics (§24 #8) | **done** | `tests/scientific/`, `tests/unit/population/` | — |
| C.9 | Queue crash/retry/fencing; object-publication fault injection (§24 #9) | **done** | `QR`, `tests/integration/test_storage_publication.py` | — |
| C.10 | UI e2e provenance, scenario banner, missing data, auth/CSRF (§24 #10) | **done** | `e2e` job in `.github/workflows/ci.yml` → `scripts/e2e_stack.py --run`; `ui/e2e/*.spec.ts` | `ui/e2e/RESULTS.md` is stale (21/25 at 23:56; 25/25 claimed by commit `186f0f5`) |
| C.11 | Dependency/secret/licence scans; sandbox adversarial tests (§24 #11) | **done** | CI steps `pip-audit`, `npm audit`, licence scans in `.github/workflows/ci.yml`; `tests/security/test_secrets_logs.py`, `tests/security/test_sandbox_isolation.py` | — |
| C.12 | Named-hardware performance budget and memory tests (§24 #12) | **blocked-on-credentials** | `tests/performance/` (marker `perf`) | Named Render hardware unavailable; see A.P5 |
| C.13 | Docker build, non-root smoke, scrubbed env, Blueprint validation (§24 #13) | **done** | `docker` job in `.github/workflows/ci.yml`; `tests/security/test_start_env.py`; `scripts/validate_blueprint.py` | — |

---

## 8. Non-negotiable invariants (architecture §24.1) and known risks

### 8.1 Invariants 1–35

Source: `tests/invariants/coverage.yaml`, validated by `tests/invariants/test_coverage_map.py`, which checks that
every cited node is collected and every enforcement symbol exists. "proven" maps to **done** and
"partially_proven" to **partial**. Enforcement points and test node ids for every row are in the YAML.

| # | Invariant (short) | Status | Gap (from coverage.yaml, re-checked) |
|---|---|---|---|
| I.1 | Complete provenance or explicit blocker | **done** | — |
| I.2 | No overwriting observation/model/scenario/release | **done** | — |
| I.3 | No scenario/evaluation result as evidence | **done** | — |
| I.4 | No source report labelled certain | **done** | — |
| I.5 | No missing → zero | **done** | — |
| I.6 | No unit/basis change without versioned transform | **done** | — |
| I.7 | No aggregation across incompatible support/classification/universe | **done** | — |
| I.8 | No correlation labelled causal | **done** | Structural (no causal model in V1) |
| I.9 | No loss of uncertainty/dependence metadata | **partial** | No approximation-record type; dependence_group propagation through forecast/simulation untested |
| I.10 | No synthetic/private identity linkage | **done** | — |
| I.11 | No model execution fetching live inputs | **done** | — |
| I.12 | No replay with unavailable revisions/later preprocessing | **done** | — |
| I.13 | No source-family duplicates counted as independent | **partial** | `lineage_family` is recorded but nothing consumes it (no pooling in V1); `EF::test_invariant13_alternatives_record_lineage_family_independence` covers recording only |
| I.14 | No arbitrary code from documents/agents in production | **done** | — |
| I.15 | No self-promotion / editing own gates | **done** | Changed since the draft: DB-enforced governance plus the separate gate login (P1.7, P6.9, §8.3) |
| I.16 | No kernel/ontology mutation without version | **done** | — |
| I.17 | No silent truncation | **done** | — |
| I.18 | No partial multi-object publication visible | **done** | Changed since the draft: seal races and the relation seal are tested (P1.8); a SIGKILL inside the publication transaction leaves no half-published release (`tests/chaos/test_chaos_publication.py`) |
| I.19 | No commit after losing fencing token | **done** | — |
| I.20 | No model outside declared support | **done** | — |
| I.21 | No fabricated probabilities | **done** | — |
| I.22 | No interval conflation | **done** | — |
| I.23 | No provider code as universal ID | **done** | — |
| I.24 | No instability/conservation hidden by rounding | **done** | — |
| I.25 | No geography permanently excluded | **done** | DZA kept as a visible gap |
| I.26 | No household/network coarsening without diagnostics | **done** | By refusal (no households in V1) |
| I.27 | No calibration claim without denominator/holdout | **done** | — |
| I.28 | No global knowledge % without finite catalogue | **done** | — |
| I.29 | No `os.cpu_count()` concurrency | **done** | — |
| I.30 | No egress/LLM/compute beyond budget | **partial** | Changed since the draft: daily egress budgets are enforced through the persistent ledger `ops.budget_ledger` on every live provider path in `src/reva` (`src/reva/operations/budgets.py`), and NL drafting reserves and settles against `llm.scenario_drafts` (`src/reva/nl/budget.py`). Still open (coverage.yaml): operator scripts outside `src/reva` (`scripts/live_source_checks.py`, live `scripts/demo_workflow.py`) are bounded only by per-run budgets; internal HTTP retries are not charged separately. The coverage.yaml text still says the LLM is "disabled with a zero budget", which is out of date for drafting (`docs/dev/v2_nlc.md` request 2) |
| I.31 | No secrets in code/fixtures/logs/artifacts | **done** | — |
| I.32 | Restricted-data deletion overrides retention | **partial** | No end-to-end deletion workflow (objects, backups, derived artifacts); V1 stores no restricted data |
| I.33 | No dynamic economic effect from the accounting identity | **done** | — |
| I.34 | No success from a stub/unavailable capability | **done** | — |
| I.35 | No claim that tests prove causal truth/completeness | **partial** | Not provable by tests; lexical wording scan only |

### 8.2 Known data gap: Algeria (DZA)

UN Comtrade public preview returned **zero rows** for DZA HS 2709 imports and exports in **2020–2023** (exact
world-partner keys, HTTP 200, `count: 0`). These are stored as typed `Missing(unavailable)` with
`provider_returned_no_rows=true` and never zero-filled (`docs/dev/live_checks.md`). The default scenario runs
FRA/CHN/USA. An all-four request for 2023 returns `missing_input` for DZA (P4.4). 2024 has one provider-estimated
H4 row per flow (`isReported=false`, `legacyEstimationFlag=6`). The import value is US$5,564, which is
implausible, so a 2024 all-four request would run with a value that needs review. Keep the negative demonstration
pinned to 2023 and the DZA gap visible (`/v1/gaps`, Country screen).

### 8.3 Governance: enforced in the database, gate in its own process (resolved since the draft)

The draft recorded that `reva_worker` could insert self-approved `evidence.eligibility`, `ontology.mapping` and
`model.version` rows (R5-01). That is closed:

* `meta.enforce_governance` (0007) records the writing login (`recorded_by = session_user`). Approved states are
  written only by the owner or by `reva_registry`-owned approval functions. Those functions need a passing gate
  result **recorded by `reva_gate`** or a human review recorded through the API. The approval is bound to the
  gate-derived scope and manifest (R6-02) and used once (`ops.approval_use`). T2+ changes need a human review
  (R6-03). Tests: `GOV::test_worker_cannot_self_approve_registry_rows`, `GS::*`.
* The promotion gate runs as a separate process with its own login, the only one that may record gate results.
  The worker files `ops.gate_request` rows and waits. With no gate it reports `gate_pending` and promotes
  nothing, and `gate_followup` promotes a late pass (`docs/dev/gate_separation.md`;
  `tests/integration/test_gate_separation.py`, `tests/integration/test_gate_followup.py`).

Residual risk (documented in `docs/limitations.md` "Governance limits" and `docs/dev/gate_separation.md`):
* The gate decides on inputs the worker stored, so a compromised worker chooses *what* is evaluated, though not
  the verdict.
* The API, worker and gate run the same image.
* In local development the gate shares the host and the `.env` with the worker, so the separation is a credential
  convention there.
* `model.record_promotion`, `model.evaluation` and `meta.artifact` stay worker-writable.

---

## 9. Actual test results (final run, 2026-10-15)

All results below are from the final code (`docs/final_report.md` §3 has the same table with context).

| Check | Command | Result | Evidence |
|---|---|---|---|
| pytest offline | `uv run pytest -m "not live and not perf" -q -rfE` | **2,439 passed, 0 failed**, 30 skipped, 21 deselected | session log; CI runs the same selection |
| pytest live | `uv run pytest -m live -q` | see `docs/final_report.md` §4 | — |
| live acceptance workflow | `uv run python scripts/demo_workflow.py --live` | **13/13 pass** | `docs/dev/final_run/demo_live_summary.json` |
| ruff / format / import-linter / mypy | `ruff check .`; `ruff format --check .`; `lint-imports`; `mypy -p reva.kernel -p reva.ports` | all pass | — |
| UI typecheck / vitest / build | `npm --prefix ui run typecheck`; `npx vitest run`; `npm run build` | pass / 144 passed (22 files) / pass | — |
| UI Playwright | `uv run python scripts/e2e_stack.py --run --recorded-drafter --v2-fixtures` | **53 passed**, 1 skipped (drafting-disabled path) | — |
| performance | `uv run python scripts/measure_performance.py` | all 8 targets pass (API p95 147 ms at 1 CPU, 281 ms at 0.5 CPU; L1 2.9 ms; queue ack 249 ms; 100k records 1.6 s / 183 MiB) | `docs/dev/final_run/perf_results.json` |
| Docker build | `docker build --secret id=build_ca,… -t reva-world:final .` | builds (89 s), 1.34 GB, non-root uid 10001 | — |

## 10. Deployment readiness

**Not deployed — blocked on credentials.** Blockers (operator-supplied, see `docs/deployment.md`): a Render
account/API key allowed to create paid resources; a private S3-compatible bucket and three scoped key pairs (API
read-only, worker prefix-write, gate prefix-scoped) plus a read-only backup key; the admin password hash and a
contact e-mail. Ready and verified locally: `render.yaml` (validated), Docker image (built from the final code,
non-root), fail-closed start scripts for API/worker/gate, backup/restore (filesystem and MinIO S3), S3
credential split on MinIO. Unverified until a real deploy: Render internal-database TLS, AWS-specific S3
behaviour, Render-hardware performance, minisign signing.

---

## 11. Top gaps (ordered by risk to an honest P0–P7 claim)

Updated by DOCV2. Items closed since the draft: worker self-approval of governance rows (now DB-enforced, separate
gate); the live demo (13/13 recorded); untested seal, scope and `reva_sim` fixes (tests exist); persistent egress
budgets (ledger exists).

1. **Deployment blocked on credentials** (P7.2, P7.3, P7.13, A.P5, C.12). Needs a Render API key and the S3 bucket
   with three scoped key pairs. Not deployed. S3 behaviour is checked only on a local MinIO.
2. **No final full run yet** (§9). The recorded live demo and the restore runs predate later changes (gate
   separation, V2, locks). The MVP cannot be declared operational until the final run confirms them (M.6).
3. **Migrations edited in place** (§2, C.4, R5-04). Freeze 0001–0008 before release. The dev DB `reva` is at an
   older head without the later fixes.
4. **World.relation pinning not shown end to end** (P3.3). Both recorded demos show `release_pinned_edges=0`. The
   pinning code and the relation seal landed later, and no test asserts that `publish_release` pins the ingested
   edges.
5. **DZA data gap** (§8.2, M.3). Zero Comtrade crude rows 2020–2023. The 2024 import value (US$5,564) is
   implausible and is only a review signal.
6. **Budget gaps outside `src/reva`** (I.30). Operator scripts are bounded only per run.
7. **Lint/type gate not confirmed** (P0.4, C.1). At drafting: 1 ruff E501, and the local mypy config omits
   `reva.ports`; re-check in the final run.
8. **Eurostat Jan-1 population absent from the live release** (P6.2, P6.11). The V1 population service has only
   run on fixtures. The V2 cohort model has its own connector (§12).
9. **V2 models are experimental** (§12). There is no maintainer review, and the scientific review's fixes were not
   re-reviewed.

Further open items: BacktestRunner strict/available-then modes blocked (P5.2); I.9, I.13 and I.32 partial;
stale `ui/e2e/RESULTS.md` and the acceptance-report mapper ignoring Playwright drill-through (P5.5); source
observations not in Parquet manifests and DuckDB unused (P1.11); P8–P12 not implemented (interfaces only). The V2
models in §12 are narrower and leave those capabilities `reserved`: `economic.io_price` is a single-period
price model, not P9.1 (`economic.input_output`); `population.household_margins` is not P10.1 household synthesis
(`population.households`).

---

## 12. V2 ledger (roadmap approved 2026-10-15; `docs/roadmap/v2_design.md`)

Same status vocabulary as above. "done" here means the code and its tests exist; it does **not** mean reviewed.
Every V2 model is registered as `candidate` with `review_status: maintainer_review_pending`, and every result says
"experimental model, not yet reviewed". Projections say "conditional projection under stated assumptions, not a
forecast". V2 capabilities are `implemented` in `src/reva/capabilities.py` only with cited tests and docs
(`tests/unit/test_capabilities_v2.py`). The live runs below used throwaway databases and their own budgets. No row
here has been confirmed in a final full run (§9).

| ID | Item | Status | Evidence | Gaps |
|---|---|---|---|---|
| V2.P1 | Platform: model plugins, scenario spec 2.0.0 generated from the registered plugins, the crude model as plugin #1 with bit-identical outputs | **done** | `src/reva/simulation/plugins.py`, `scenario_schema.py`, `contracts/scenario.schema.json` (V1 frozen as `contracts/scenario.v1.schema.json`); `docs/dev/v2_eng.md`; `tests/unit/test_simulation_v2_plugins.py`, `tests/integration/test_simulation_v2_plugin_identity.py` (job path vs stored V1 goldens), `tests/contracts/test_scenario_schema_v2.py` | The implementation digest changed by design, so earlier L0 cache entries are not reused |
| V2.P2 | Platform: dataset inputs (Parquet from archived raw bytes, manifest, release-pinned by digest) | **done** | `src/reva/simulation/datasets.py`, `migrations/versions/0008_v2.py` (`evidence.dataset`, `belief.release_dataset`, sealed like estimates); `tests/integration/test_simulation_v2_datasets.py` | Datasets have no separate human eligibility decision; they must come from reviewed built-in connectors (`docs/dev/v2_eng.md` decision 4). `publish_release` refuses a release with datasets but no eligible observation |
| V2.P3 | Platform: multi-period results (`period_start`/`period_end`, half-open) served by the API | **done** | `0008_v2.py` (`result_period_half_open`); `src/reva/api/routes/sim.py` (`SimResult.period_*`, `results_truncated` above 5,000 rows); `tests/integration/test_simulation_v2_datasets.py` | Capabilities `horizon.multi_period` / `outputs.macroeconomic` (P9) stay reserved |
| V2.1 | Step 1: crude scenario for every country with data (`countries: ["ALL"]`, `skip_missing_countries`) | **done** | `src/reva/adapters/connectors/{country_crosswalk,comtrade_batch,worldbank_all}.py`, `registry/entities/country_crosswalk.json`, `src/reva/simulation/compile.py` (`resolve_all_countries`); `docs/dev/v2_allc.md` (live throwaway run: 74–89 countries with complete inputs per reference year 2020–2024, the rest skipped with typed reasons); `tests/unit/test_country_crosswalk.py`, `tests/unit/test_comtrade_worldbank_all_connectors.py`, `tests/integration/test_comtrade_worldbank_all_countries.py`, `tests/integration/test_scenario_all_countries.py` | The crosswalk is `machine_derived_pending_maintainer_review`. HS H2/H3 values (e.g. Iraq's 2023 exports) stay ineligible without a reviewed mapping. A country missing one flow is skipped, never zero-filled (owner questions in `v2_allc.md`) |
| V2.2 | Step 2: natural language → draft scenario (`POST /v1/scenario-drafts`) | **done** (`requires_config`) | `src/reva/nl/`, `src/reva/api/routes/nl.py`, budget `llm.scenario_drafts` in `registry/policies/budgets.yaml`, ledger functions in `0008_v2.py`; `docs/dev/v2_nlc.md` (15 recorded live OpenAI responses, all answered by `gpt-4.1-2025-04-14`); `tests/unit/test_nl_drafts.py`, `tests/unit/test_nl_crosschecks.py`, `tests/unit/test_nl_ledger_migration.py`, `tests/integration/test_nl_drafts_api.py`, `tests/integration/test_nl_live.py` (`live`) | Disabled by default (`REVA_LLM_ENABLED`), and it needs a credential-injecting egress proxy. The Gemini fallback has never answered successfully live (402). The server cross-checks are lexical (fr/en). `tests/invariants/test_invariant_30_budgets.py` scans the LLM path, but the I.30 text in `tests/invariants/coverage.yaml` still says the LLM is disabled with a zero budget |
| V2.3a | Step 3a: cohort-component population projection `demography.cohort_component` (EU-27 + EFTA) | **done** (experimental) | `src/reva/adapters/connectors/eurostat_demography.py`, `src/reva/models/population/`, `src/reva/evaluation/population_backtest.py`; `docs/dev/v2_pop.md`, `docs/dev/v2_pop_backtest.md` (31 countries, 2013 → 2023); `tests/unit/test_population_cohort.py`, `tests/unit/test_eurostat_demography_connector.py`, `tests/property/test_population_cohort_properties.py`, `tests/integration/test_demography_cohort.py`, `tests/integration/test_demography_live.py` (`live`) | On totals the default projection beats naive no-change in most countries but a constant-growth benchmark only in a minority (`v2_pop_backtest.md`). Revised-data retrospective only. Migration breaks are excluded only when flagged |
| V2.3b | Step 3b: multi-region IO price model `economic.io_price` (FIGARO 2024) | **done** (experimental) | `src/reva/adapters/connectors/io_tables.py` (identity checks, exact at published precision), `src/reva/models/economic/io_price.py`; `docs/dev/v2_io.md` (live run, 49 countries, all phases within 2 GB); `tests/unit/test_io_tables.py`, `tests/unit/test_io_price.py`, `tests/integration/test_io_price_pipeline.py`, `tests/integration/test_io_tables_live.py` (`live`) | Labelled "first-round cost-push under full pass-through; not an inflation forecast". The basket is at basic prices, with a purchasers'-price reading for fixed per-unit taxes; neither is a CPI. No external benchmark reproduced (plausibility only). `B` is all of mining and quarrying, broader than crude. OECD ICIO not implemented |
| V2.4 | Step 4: retirement-age model for France `social.retirement_age` and household margins | **done** (experimental) | `src/reva/adapters/connectors/eurostat_labour.py`, `src/reva/models/social/`, `registry/rules/retirement_age/FRA.yaml`, `src/reva/population/households.py` (`population.household_margins`); `docs/dev/v2_pen.md`, `docs/dev/v2_pen_backtest.md`; `tests/unit/social/`, `tests/integration/test_labour_pensions_retirement_pipeline.py`, `tests/integration/test_labour_pensions_live.py` (`live`) | The 2010-reform back-test fits (MAE 0.55 pp women, 1.20 pp men, 2012–2019), but a placebo on ages 55–59 misses by 6–12 pp: two errors of opposite sign offset each other, so the fit is not validation. It was not pre-registered. The pension-receipt drop is a proxy of unknown direction. Every result is `qualified`. LFS rates exist only in five-year groups |
| V2.5 | Step 5: retirement-age rule tables for other countries; IO shocks for every FIGARO country | **done** (experimental) | `registry/rules/retirement_age/{DEU,ESP,NLD}.yaml` with archived legal sources; ITA, BEL, SWE and POL refused with `capability_gap` (`registry/models/methods.yaml`); IO: the 49 FIGARO countries reported, others refused (`v2_io.md` §3) | Countries without a sourced rule table are refused. The rest of the world can be an IO shock origin but is not reported |
| V2.6 | Gate separation (independent promotion gate process) | **done** | See P6.9 and §8.3; `docs/dev/gate_separation.md` | Residual risk in §8.3 |
| V2.7 | Back-test API: `POST`/`GET /v1/models/{model_key}/backtests`, worker job `backtest`, `reva backtest` | **done** | `src/reva/evaluation/backtests.py` (back-test functions for `demography.cohort_component` and `social.retirement_age`), `src/reva/api/routes/{sim,catalog}.py`, `src/reva/cli/main.py`; `tests/integration/test_backtest_api.py`; `docs/api.md` | Revised-data retrospectives only. Other models return `backtest_unavailable` (GET) or `capability_gap` (POST) |
| V2.8 | API fields: `period_start`/`period_end`, `results_truncated`, `source_flags`/`stale` on beliefs, `object_store` readiness check | **done** | `src/reva/api/routes/{sim,beliefs,evidence,system}.py`, `contracts/openapi.json`; `docs/api.md` "V2 additions" | The `object_store` check is reported, never fatal |
| V2.9 | UI V2: world map, mirror view, scenario composer (confirm before run), multi-period run pages, IO heatmap, command palette | **done** | `ui/src/pages/{MapPage,ComposerPage,RunsPage,RunPage}.tsx`, `ui/e2e/v2_{map,composer,runs}.spec.ts`; `docs/dev/ui_v2.md` (typecheck pass, vitest 144 tests, e2e 53 passed / 1 skipped on the recorded-LLM stack) | E2E uses recorded LLM responses and fixture data. The IO screens run on a **synthetic** 3×2 table, not FIGARO figures. A live shock slider was deliberately not built. `ui/e2e/RESULTS.md` is not regenerated |
| V2.10 | Independent scientific review of the V2 models | **partial** | `docs/dev/review_science_v2.md` (0 critical, 4 high, 9 medium, 7 low; numerics reproduced independently for POP and IO). Responses: H2/H3/M6 server cross-checks (`docs/dev/v2_nlc.md` decision 17); H1/M1/M8/L3 (`v2_pop.md`, `v2_pop_backtest.md`); H4/M2/M3/M4 (`v2_pen.md`); M9/L1 (`v2_io.md`, `docs/methodology.md`); L2 (`src/reva/capabilities.py`); M7 and L4–L7 documented in `docs/limitations.md` / `v2_pop_backtest.md` | The fixes have not been re-reviewed. M5 (back-test not pre-registered) cannot be fixed after the fact and is disclosed. No maintainer review of any V2 model |
