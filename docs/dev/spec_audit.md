# REVA World — specification compliance audit (agent S)

Read-only audit of the repository against the binding specification
(`864d2dd3-REVA_Claude_Code_Prompt.md`: implementation prompt §1–§14, architecture §1–§32, appendices).

* **Snapshot:** 2026-10-14, 22:59–23:45 UTC, working tree on top of commit `176751f` ("WIP snapshot")
  plus uncommitted edits. Fourteen agents were writing in parallel. Between my first and last scan, about
  150 files appeared or changed: the API, UI, autonomy, world graph, ontology, CI, Dockerfile, docs,
  backup/restore and most tests. Migrations 0001–0007, `settings.py` and `cli/bootstrap.py` were edited
  between 23:22 and 23:36. Any row can be stale within minutes, so re-verify before you rely on it.
* **Method:** I read the specification in full and the code for every row. I ran `pytest` (offline),
  `ruff`, `mypy`, `lint-imports`, `scripts/validate_blueprint.py`, the UI `typecheck` and `vitest`, and
  `git ls-remote` on the CI action pins. I did not run any live test. I wrote no code.
* **Status vocabulary:** **done** means the code exists and at least one test exercises it. **partial**
  means the item is implemented with a named gap. **missing** means no implementation. **in progress (X)**
  means the owner is actively writing in that area (files changed after 23:00) and the gap may close
  soon. **blocked-external** means credentials or provisioning are needed. Owners follow
  `docs/dev/AGENT_BRIEF.md`.
* This audit complements `docs/dev/review_core.md` (agent R, adversarial code review, R-01…R-28). Where a
  gap is one of R's findings, I cite the R-number and do not repeat the analysis.

## 0. Test and tool state at the snapshot

| Check | Result | Notes |
|---|---|---|
| `uv run pytest -q` (offline, 1119 collected), run at 23:17 | 789 passed, 25 failed, 283 errors, 21 xfailed | Almost all errors were bootstrap failures (`provisioning runtime role reva_api failed`): `cli/bootstrap.py` was being edited during the run. |
| Re-run at 23:31 | Most bootstrap errors gone. New errors: `FATAL: too many connections for role "reva_worker"` (27×) | `provision_roles` now sets `CONNECTION LIMIT 10`. Roles are cluster-wide, so every agent's pytest sessions on the shared server count against the same 10 connections. Test-environment hazard, not a production bug (pool 2 + overflow 0). **Remedy (lead):** dispose engines per test module, or raise or remove the limit for test databases. |
| Failures still present at 23:31 (excluding the connection-limit ones) | `test_settings` (3), `test_start_env::test_api_production_validation_redacts`, `test_storage_s3::test_factory_builds_s3_store_for_production_settings`, `test_revisions::test_two_revisions_yield_different_as_of_answers`, `test_replay_job::test_operational_replay_job_writes_evaluation_context_and_manifest`, `test_storage_publication::test_failure_inside_transaction_rolls_back_everything`, `test_fixture_ingest`, `test_registry_components::test_published_versions_match_pinned_digests`, `test_ontology_components::test_verify_published_detects_in_place_edit`, `test_perf_queue::test_queue_acknowledgement_under_1s`, `test_db_hardening::test_provisioned_verifier_lets_the_runtime_role_log_in` | The settings/production failures follow from the new `REVA_DB_SSLMODE`/`REVA_PROCESS_ROLE` rules (see §23.1, D-13). The component digest failures mean a published component schema was edited after it was pinned. |
| Strict xfails (known, unfixed defects) | 7 | Decimal canonical form; Settings error echoes secrets; observation export vs schema; runner crash on row-level drift; `RecordingHttp` records secret query params; `sim.create_scenario` trusts the caller's digest; `release_by_id` serves unpublished releases. All listed in §19. |
| `ruff check` | 50 findings at 22:59, mostly in files then being written | Re-run before release. |
| `mypy` (strict, `reva.kernel`) | 1 error at 22:59: `kernel/uncertainty.py:102` (`object` compared with `>`) | `kernel/uncertainty.py` changed again later; re-check. |
| `lint-imports` | Failed at 22:59 (`reva.autonomy` had no `__init__`). The package exists now (23:10); not re-run. | |
| `scripts/validate_blueprint.py` (cached official schema) | valid, 0 problems | |
| UI `npm run typecheck`, `vitest run` | Typecheck clean; 39/39 tests pass | Playwright e2e (`ui/e2e/smoke.spec.ts`) is not run in CI. |
| CI action pins | `actions/checkout@3d3c42e…` = v7.0.1 and `astral-sh/setup-uv@1c37ad0…` = v10.3.0, confirmed with `git ls-remote` | The other pins were not verified. |

---

## 1. Prompt §3 — First actions

| # | Requirement | Status | Evidence | Gap / remedy (owner) |
|---|---|---|---|---|
| 1 | Inventory tree, runtime, Git, migrations, tests, deployment, secrets, data models | partial | `docs/dev/AGENT_BRIEF.md`, `docs/architecture.md` | No written inventory of existing files mapped to the prescribed modules (§1 asks for one). **Remedy:** add a mapping table to `docs/implementation_status.md` (lead). |
| 2 | `docs/implementation_status.md` with phases, checkboxes, evidence paths, blocked items | **missing** | — | **Remedy:** create it from §25 of this audit, using pass/fail/blocked per gate (lead). |
| 3 | Scientific invariants in `docs/data_constitution.md` plus `tests/invariants/` coverage mapping | partial | `docs/data_constitution.md` §"Non-negotiable invariants" maps all 35 invariants to tests; every cited test exists (checked by script) | No `tests/invariants/` directory or executable coverage check. **Remedy:** add `tests/invariants/test_coverage_map.py` that parses the table and asserts each cited test exists and is collected (agent I or lead). |
| 4 | Makefile/CLI commands, configuration validation, health endpoints | done | `Makefile` (31 targets), `src/reva/cli/main.py`, `settings.py` production validator, `api/routes/system.py` (`/health/live`, `/health/ready`) | — |
| 5 | Local fixtures; captured live fixtures with URLs, hashes, licences, retrieval times | done | `tests/fixtures/source/*/manifest.json` (`url`, `sha256`, `retrieved_at`, `licence`); `RecordingHttp` | `RecordingHttp` writes secret query params into manifests (strict xfail in `test_secrets_logs.py:259`). **Remedy:** strip secret params before recording (lead, `evidence/runner.py`). |
| 6 | Fixture values never in live production context; `origin=test_fixture` rejected by production publication | done | `kernel/context.assert_publishable`; `meta.context.origin`, `evidence.acquisition.origin`; `belief.check_input` rejects fixture observations in live releases | Tests: `test_roles_grants::test_belief_input_rejects_fixture_observation_in_live_release`, `test_kernel_values::test_fixture_origin_never_publishable_in_production`. |

## 2. Prompt §4 — Kernel, data modelling, migrations

| Requirement | Status | Evidence | Gap / remedy (owner) |
|---|---|---|---|
| Frozen typed records: Referent, Concept, Support, Assertion, Artifact, Activity, Context | done | `kernel/records.py`, `kernel/support.py`, `kernel/context.py` (all `frozen=True, extra="forbid"`) | — |
| TimeExtent: calendar, bounds, precision, interval/instant, uncertain endpoints; deep time separate | partial | `kernel/time.py` (`TimeExtent`, `UncertainBound`, `DeepTimeExtent`) | Calendars are only gregorian, proleptic_gregorian and julian, and there is no BCE/CE conversion operation. Acceptable for V1; record it as a limitation (H2 tests, lead kernel). |
| Value union: Decimal, float, category, text, entity, array, distribution, geometry, Missing; JSON precision survives | partial | `kernel/values.py`; `tests/unit/test_kernel_values.py` | `ScalarDecimal` accepts non-canonical strings (`1E+5`, `+1`, `1_000`) that the observation contract rejects (strict xfail `test_kernel_values.py:105`). **Remedy:** enforce the contract regex in `_decimal` (lead/kernel). |
| UncertaintySpec: status, interval/distribution kind, conditioning, dependence group, method; unknown valid, never zero | done | `kernel/uncertainty.py`; `tests/scientific/test_uncertainty_types.py` | Kernel kind `probability` is absent from `observation.schema.json` (strict xfail; see D-8). |
| Concept semantic profiles (universe, denominator, temporal/spatial support, aggregation, scale type, currency/price basis, permitted transformations) | done | `kernel/records.SemanticProfile`; `registry/concepts/*.yaml`; `ontology/validation.py`; `tests/unit/test_ontology_validation.py` | — |
| Component manifests with independently versioned schemas | done | `kernel/records.ComponentManifest`, `ontology/components.py`, `registry/components/*.v1.schema.json`, `tests/unit/test_ontology_components.py` | `test_published_versions_match_pinned_digests` fails at the snapshot: a published v1 schema was edited in place (K). |
| Immutable provenance; model computation ≠ real-world event | done | `meta.activity` vs `world.occurrence`; `immutable()` triggers on all scientific tables | — |
| Split reference SQL into `0001_kernel … 0007_security` | done | `migrations/versions/0001…0007` | Dependency-order moves are documented (D-1). Migrations are still edited in place after being applied (R-15); freeze them before release. |
| Supporting constraints/functions, not only tables | done | Triggers: `check_revision`, `check_input`, `check_publication`, `check_participant`, `check_relation_cardinality`, `check_location_size`, acyclic provenance; narrow `SECURITY DEFINER` functions | — |
| Real role tests with `reva_api`/`reva_worker`/`reva_sim` logins | done | `tests/security/test_roles_grants.py` (45 tests), `tests/security/test_db_hardening.py` (25 tests) | — |
| No UPDATE/DELETE on scientific history for runtime roles; restricted deletion via a reviewed administrative process | done | `meta.reject_mutation` plus the `reva_restricted_deletion` role and the `reva.reviewed_deletion_ticket` GUC; `test_runtime_roles_cannot_update_delete_truncate_history`, `test_deletion_ticket_does_not_help_runtime_roles` | The administrative deletion procedure is not documented as a runbook. **Remedy:** add it to `docs/operations.md` (I). |
| World reads are projections of a pinned release; no unversioned `current_facts` | done | `belief/snapshots.py`; no current-facts table; API resolves `release_as_of` | `release_by_id` serves unpublished releases (strict xfail `test_roles_grants.py:1083`). **Remedy:** inner-join `ops.publication` (lead/E). |

### 2.1 The twelve mandatory DB/application checks

| # | Check | Status | Enforcement point | Test | Gap / remedy |
|---|---|---|---|---|---|
| 1 | Acquisition's connector belongs to its declared source | done | Composite FK `(connector_version_id, source_id)` in `0002_evidence` | `test_roles_grants::test_acquisition_connector_must_belong_to_source` | — |
| 2 | Observation source/connector/acquisition agree | done | Composite FK to `acquisition(id, source_id, connector_version_id)` | `test_observation_must_agree_with_its_acquisition` | — |
| 3 | `supersedes` points to same source/logical key with lower revision; no cycle | done | Composite FK; `CHECK ((revision_no=1)=(supersedes_id IS NULL))`; `UNIQUE(supersedes_id)`; trigger requires `revision_no-1` | `test_revisions::test_supersedes_chain_constraints_are_enforced_by_the_database` | — |
| 4 | Source metadata/rights changes append versions; acquisitions pin the version; registrations immutable | done | `evidence.source_metadata_version` with contiguous-version trigger; acquisition FK `(source_metadata_version_id, source_id)`; `repo.append_source_metadata_if_changed` | Partially covered by role tests | No dedicated test that a rights change appends version n+1 and the next acquisition pins it. **Remedy:** add one (H2). |
| 5 | A→B→A creates a new revision; dedup only an unchanged latest state; semantic hash not unique | done | `evidence/revisions.decide_revision`; per-lineage advisory lock | `test_revisions::test_aba_creates_three_revisions_identical_polls_create_none`, `::test_aba_with_byte_identical_return_creates_third_revision` | Cross-origin lineage keying (R-04) has a fix in progress (`test_evidence_fixes::test_r04_*`). |
| 6 | Source report eligible only for approved concept/scope per connector version | done (recently fixed) | `evidence.is_eligible(cv, concept, subject, at, ref_year)` now enforces `subject_ids` and `years` (0002, edited 23:22); `belief.check_input` | `test_db_hardening::test_years_outside_the_decision_scope_are_ineligible`, `::test_input_trigger_enforces_the_support_year_scope` | Classification scope (`classifications: [H4,H5,H6]` in `registry/mappings/comtrade.yaml`) is still not enforced. **Remedy:** match `source_flags.classificationCode` against the scope or drop the key (lead). |
| 7 | Belief context mode=belief; scenario mode=simulation; evaluation/population distinct | done | `belief.check_release`, `sim.check_scenario`, `model.check_evaluation`, `model.check_population`; RLS on `meta.context` | `test_release_requires_belief_mode_context`, `test_sim_cannot_create_belief_context_but_can_create_simulation_context`, `test_worker_cannot_create_simulation_context` | — |
| 8 | Provenance edges acyclic; selector references resolve | partial | `meta.check_input_acyclic`/`check_output_acyclic`; `raw_selector.artifact_sha256` must equal the acquisition raw hash | `test_provenance_cycles_rejected`; `test_api_endpoints::test_provenance_reaches_raw_artifact_and_selector` | Only the artifact half of the selector is checked. `raw_selector.match`/`field` is never resolved against the stored bytes. **Remedy:** validate at parse time that each selector matches exactly one raw element (connector `validate`), and test it (lead/H2). |
| 9 | Belief inputs cannot refer to `sim.result` or evaluation predictions | done | `belief.input` FKs only to observation, prior estimate or claim | `test_belief_input_cannot_reference_simulation_or_evaluation_outputs` | — |
| 10 | Relation participant types/roles satisfy predicate signature | done | `world.check_participant`; deferred cardinality trigger; late inserts blocked (R-09 fix) | `test_relation_*`, `test_db_hardening::test_participants_cannot_be_added_after_the_relation_committed` | — |
| 11 | Release manifests reference only committed, checksum-verified artifacts | done | `ops.check_publication` (verified locations, inputs present, lock held); sealed release (R-02 fix) | `test_publication_rejects_unverified_artifacts`, `test_db_hardening::test_published_release_is_sealed` | The S3 "verification" trusts its own metadata (R-16). |
| 12 | Clinical/sensitive/person-level data unavailable | done | `artifact_v1_public_only` CHECK; population `privacy_class` CHECK; recipe `FORBIDDEN_ATTRIBUTES` | `test_restricted_privacy_classes_rejected`, `test_population_recipe::test_forbidden_attributes_rejected` | — |

## 3. Prompt §5 — Object storage and transactions

| Requirement | Status | Evidence | Gap / remedy |
|---|---|---|---|
| `ArtifactStore.put/get/head/range_read`, filesystem + S3 | done | `adapters/objects/{filesystem,s3}.py`; `tests/unit/test_storage_fs.py`; `tests/integration/test_storage_s3.py` (moto) | — |
| Streamed hashes, bounded memory, verified bytes; no ETag as SHA-256 | done | `test_streaming_put_memory_is_bounded`, `test_sha_comes_from_verified_metadata_never_from_etag` | R-16: S3 verification is circular. **Remedy:** use `ChecksumSHA256` or re-read the bytes on first registration (H2/lead). |
| Objects first, then atomic metadata/manifest/outbox commit; never serve uncommitted manifests | done | `evidence/ingest.publish_acquisition`, `belief/publication.publish_release` | — |
| Orphan cleanup separate from source-history retention | done | `operations/gc.py` (two-pass, 7 days); `ops.gc_candidate`; `test_queue_gc.py` | Race with concurrent re-publication (R-11), status unknown. |
| Fault tests: before upload, mid-upload, after upload/before commit, after commit/before ack | done | `test_storage_publication.py` (first three); `test_queue_reliability::test_kill_after_db_commit_before_ack_yields_one_acquisition` | `test_failure_inside_transaction_rolls_back_everything` fails: the test reads `ops.outbox` as `reva_api`, which has no grant (H2). |
| Manifests store Arrow schema fingerprint, sorted dimension order, row counts, logical keys, temporal coverage; byte vs logical hashes separate | partial | Belief release manifest (`belief/publication._estimate_table`); population Parquet metadata | Source observations are not written as normalised Parquet batches with manifests. Acquisitions carry only `schema_fingerprints` in `response_metadata`, so the `normalised/` prefix is unused. Spec §8.1 allows PostgreSQL for small panels, so this is acceptable for V1; record it in `docs/limitations.md` (lead). |
| No shared writable DuckDB; ephemeral connections over pinned manifests | partial (not used) | `duckdb` is a dependency; only `settings.reva_duckdb_memory_limit` references it | Nothing violates the rule because nothing uses DuckDB. Either use it for one analytical read over a pinned manifest or document it as reserved (lead). |

## 4. Prompt §6 — Connectors

| Source / requirement | Status | Evidence | Gap / remedy (owner) |
|---|---|---|---|
| ConnectorSpec/FetchPlan/RawAcquisition/ObservationBatch/QualityReport; six-method protocol | done | `ports/connectors.py`; all connectors implement `describe/plan/fetch/parse/validate/next_cursor` | — |
| **WB** v2 country/indicator metadata and series, FRA/DZA/CHN/USA | done | `adapters/connectors/worldbank.py` | — |
| **WB** SP.POP.TOTL and NY.GDP.MKTP.CD, 2000–2024 | done | `INDICATORS`; live check `var/live/live_checks.md` (25 years × 4) | — |
| **WB** full pagination; country/source metadata, missing values, source notes preserved | done | follow-up pages; `sourceNote`; `test_http_connectors::test_wb_pagination_truncation_blocks`, `::test_wb_null_values_stay_missing` | Row-level drift crashes the run (strict xfail `test_http_connectors.py:384`). **Remedy:** catch parse `KeyError`/`ValueError` per item and quarantine it (lead `evidence/runner.py`). |
| **WB** population support/definition from indicator metadata | done | `definition_must_contain`; `test_wb_semantic_drift_in_indicator_metadata_blocks` | — |
| **Comtrade** exact annual goods/HS/2709 M/X, partner 0 | done | `comtrade.py` `_url`, `FIXED_DIMS` | — |
| **Comtrade** reporter codes FRA=251, DZA=12, CHN=156, USA=842 verified against provider metadata | done | `REPORTERS`; `registry/reference/comtrade_partnerAreas.json`; live gate `comtrade_reporter_codes_verified: True` | — |
| **Comtrade** totals 2020–2024, bilateral 2023 | done | One request per year (D-2); bilateral `required=False` | Bilateral requests omit `partnerCode` (all partners), so they are not "exact keys" in the strict sense (D-3). |
| **Comtrade** classification revision and aggregation/estimation flags recorded | done | `_flags` (`classificationCode`, `isReported`, `isAggregate`, `legacyEstimationFlag`, …) | — |
| **Comtrade** CIF/FOB/primaryValue preserved separately; primary series with basis | done | flags keep all three; `_valuation` (flow-aware fix, R-23 / `test_evidence_fixes::test_r23_*`) | — |
| **Comtrade** no barrels from value; null/zero weights stay Missing | done | `netWgt <= 0` gives Missing plus `weight_unusable`; `ontology/units` refuses kg→barrel without density and convention | Tests: `test_comtrade_unusable_weight_stays_missing`, `test_ontology_units::test_kg_to_barrel_refused_without_density_and_convention`. |
| **Comtrade** preview completeness only for bounded exact keys; ambiguity blocks | done | `validate`: count mismatch, `n >= maxRecords`, dimension mismatch, duplicates are all blockers | Tests: `test_comtrade_count_mismatch_blocks`, `::test_comtrade_max_records_truncation_blocks`, `::test_comtrade_dimension_mismatch_blocks`, `::test_comtrade_duplicate_rows_block`. |
| **Comtrade** missing flow is missing, not zero | done | `provider_returned_no_rows` gives Missing | `test_comtrade_missing_flow_stays_missing_never_zero`; API `test_dza_crude_import_belief_is_typed_missing`. |
| **Comtrade** all-four request returns `missing_input` for DZA; never synthesize | done | `simulation/compile.py`; `test_simulation_engine::test_all_four_countries_returns_missing_input_for_dza` | **New fact:** the 23:29 live run shows DZA **2024** rows (imports US$5,564, exports US$14.18 bn). 2023 is still missing, so the negative test holds only while reference year 2023 is pinned. The US$5,564 import value is implausible; see D-4. |
| **Comtrade** partner-total and bilateral are distinct supports; never add both | done | Distinct concepts and supports; bilateral parser skips partner 0; `world/graph` refuses the sum | `test_world_graph_core::test_world_total_plus_bilateral_is_refused`, `test_ontology_validation::test_world_total_and_bilateral_cannot_be_summed`. |
| **Natural Earth** pinned low-res asset by hash; terms retained | done | Raw artifact sha → `world.display_geometry.dataset_sha256`; `registry/sources/naturalearth.yaml` rights | No pre-declared expected hash: whatever is downloaded becomes the pin. Acceptable; release-pinned. |
| **Natural Earth** reviewed provider keys; unmatched/disputed/overseas explicit | done | `match_status` CHECK (`reviewed_match/unmatched/disputed/overseas_part`); `world/space.py` notes | — |
| **Natural Earth** display only, not authoritative | done | ADR 0010; `MapView.tsx` disputed layer; component `display_geometry.v1` | — |
| **Crossref** DOI metadata for 10.1257/aer.99.3.1053 | done | `crossref.py`; fixture | — |
| **Crossref** cache; honour rate/concurrency headers and 429 | partial | `RateLimiter.adapt_from_headers` (X-Rate-Limit); 429/Retry-After in `SafeHttpClient`; `test_ssrf_http::test_429_retry_after_is_honoured_then_succeeds` | No HTTP cache or conditional request for Crossref; only acquisition-level dedup and the weekly cadence. **Remedy:** send `If-None-Match`/`If-Modified-Since` from the last acquisition's response metadata, as Natural Earth does (lead). |
| **Crossref** reviewed candidate EvidenceCard, no invented coefficients | done | `registry/evidence/kilian2009.yaml` (all quantitative fields `not_extracted`); `evidence/cards.py` `_no_fabrication` | — |
| **Eurostat** official catalogue discovery plus approved JSON-stat template | done | `autonomy/discovery.EurostatCatalogueDiscoverer`, `adapters/connectors/eurostat_template.py` | — |
| **Eurostat** locate demo_pjan for France; generate manifest; sample, validate, capture fixture | done | `test_eurostat_discovery::test_discovers_demo_pjan_from_captured_catalogue`, `::test_manifest_validates_against_contract_schema`, `::test_validator_passes_captured_sample` | `test_end_to_end_promotion_ingestion_and_belief_separation` was failing at 23:17 (G, in progress). |
| **Eurostat** pin dimensions/order/status flags; 1 January support; provisional flags | done | Template docstring and `structure_fingerprint`; flags kept verbatim | — |
| **Eurostat** demonstrate non-identity with WDI mid-year | done | `registry/mappings/eurostat.yaml` (`incompatible`); `test_eurostat_discovery::test_wdi_midyear_equivalence_is_incompatible`; `test_ontology_validation::test_january_and_midyear_population_are_not_identical_concepts` | Live release at 23:29 has `R-POP-JAN-AGESEX` **absent**, so the Eurostat data did not reach a live belief release in that run (G/lead). |
| All connectors use SafeHttpClient; reject private redirects, oversize, decompression bombs, malformed schema | done | `adapters/http/safe_client.py`, `archive.py`; `tests/security/test_ssrf_http.py` (36 tests) | DNS TOCTOU (R-12) and decompression memory (R-13) are open per agent R. |
| Default cadences (WDI monthly, Comtrade monthly + recent-year rechecks, NE weekly, Crossref weekly, Eurostat weekly) | partial | `cli/seed.DEFAULT_SCHEDULES` (WDI/Comtrade 30 d, NE/Crossref 7 d) | No Eurostat weekly metadata/update schedule. Comtrade "rolling recent-year" is implicit (the plan re-fetches all years every month). **Remedy:** add `ingest:eurostat.*` or `discovery:eurostat` weekly schedules for promoted manifests (G/lead). |
| Conditional requests where supported | partial | Natural Earth only (`If-None-Match`/`If-Modified-Since`) | Extend to Crossref and Eurostat (lead/G). |

## 5. Prompt §7 — Queue, scheduler, incremental computation

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| SKIP LOCKED short transaction; release locks before work | done | `adapters/postgres/queue.py`; `test_queue_reliability::test_claim_commits_and_releases_row_locks_before_work` | — |
| Lease expiry, heartbeat, monotonic fencing, attempts, dead letter | done | `ops.fencing_seq`; `ops.assert_lease/heartbeat/complete_job`; `test_expired_lease_*`, `test_retry_with_exponential_backoff_then_dead_letter` | — |
| Idempotency key = job type + immutable input/spec digests | done | `queue.idempotency_key`; `ops.submit_job` refuses key reuse with a different request | — |
| Durable checkpoints | done | `ops.job.checkpoint`, `operations/jobs.save_checkpoint` | — |
| Priority with starvation protection; one active job | done | Aging term in the claim ordering; `REVA_WORKER_CONCURRENCY=1`; `test_priority_and_starvation_protection` | — |
| Graceful shutdown/cancellation; bounded retry | done | `operations/worker.py`; `test_worker_loop::test_sigterm_stops_claiming_signals_handler_and_drains`; `test_cancel_running_job_is_honoured_cooperatively` | — |
| Result publication verifies owner/token/lease in same transaction; DB clock | done | `assert_lease` uses `clock_timestamp()`; `test_simulation_engine::test_stale_fencing_token_cannot_publish`, `test_replay_job::test_stale_lease_cannot_publish_a_replay` | — |
| Scheduler: schedule key + due slot idempotent; transactional advance; no duplicates | done | `operations/schedules.tick`; `test_two_schedulers_create_exactly_one_job_per_slot` | — |
| Outbox/dependency invalidations in publication transaction; affected nodes only; cycles invalidate coupled component; materiality error budget | done | `evidence/ingest` outbox and `ops.dirty_node`; `operations/invalidation.py`; `test_queue_invalidation.py` (5 tests) | R-06 (lost dirty nodes) has a fix test (`test_r06_*`). |

## 6. Prompt §8 — Beliefs and exact temporal replay

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| `source_select_v1` registered; publishes `selected_report` with original uncertainty | done | `belief/selection.py`, `belief/publication.py`; `model.version` `belief.source_select@1.0.0` (seed) | — |
| As-of: original release at/before cutoff → pinned inputs → revision selection within pre-cutoff observations | done | `belief/snapshots.release_as_of`; `selection.ELIGIBLE_SQL` filters before ranking; `test_revisions::test_two_revisions_yield_different_as_of_answers` | That test **fails** at 23:31 (H1/lead, in progress). R-03 (commit-time vs `recorded_at`) fix is in progress (`evidence/locks.py`, `test_r03_*`). |
| Three replay modes; never silently downgrade strict | done | `evaluation/replay.py`, `cutoff.py`, `leakage.py`; `test_leakage_cases::test_strict_never_downgrades` | `test_operational_replay_job_writes_evaluation_context_and_manifest` fails at 23:31 (H1). |
| Available-then requires archived availability proof; no proof means exclusion | done | `test_available_then_excludes_records_without_availability_proof` | — |
| Six leakage fixtures (revision, renamed boundary, later scaler, later model, later paper coefficient, population recalibration) blocked in strict mode | done | `tests/fixtures/vintages/*`; `test_leakage_cases::test_six_leakage_cases_blocked_in_strict_mode` | — |
| Rolling retrospective evaluation labelled `revised_data_retrospective` in API, UI and report | done | `RETROSPECTIVE_LABEL`/disclaimer; `ui/src/pages/ModelsPage.tsx`; `test_revised_data_retrospective_is_labelled_everywhere` | — |

## 7. Prompt §9 — Numerical models and simulations

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| `last_value_v1`, `log_drift_v1`, `local_level_log_v1`; explicit positive-series logs; in-fold preprocessing; ineligible on short series | done | `models/forecasting/*`; `tests/unit/forecasting/*` | — |
| `economic.crude_exposure` 1.0.0 formulas (L1) | done | `models/economic/crude_exposure.py` (50-digit Decimal) | — |
| L2 elasticity formula; L1 as ε=0 special case | done | `exposure_l2`; `test_zero_elasticity_l2_equals_l1_exactly` | — |
| No GDP-loss/unemployment/inflation/welfare outputs | done | `NOT_CLAIMED`; outputs limited to 4 keys; ADR 0007 | — |
| L0 exact-spec cache; L1 deterministic; L2 labelled sensitivity; L3/L4 unavailable in descriptors | done | `simulation/engine.py`; `capabilities.py` (`fidelity.L3/L4`); `test_simulation_engine::test_l0_cache_miss_then_hit_after_l1`, `::test_l3_and_l4_rejected` | L0 cache key excludes `fidelity` (D-11). |
| Validate reference year, one-period horizon, current-USD basis, p>0, registered envelopes; scope checks after schema | done | `validate_country_inputs`, `check_horizon`, `_check_multiplier/_check_elasticity`; `simulation/spec.py` | — |
| Resolve a real baseline (fixture UUID rejected) in UI/CLI | done | `cli/demo.py` (`release_as_of`); `ui/src/lib/scenario.ts` rejects `FIXTURE_BASELINE_UUID`; `simulation/spec.FIXTURE_BASELINE` | — |
| Run pins baseline, model/ontology manifests, intervention, seed streams, execution profile, output digest | done | `sim.run` and `sim.run_outcome` columns; `simulation/rng.py`; `test_seed_plan_records_streams` | — |
| Results explain signs, units, assumptions, unavailable uncertainty, source flags; simulation-context isolation | done | `ce.explain`, `_applicability`; RLS plus `reva_sim` grants; `test_sim_role_cannot_write_evidence_or_beliefs` | — |
| Acyclic coupling; port compatibility; SCC detection; reject feedback | done | `simulation/coupling.py`; `test_crude_exposure_coupling_rng.py`; `extensions/feedback.py` refuses | — |

## 8. Prompt §10 — Population, compression, autonomy

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Recipe from validated Eurostat age × recorded-sex leaf cells; no totals/subtotals double counted | done | `population/recipe.py`; `test_population_recipe::test_only_disjoint_leaves_are_counted` | — |
| ≤100,000 weighted records; unknown/top-coded retained; exact per-cell weight sums; resolution error/coarsening suggestion | done | `population/materialise.py` (Fraction weights, `ResolutionError` with `suggested_coarsening`); `test_population_materialise.*`; property test | — |
| Deterministic aggregation/refinement, calibration diagnostics, ESS, weight concentration; no invented joint dimensions/attributes | done | `compression/{stratified,refine,certificates}.py`, `population/diagnostics.py`, `calibration.py`; `test_refine_along_unsupported_dimension_raises`, `test_forbidden_attributes_rejected` | — |
| End-to-end service (Eurostat cells → Parquet artifact → `model.population_dataset` → certificates) | partial | `population/service.py` (lead), job type `population` | **No integration test** runs `build_population`/`handle_population` against a database. The live release had no Jan-1 cells (§4). **Remedy:** add `tests/integration/test_population_service.py` using the Eurostat fixture (lead). |
| All 16 autonomy protocols with concrete V1 behaviour | done (verify) | `src/reva/autonomy/__init__.py` mapping table; `tests/integration/test_autonomy_contracts.py::test_protocol_conformance` | See §8.1. |
| Pure declarative generation; real no-secret validation runner; external-runner adapter; arbitrary code → blocked | done | `autonomy/generator.py`, `sandbox.py` (`TrustedTemplateSandbox`, `HttpExternalRunner`); `test_sandbox_isolation::test_generated_code_is_blocked_without_external_runner` | `test_worker_never_imports_proposed_code` was failing at 23:17 (G, in progress). |
| T0/T1/T2/T3 tiers exactly; LLM cannot change its gates, approve causal claims, access secrets, auto-merge kernel/security | done | `registry/policies/promotion.yaml`, `autonomy/policy.derive_tier`, `gate.py`; DB: gate evaluator ≠ proposer, human reviewer prefix; `test_autonomy_gates.py` (16 tests) | `promotion.yaml` refers to a `policies/digests.yaml` that does not exist; the pins live in code (`autonomy/policy.py`). **Remedy:** fix the comment or add the file (G). |
| Gap catalogue; analytic VOI with a known answer; heuristic score without utility model | done | `registry/requirements/mvp.yaml`; `autonomy/gaps.py`, `voi.py`; `evaluation/voi.py`; `test_voi_analytic.py` | Requirement catalogue lacks the "model applicability" field required by architecture §20.1 (lead). |

### 8.1 The sixteen autonomy protocols

| Protocol | Implementation | Status | Note |
|---|---|---|---|
| SourceDiscoverer | `autonomy/discovery.EurostatCatalogueDiscoverer` | done | Allowlisted Eurostat TOC; `test_discovers_demo_pjan_from_captured_catalogue` |
| ConnectorGenerator | `autonomy/generator.DeclarativeConnectorGenerator` | done | `test_generator_refuses_unapproved_template` |
| ConnectorValidator | `autonomy/validator.JsonStatConnectorValidator` | done | Pass, fail and blocked paths tested |
| ConnectorHealthMonitor | `autonomy/health.RunHistoryHealthMonitor` (+ `operations/health.py`) | done | Health vector; `test_health_vector`, `test_health_job_appends_cadence_relative_vector` |
| OntologyMapper | `autonomy/mapping.RegistryOntologyMapper` | done | Exact plus candidates; incompatible WDI case |
| EvidenceExtractor | `autonomy/evidence.StructuredCardImporter`, `LlmEvidenceExtractor` (blocked by default) | done | `test_injected_paper_text_yields_only_a_candidate` |
| DataQualityEvaluator | `autonomy/quality.DimensionRuleQualityEvaluator` | done | `test_data_quality_dimension_rules` |
| ModelRegistry | `autonomy/models.PostgresModelRegistry`; `models/registry.py` | done | Approval required, immutable |
| ModelEvaluator | `autonomy/models.TournamentModelEvaluator` | done | Proper metrics |
| BacktestRunner | `autonomy/models.TournamentBacktestRunner` | **partial** | Supports only `revised_data_retrospective`; strict and available-then return typed `blocked` ("needs vintage replay (agent H1)"). **Remedy:** route those modes to `evaluation/replay.py` (G with H1). |
| KnowledgeGapDetector | `autonomy/gaps.CatalogueGapDetector` | done | `test_gap_detector_against_database_finds_dza_crude_missing` was failing at 23:17 (G). |
| ValueOfInformationEstimator | `autonomy/voi.GapValueEstimator` | done | EVSI only with a decision spec, otherwise a proxy |
| ModelChallenger | `autonomy/challenger.BoundedModelChallenger` | done | Bounded T2 proposals |
| PlatformChangeProposal | `autonomy/proposals.ProposalStore` | done | Persisted in `ops.proposal` (immutable) |
| SandboxRunner | `autonomy/sandbox.TrustedTemplateSandbox` (+ `HttpExternalRunner`) | done | Code → external runner or blocked |
| PromotionGate | `autonomy/gate.PolicyPromotionGate` | done | Pinned-policy digest; independent reports |

## 9. Prompt §11 / architecture §19 — API, UI, explanations

### 9.1 Endpoints (architecture §19 table)

All routes are mounted from `src/reva/api/app.py`. Test coverage is in `tests/integration/test_api_endpoints.py` (17 tests) and `tests/security/test_auth_session.py` (16 tests). The API integration tests were erroring at the snapshot because of the bootstrap churn, not the routes; re-run them.

| Endpoint | Status | Route file | Note |
|---|---|---|---|
| GET /health/live, /health/ready | done | `routes/system.py` | Public; ready checks DB/schema |
| GET /v1/entities, /v1/entities/{id} | done | `routes/world.py` | Keyset pagination (`deps.encode_cursor`) |
| GET /v1/relations, /v1/events | done | `routes/world.py` | Truncation flag (`test_relations_bounded_with_truncation_flag`) |
| GET /v1/observations, /v1/timeseries | done | `routes/evidence.py` | Vintage filters (`test_timeseries_revision_filtering`) |
| GET /v1/beliefs, /v1/belief-releases/{id} | done | `routes/beliefs.py` | Also lists `/v1/belief-releases`. `release_by_id` serves unpublished releases (strict xfail). |
| GET /v1/evidence, /v1/evidence/{id} | done | `routes/evidence.py` | |
| GET /v1/provenance/{kind}/{id} | done | `routes/provenance.py` | Bounded, `truncated` flag; reaches raw artifact and selector |
| GET /v1/locations | done | `routes/world.py` | |
| GET /v1/models, /v1/models/{id}/evaluations | done | `routes/catalog.py` | |
| POST /v1/scenarios | done | `routes/sim.py` | Through `sim.create_scenario`. That function trusts the caller's digest (strict xfail, `test_roles_grants.py:827`). **Remedy:** recompute the digest in SQL (lead migration). |
| POST /v1/simulations | done | `routes/sim.py` | 202 plus Idempotency-Key; 422 `CapabilityGap` |
| GET /v1/simulations/{id} | done | `routes/sim.py` | |
| POST /v1/replay | done | `routes/sim.py` | 202 |
| GET /v1/sources, /v1/connectors | done | `routes/catalog.py` | |
| GET /v1/ontology | done | `routes/catalog.py` | |
| GET /v1/coverage, /v1/gaps | done | `routes/catalog.py` | Catalogue-relative |
| POST /v1/discovery | done | `routes/sim.py` | 422 if autonomy disabled |
| GET /v1/proposals, POST /v1/proposals/{id}/review | done | `routes/catalog.py` | Named human reviewer |
| POST /v1/query | done | `routes/sim.py` (`query_router`) | Typed QuerySpec; unknown kind → 422 |
| GET /v1/jobs/{id}, POST /v1/jobs/{id}/cancel | done | `routes/system.py` | |
| POST/DELETE /v1/session | done | `routes/system.py` | Argon2id, HttpOnly/SameSite=strict, CSRF, rate limit, audit |
| Extra (beyond spec) | — | `/v1/scenarios` (GET), `/v1/discovery/candidates`, `/v1/evaluations` (POST), `/v1/jobs`, `/metrics` (scope-protected) | Additive, fine. |

| Contract item | Status | Evidence / gap |
|---|---|---|
| Generated OpenAPI plus typed UI clients | done | `scripts/export_contracts.py` → `contracts/openapi.json`; `ui/src/api/openapi.gen.ts`, `contract.check.ts` |
| Long operations: 202/jobs, idempotency, cancellation | done | — |
| Unknown capabilities → typed 422 | done | `api/errors.py` maps `CapabilityGap` |
| Response envelope fields (data, context_id, modality, estimand, units, support, baseline_release, model_versions, assumptions, uncertainty, data_freshness, evidence_profile, applicability, numerical_error, compression_error, model_disagreement, historical_validation, provenance_links, warnings, cost) | done | `api/schemas.Envelope` |
| Auth: Argon2id; short-lived secure HttpOnly sessions; CSRF; scopes; no tokens in localStorage; no public unauthenticated simulation/ingestion | done | `api/auth.py`; `test_auth_session.py`; `ui/src/api/http.ts` |

### 9.2 The seven screens and display requirements

| Screen | Status | File | Note |
|---|---|---|---|
| World | done (F active) | `ui/src/pages/WorldPage.tsx`, `MapView.tsx` | Local GeoJSON via API; no tile service |
| Country | done | `CountryPage.tsx`, `TimeSeriesChart.tsx` | |
| Evidence | done | `EvidencePage.tsx` | |
| Graph | done | `GraphPage.tsx`, `GraphView.tsx` (Cytoscape) | |
| Simulation | done | `SimulationPage.tsx` (+ test) | Persistent banner; CSV export carries scenario context |
| Models | done | `ModelsPage.tsx` | Retrospective label |
| Acquisition | done | `AcquisitionPage.tsx` | |

| Every-result display field | Status | Evidence | Gap |
|---|---|---|---|
| Modality, context, valid/reference time, knowledge cutoff | done | `ContextBanner.tsx` (+ test), `EnvelopeDetails.tsx` | — |
| Units and definition | done | `DefinitionPopover.tsx`, `lib/concepts.ts` | — |
| Uncertainty status/type (sensitivity ≠ CI) | done | `UncertaintyPanel.tsx` (+ test) | — |
| Assumptions and applicability | done | `EnvelopeDetails.tsx` | — |
| Model version and historical validation | done | Envelope `model_versions`, `historical_validation` | Mostly "not applicable" placeholders outside the Models page; acceptable. |
| Source freshness and provenance drilldown | done | `ProvenanceDrawer.tsx`; API provenance | "Provenance drawer reaches source bytes/selectors" has an API test but no UI e2e test in CI (see §14, check 10). |
| Fixture mode visibly labelled; missing never zero; negative/degraded states | done | `ContextBanner` (fixture), `MissingValue.tsx` (+ test), `StateViews.tsx` | — |
| Graph bounds and hyperedge roles; disputed boundaries not implied resolved | done | `world/graph.py` (depth ≤ 4, 500 edges); `MapView` disputed layer | — |

## 10. Prompt §12 — Deployment and operations

| Requirement | Status | Evidence | Gap / remedy (owner) |
|---|---|---|---|
| Full `render.yaml` appendix (not an approximation) | done | Identical to the appendix (diffed); validated against cached official schema | **Conflict:** see D-13. `settings.py` (23:26) now *requires* `REVA_DB_SSLMODE` in production, and non-API processes need `REVA_PROCESS_ROLE` to skip the session-secret rule. Neither is set by `render.yaml` or the start scripts, so a deploy as written would fail startup validation. **Remedy:** either set `REVA_PROCESS_ROLE` in `start_worker.py`/`start_api.py`/`bootstrap_runtime.py` and add `REVA_DB_SSLMODE` to the `reva-common` group (minimal documented Blueprint change), or relax the validator (lead + I). |
| `bootstrap_runtime.py`: advisory lock, capability/PostGIS check, migrations as owner, idempotent roles with generated passwords, grants, ownership check, release lock, report schema version without secrets | done | `scripts/bootstrap_runtime.py` → `cli/bootstrap.py`; `test_start_env::test_bootstrap_runtime_reports_schema_without_secrets`; `test_db_hardening::test_scram_verifier_matches_rfc7677` | Being edited at 23:36 (R-17 fix: SCRAM verifier instead of plaintext password in SQL). |
| `start_api.py`: validate, safe DSN, scrub env, exec Uvicorn 0.0.0.0:$PORT 1 worker, test child env | done | `scripts/start_api.py`, `_runtime_env.py`; `test_start_env::test_start_api_subprocess_child_environment` | — |
| `start_worker.py`: refuse owner URL, bounded schema wait, worker + sim connections, explicit thread budget/cgroup, SIGTERM drain | done | `scripts/start_worker.py`; `test_start_worker_refuses_owner_url_in_production`, `test_wait_for_schema_is_bounded`, `test_thread_budget_is_explicit_and_capped_by_cgroup`; `test_worker_loop` | — |
| API object credentials read-only by prefix; worker prefix-write; sandbox none; document exact inputs | partial / blocked-external | `docs/deployment.md` | IAM policies cannot be tested without a provider. Check that `docs/deployment.md` lists exact per-prefix policies (I). |
| Backup/restore commands; tested restore | done | `scripts/backup.sh`, `restore.sh`, `backup_restore.py`; `test_backup_restore.py` | `test_backup_restore_reconstructs_manifests_and_as_of` failed at 23:17 (bootstrap churn); re-run. |
| JSON logs, metrics, traces with bounded cardinality | partial | `operations/telemetry.py` (JSON logs, Prometheus registry); `test_queue_telemetry.py` | **No OpenTelemetry traces**: `OTEL_EXPORTER_OTLP_ENDPOINT` is not read anywhere. **Remedy:** add an optional OTel span exporter, or document traces as deferred (D/I). |
| Record actual latency/RSS/queue/cost measurements | partial | `tests/performance/*`, `scripts/measure_performance.py`; restore timing in `docs/operations.md` | p95/RSS/queue numbers are not recorded in docs. `test_perf_queue::test_queue_acknowledgement_under_1s` failed at 23:31. **Remedy:** run `measure_performance.py` and record results with hardware in `docs/operations.md` (I). |

## 11. Prompt §13 — Mandatory acceptance tests

| Group | Requirement | Mapped test(s) | Status |
|---|---|---|---|
| Scientific | M=100,X=40,G=1000,p=1.25 → ΔM=25, ΔX=10, Δnet=15, 1.5% | `tests/scientific/test_crude_exposure.py::test_hand_computed_l1_exact`; `tests/integration/test_simulation_engine.py::test_l1_exposure_matches_hand_computation` | done |
| Scientific | All multipliers 1 → zero | `test_crude_exposure.py::test_unit_multipliers_give_zero` | done |
| Scientific | Zero elasticities recover L1 exactly | `test_crude_exposure.py::test_zero_elasticity_l2_equals_l1_exactly` | done |
| Scientific | Quantity/valuation missingness blocks unsupported conversions | `test_ontology_units.py::test_kg_to_barrel_refused_without_density_and_convention`, `::test_currency_without_basis_rejected`; `test_http_connectors.py::test_comtrade_unusable_weight_stays_missing` | done |
| Scientific | Different reference years or GDP bases not silently combined | `test_crude_exposure.py::test_mixed_reference_years_rejected`, `::test_mixed_basis_rejected`; `test_ontology_validation.py::test_countries_with_different_reference_years_cannot_be_combined`, `::test_countries_with_different_gdp_bases_cannot_be_combined` | done |
| Scientific | January vs mid-year population not identical | `test_ontology_validation.py::test_january_and_midyear_population_are_not_identical_concepts`; `test_eurostat_discovery.py::test_wdi_midyear_equivalence_is_incompatible` | done |
| Scientific | Probability, credible, prediction intervals, bounds, sensitivity distinct | `tests/scientific/test_uncertainty_types.py` (8 tests) | done |
| Temporal/storage | Two revisions → different as-of answers | `test_revisions.py::test_two_revisions_yield_different_as_of_answers` | partial (test **failing** at snapshot; H1/lead) |
| Temporal/storage | A→B→A three revisions; identical polls none | `test_revisions.py::test_aba_creates_three_revisions_identical_polls_create_none` | done |
| Temporal/storage | Old raw with new parser distinguished from original vintage | `test_revisions.py::test_replayed_raw_with_new_parser_is_distinguished_from_original_vintage`; `test_evidence_fixes.py::test_r28_parser_version_change_reprocesses_same_bytes` | done |
| Temporal/storage | Staged/uncommitted objects invisible | `test_storage_publication.py::test_crash_after_upload_before_commit_is_invisible_then_retry_publishes_once` | done |
| Temporal/storage | Missing publication time never filled with reference date | `test_revisions.py::test_missing_source_publication_time_is_never_the_reference_date` | done (was failing at 23:17, passing at 23:31) |
| Temporal/storage | Scientific UPDATE/DELETE fails under runtime roles | `test_roles_grants.py::test_runtime_roles_cannot_update_delete_truncate_history` | done |
| Isolation/security | Sim role cannot insert evidence/beliefs | `test_roles_grants.py::test_sim_cannot_insert_evidence_or_beliefs`; `test_simulation_engine.py::test_sim_role_cannot_write_evidence_or_beliefs` | done |
| Isolation/security | Generated code gets no production credentials or network by default | `test_sandbox_isolation.py::test_trusted_template_env_has_no_secrets_and_network_is_template_only`, `::test_generated_code_is_blocked_without_external_runner` | done (one sibling test failing, G) |
| Isolation/security | Prompt injection in a paper cannot change policy/allowlist | `test_prompt_injection.py::test_injected_paper_text_yields_only_a_candidate`, `::test_injected_catalogue_and_metadata_change_nothing` | done |
| Isolation/security | SSRF, redirect-to-private-IP, zip bomb, SQL injection, secret-log tests | `test_ssrf_http.py` (36), `test_sql_injection.py`, `test_secrets_logs.py` | done (SQL-injection tests erroring at 23:17 on bootstrap; re-run) |
| Isolation/security | App startup env has no migration owner password/URL | `test_start_env.py::test_api_child_env_keeps_only_api_credentials`, `::test_start_api_subprocess_child_environment` | done |
| Reliability | Kill worker after upload and after commit → one visible result | `test_queue_reliability.py::test_kill_after_object_upload_then_retry_yields_one_acquisition`, `::test_kill_after_db_commit_before_ack_yields_one_acquisition` | done |
| Reliability | Expired lease/fencing holder cannot publish | `test_queue_reliability.py::test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit` | done |
| Reliability | Two workers claiming a due job → no duplicate effects | `test_queue_reliability.py::test_concurrent_claims_never_return_the_same_job`, `::test_two_schedulers_create_exactly_one_job_per_slot` | done |
| Reliability | Rate limiting, HTML-200 error page, schema drift → retries/quarantine | `test_ssrf_http.py::test_429_retry_after_is_honoured_then_succeeds`, `::test_html_error_page_with_200_is_a_failure`; `test_http_connectors.py::test_runner_quarantines_truncated_page_and_html`, `::test_wb_schema_drift_is_a_failure` | partial (row-level drift crashes the run: strict xfail) |
| Reliability | Restore reconstructs identical logical manifests and as-of results | `test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of` | done, pending re-run (failed during bootstrap churn) |
| Population | Leaf totals conserved; no double-counted totals | `test_population_materialise.py::test_leaf_totals_conserved_exactly`; `test_population_recipe.py::test_only_disjoint_leaves_are_counted`; property test | done |
| Population | Structural-zero and unknown-category handling | `test_population_recipe.py::test_observed_zero_is_structural_zero_not_missing`, `::test_unknown_and_top_coded_retained_explicitly` | done |
| Population | Same seed/config → same logical cohort | `test_population_materialise.py::test_same_inputs_identical_outputs` | done |
| Population | Coarsen/refine preserves weights; reports unsupported detail | `test_population_compression.py::test_coarsen_then_refine_round_trip`, `::test_refine_along_unsupported_dimension_raises` | done |
| Evaluation/UI | Six future-information leakage cases blocked | `tests/replay/test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode` | done |
| Evaluation/UI | Naive baseline present; insufficient evidence does not promote challenger | `test_forecast_tournament.py::test_naive_baseline_added_when_not_a_candidate`, `::test_short_panel_keeps_incumbent_insufficient_evaluation`; `test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent` | done |
| Evaluation/UI | Provenance drawer reaches source bytes/selectors | API: `test_api_endpoints.py::test_provenance_reaches_raw_artifact_and_selector`; UI: `ui/e2e/smoke.spec.ts` (not in CI) | partial: run Playwright in CI (F/I) |
| Evaluation/UI | Every simulation screen and export has scenario context | `ui/src/pages/SimulationPage.test.tsx`, `ContextBanner.test.tsx` | done |
| Evaluation/UI | Negative/degraded states render; no stub success | `MissingValue.test.tsx`; `test_queue_reliability.py::test_missing_handler_is_a_typed_failure_never_success`; `test_capabilities.py` | done |
| Performance | Measure on named target resources | `tests/performance/test_perf_api.py` (1 CPU, 0.5 CPU quota), `scripts/measure_performance.py` | partial (no recorded results; I) |
| Performance | 100k-record memory/time; API and L1 targets | `test_population_perf.py::test_materialise_100k_records_france_like`; `test_perf_simulation.py::test_l1_four_country_calculation_under_2s`; `test_perf_api.py`; `test_perf_queue.py` | partial (queue ack test failing at snapshot) |
| Performance | Report actual values; no weakened thresholds without ADR | — | missing (no measurement record; I) |

## 12. Architecture §24.1 — Invariants 1–35

`docs/data_constitution.md` (agent I) already maps every invariant to a test, and every cited test exists. The table below checks those claims against the code. Where my verdict differs, I say why.

| # | Invariant (short) | Enforcement point | Test | Audit status |
|---|---|---|---|---|
| 1 | Complete provenance or explicit blocker | `ops.check_publication` (estimates need inputs); `raw_selector` artifact check | `test_publication_rejects_estimates_without_provenance`, `test_provenance_reaches_raw_artifact_and_selector` | partial: simulation results' provenance chain to the baseline is not tested; selectors are not resolved (check 8) |
| 2 | No overwriting observation/model/scenario/release | `meta.reject_mutation` triggers | `test_runtime_roles_cannot_update_delete_truncate_history`, `test_owner_cannot_mutate_immutable_records_either` | done |
| 3 | No scenario/evaluation result as evidence | `belief.input` FKs; `reva_sim` grants; RLS | `test_belief_input_cannot_reference_simulation_or_evaluation_outputs` | done |
| 4 | No source report labelled certain | `uncertainty.status` kept (`unquantified`), modality `selected_report` | `test_uncertainty_types.py` | done |
| 5 | No missing → zero | `kernel.values.Missing`, `require_number`, DB CHECK | see data_constitution row 5 | done |
| 6 | No unit/basis change without versioned transform | `ontology/units.py` | `test_ontology_units.py` | done |
| 7 | No aggregation across incompatible support/classification/universe | `ontology/validation.py`, `world/graph` aggregation, model input checks | `test_ontology_validation.py`, `test_world_graph_core.py` | done |
| 8 | No correlation labelled causal | Modality enum; card `evidence_kind`; no causal model | `test_world_graph_core::test_sequence_relations_never_claim_causation` | partial (structural only) |
| 9 | No loss of uncertainty metadata | Selection copies source uncertainty; envelope | `test_uncertainty_types::test_specs_are_immutable` | partial: observation export drops or alters fields (strict xfail `test_json_schemas.py:204`) |
| 10 | No synthetic/private identity linkage | Recipe allowlist; privacy checks | `test_forbidden_attributes_rejected` | done |
| 11 | No model execution fetching live inputs | import-linter contracts (`reva.models`, `reva.simulation`) | `scripts/verify_imports.py` (CI) | partial: `lint-imports` failed at 22:59 (missing `autonomy/__init__`, now present); re-run |
| 12 | No replay with unavailable revisions/later preprocessing | `evaluation/cutoff.py`, `leakage.py` | `test_leakage_cases.py`, `test_forecast_no_lookahead.py` | done |
| 13 | No source-family duplicates as independent | `lineage_family` on sources; gate | none specific | **missing**: no test asserts that two sources in one lineage family are not counted as independent. `source_select_v1` only flags `multiple_lineages_same_source`. **Remedy:** add a gate/selection check and test (G/lead). |
| 14 | No arbitrary code from documents/agents in production | Template-only; `contains_code` → external runner or blocked | `test_sandbox_isolation.py`, `test_prompt_injection.py` | done |
| 15 | No self-promotion / gate editing | Pinned policy digest; `ops.check_gate_evaluator`; `ops.review_proposal` human prefix | `test_autonomy_gates.py::test_proposer_cannot_write_its_own_gate_result`, `::test_policy_change_makes_gate_refuse` | done |
| 16 | No kernel/ontology mutation without version | Immutable `concept_version`; component digests | `test_ontology_components.py`, `test_registry_components.py` | partial: `test_published_versions_match_pinned_digests` **fails** now (a v1 schema was edited in place; K) |
| 17 | No silent truncation | Count/pagination checks; `truncated` flags | `test_wb_pagination_truncation_blocks`, `test_comtrade_max_records_truncation_blocks`, `test_relations_bounded_with_truncation_flag` | done |
| 18 | No partial multi-object publication visible | Objects first, single transaction, sealed releases | `test_storage_publication.py`, `test_published_release_is_sealed` | done |
| 19 | No commit after losing fencing token | `ops.assert_lease` | queue/sim/replay stale-lease tests | done |
| 20 | No model outside declared support | Envelopes; `CapabilityGap` | `test_envelopes_and_horizon`, `test_l3_and_l4_rejected`, `test_model_registry::test_out_of_envelope_requests_are_refused_with_reasons` | done |
| 21 | No fabricated probabilities | Sensitivity has no level | `test_sensitivity_range_not_probabilistic` | done |
| 22 | No interval conflation | Distinct kinds | `test_uncertainty_types.py`, `test_forecast_metrics::test_coverage_aggregation_rejects_non_prediction_intervals` | done |
| 23 | No provider code as universal ID | Provider-scoped aliases; UUIDv5 over provider/key/epoch; R-10 fix | `test_ids_canonical.py`, `test_world_graph::test_alias_resolution_is_provider_scoped` | done |
| 24 | No instability/conservation hidden by rounding | Decimal 50 digits; Fraction weights | `test_l2_matches_definition_and_is_correctly_rounded`, population property test | done |
| 25 | No geography permanently excluded | DZA kept as gap; gaps API | `test_all_four_countries_returns_missing_input_for_dza`, `test_coverage_and_gaps` | partial: no exploration-quota mechanism (spec §15.3); V1 acceptable |
| 26 | No household/network coarsening without diagnostics | No households in V1; `extensions/population.py` refuses | `test_household_synthesis_refuses_and_validates_sources` | done (by refusal) |
| 27 | No calibration claim without denominator/holdout | Tournament reports counts; locked holdout | `test_locked_holdout_reuse_is_blocked` | done |
| 28 | No global knowledge % without finite catalogue | Coverage relative to `mvp.yaml` | `test_coverage_and_gaps` | done |
| 29 | No `os.cpu_count()` concurrency | `threads_budget`, start scripts | `test_threads_budget_ignores_cpu_count`, `test_thread_budget_is_explicit_and_capped_by_cgroup` | done |
| 30 | No egress/LLM/compute beyond budget | `budgets.yaml`; `AutonomyGuard`; `ops.budget_ledger` | `test_autonomy_gates.py::test_budgets_policy_is_used`, `::test_kill_switch_blocks_everything` | partial: the connector `run_connector` path (scheduled ingestion) does not consult `budgets.yaml` daily limits or write `ops.budget_ledger`. **Remedy:** reserve and consume budget in `run_connector`/`handle_ingest` (D/lead). |
| 31 | No secrets in code/fixtures/logs/artifacts | Redaction; scrubbed envs; scans | `test_secrets_logs.py`, `test_start_env.py` | partial: `RecordingHttp` leaks secret query params into fixture manifests (strict xfail); `Settings()` errors echo secrets (strict xfail) |
| 32 | No retention claim overriding required deletion | Deletion-ticket path; V1 public-only | `test_deletion_ticket_does_not_help_runtime_roles` | partial (no runbook) |
| 33 | No dynamic economic effect from accounting identity | Horizon=1, `NOT_CLAIMED` | `test_five_year_horizon_rejected` | done |
| 34 | No production success from stub/unavailable capability | `capabilities.py`; typed failures | `test_capabilities.py`, `test_missing_handler_is_a_typed_failure_never_success` | done |
| 35 | No claim tests prove causal truth/completeness | Docs (`docs/limitations.md`) | not testable | done (documentation) |

## 13. Architecture §24 — Required CI checks

| # | Check | Status | Evidence (`.github/workflows/ci.yml` unless noted) | Gap |
|---|---|---|---|---|
| 1 | Ruff format/lint; mypy strict kernel/contracts; TS typecheck; import boundaries | partial | Jobs: ruff check, ruff format, mypy, `verify_imports.py`, `npm run typecheck` | mypy covers only `reva.kernel`, not "contracts" (`ports/*`). Ruff currently 50 findings and mypy 1 error, so CI would be red. **Remedy:** add `reva.ports` to `[tool.mypy] packages` and fix findings (lead). |
| 2 | JSON Schema/OpenAPI compatibility, registry validation | done | `scripts/validate_contracts.py`; `tests/contracts/*` | OpenAPI *backward-compatibility* diff against the previous release is not performed (first release; acceptable). |
| 3 | Unit/property tests, numerical identities, dimensional analysis | done | pytest job | — |
| 4 | Migrations from empty and from previous supported version | partial | conftest migrates a fresh DB from empty | No "previous version → head" upgrade test. Migrations are edited in place (R-15). **Remedy:** freeze 0001–0007 at release and add an upgrade test from the release tag (lead). |
| 5 | Role/grant and cross-context tests with real logins | done | `test_roles_grants.py`, `test_db_hardening.py` | — |
| 6 | Captured connector replay, revisions, pagination, errors, drift | done | `test_http_connectors.py`, `test_revisions.py`, `test_fixture_ingest.py` | — |
| 7 | Temporal/spatial consistency, leakage | done | `tests/replay/*`, `test_extensions_physical.py` (antimeridian) | — |
| 8 | Scientific reference cases, conservation, population diagnostics | done | `tests/scientific/*`, `tests/unit/population/*` | — |
| 9 | Queue crash/retry/fencing; object-publication fault injection | done | `test_queue_reliability.py`, `test_storage_publication.py` | — |
| 10 | UI e2e provenance, scenario banner, missing data, auth/CSRF | partial | vitest unit tests in CI; `ui/e2e/smoke.spec.ts` + `scripts/e2e_stack.py` exist | Playwright e2e is **not** in `ci.yml`. **Remedy:** add a job running `npm --prefix ui run e2e` against `scripts/e2e_stack.py` (F/I). |
| 11 | Dependency/secret/licence scans; generated-code sandbox adversarial tests | partial | `test_secrets_logs.py`, `test_sandbox_isolation.py` run in pytest | No dependency vulnerability scan (pip-audit / `npm audit`) and no licence scan in CI. **Remedy:** add `uvx pip-audit`, `npm audit --omit=dev` and a licence allowlist step (I). |
| 12 | Named-hardware performance budget and memory tests | partial | `tests/performance/*` marked `perf` and `db` | Not a separate CI job with named hardware; results not recorded (I). |
| 13 | Docker build, non-root smoke, scrubbed startup env, Blueprint validation | done | `docker` job (build + non-root smoke); `validate_blueprint.py`; `test_start_env.py` | Build not run in this audit (no Docker build attempted). |

## 14. Architecture §25 — Phase gates P0–P7

| Phase | Gate | Status | Evidence / blocking items |
|---|---|---|---|
| P0 | Clean install; `/health/live`; unit tests and lint pass; imports without network/secrets; honest build status | partial | Health and unit tests are fine. Lint (ruff 50, mypy 1) not clean. `docs/implementation_status.md` missing. ADRs 0001–0012 exist (23:15). |
| P1 | Re-fetch idempotency; correction appends; earlier cutoff sees original; missing/zero; failed upload invisible; wrong unit rejected; roles deny invalid writes | partial | All mapped to tests (§11). The "earlier cutoff sees original" test fails at the snapshot (`test_two_revisions_yield_different_as_of_answers`, H1/lead). |
| P2 | Each source fetched live once; offline replay equivalent; pagination; 429/HTML/API-error; Comtrade missing weight; crash/retry one acquisition | partial | Live run 23:29 passes all 4 sources plus Eurostat (`var/live/live_checks.md`). "Offline fixture replay equivalent" relies on `test_r04_live_ingest_identical_to_fixture_creates_live_lineage`. Row-level drift crash is a strict xfail. |
| P3 | Every displayed value traces to raw source; graph roles; release-pinned world view; WDI/Eurostat incompatible; no scenario input | partial | All tested at API level. The live release lacks Eurostat Jan-1 cells (G/lead). `release_by_id` serves unpublished releases (strict xfail). |
| P4 | Hand-computed cases; p=1 → 0; ε=0 = L1; missing blocks; 5-year rejected; scenario writes cannot change beliefs; cancellation/checkpoint/retry | done (pending re-run) | `test_simulation_engine.py` (errored at 23:17 only because of bootstrap churn); scientific tests pass. |
| P5 | Future revisions/features/weights blocked; retrospective labelled; uncertainty types; UI drill-through; no-result/missing/auth flows; naive baseline shown | partial | Replay/leakage done. UI drill-through e2e not in CI. `test_operational_replay_job_*` failing (H1). |
| P6 | Discover demo_pjan; validate live sample; semantic mismatch rejected; 100k preserves leaf totals; coarsen/refine preserves mass; generated code cannot run in worker; hostile document cannot alter policy | partial | Population numerics and autonomy gates done. Live Eurostat sample passes. End-to-end Eurostat → release → population service is untested and absent from the live release. `test_worker_never_imports_proposed_code` was failing (G). |
| P7 | Schema/CLI validation; deploy only with access; roles/credentials verified; restore from backups; no filesystem persistence assumptions; p95/RSS recorded; complete live demo | partial / blocked-external | Blueprint validated offline. Deployment is blocked by missing Render/S3 credentials. Restore test exists. p95/RSS not recorded. No complete 12-step live demo (§15). New `REVA_DB_SSLMODE`/`REVA_PROCESS_ROLE` conflict with `render.yaml` (D-13). Machine-readable acceptance report (§25.1) missing. |

## 15. Architecture §17.3 — The 12-step demonstration workflow

| Step | Status | Evidence | Gap |
|---|---|---|---|
| 1 Fetch and archive real source responses | done | `reva ingest --live`; `scripts/live_source_checks.py` → `var/live/` | — |
| 2 Show source flags, units, classifications, release uncertainty | done | `/v1/observations`, EvidencePage | — |
| 3 Publish source-selected baseline beliefs | done | `reva beliefs publish`; worker `publish_beliefs` job | — |
| 4 Explore trade hyperedges (product, reporter, counterpart, interval) | done | `/v1/relations`, GraphPage, `world/graph.py` | — |
| 5 Apply +25% border-price scenario | done | `reva demo --scenario crude-border-price-25`; SimulationPage | — |
| 6 Deterministic exposure + labelled elasticity sensitivity range | done | L1/L2; `test_l2_sensitivity_range` | `reva demo` takes one fidelity per call, so the demonstration needs two runs. |
| 7 Drill to exact response fields and raw artifacts | done | `/v1/provenance`, ProvenanceDrawer | — |
| 8 Replay two-vintage sequence; later revisions excluded | partial | `POST /v1/replay`; vintage fixtures; `test_two_revisions_yield_different_as_of_answers` (failing) | No CLI or demo step loads `tests/fixtures/vintages` into a demonstrable replay. |
| 9 Discover and validate Eurostat connector; reject false equivalence | done | `POST /v1/discovery`; autonomy job | — |
| 10 Materialise/calibrate/coarsen/refine demographic recipe | partial | `population/service.py` job | Untested end to end; no live dataset in the 23:29 run. |
| 11 Naive vs state-space tournament on a labelled retrospective panel | done | `POST /v1/evaluations`; `evaluation/tournament_service.py`; ModelsPage | — |
| 12 Display unresolved gaps and a reviewable connector change proposal | done | `/v1/gaps`, `/v1/proposals`, AcquisitionPage | — |
| Separate negative demo: all four countries → Algerian blocker | done | `reva demo --countries FRA,DZA,CHN,USA` → `missing_input`; test | Holds only for reference year 2023 (D-4). |
| **One workflow orchestrating all 12 steps** | **missing** | `reva demo` covers steps 3, 5 and 6 only | **Remedy:** extend `cli/demo.py` (or add `reva demo --full`) to run ingest → publish → graph sample → L1+L2 → provenance sample → vintage replay → discovery → population → tournament → gaps/proposals, and emit a machine-readable report usable as the §25.1 acceptance report (lead). |

## 16. Architecture §23 — Commands

| Command | Status | Evidence / gap |
|---|---|---|
| `cp .env.example .env` | done | `.env.example` is identical to the appendix |
| `docker compose up -d db` | done | `compose.yaml` matches the appendix, with an image digest pin |
| `uv sync --frozen --all-extras` | done | `uv.lock`; `research` extra is empty |
| `uv run reva db bootstrap` | done | `cli/main.py db bootstrap` |
| `uv run reva db upgrade` | partial | An alias of bootstrap: it also provisions roles and needs all three role passwords (D-5). |
| `uv run reva seed --profile mvp` | done | `profile` is accepted but ignored (only `mvp` exists). |
| `uv run reva fixtures load --profile mvp` | done | Replays all built-in connectors (`origin=test_fixture`); Eurostat fixtures are not included. |
| `uv run reva verify --profile local` | done | Writes a probe object into `quarantine/` (a side effect; minor). |
| `uv run reva api` | done | Binds 127.0.0.1:8000, one worker |
| `uv run reva worker` | done | `operations/worker.main` (imports OK) |
| `npm --prefix ui ci`, `npm --prefix ui run dev` | done | `ui/package.json`, `package-lock.json` |
| `uv run reva ingest --profile mvp --live` | done | `--live/--fixtures`, `--capture` |
| `uv run reva demo --scenario crude-border-price-25` | partial | Exists and resolves a real baseline. Not the full 12-step workflow (§15). |
| `uv run pytest` | done | `-m 'not live'` by default |
| `render blueprints validate render.yaml` | blocked-external | Render CLI/credentials not available. `scripts/validate_blueprint.py` validates against the cached official schema (valid). |
| `uv run reva auth hash-password` (from `.env.example`) | done | Plus `auth create-token` and `revoke-token` |

## 17. Architecture §23.1 — Environment variables

`settings.py` declares every `REVA_*` variable in §23.1 with the spec defaults. `.env.example` and `render.yaml` are verbatim copies of the appendices, so they omit the optional variables (`REVA_S3_*` in `.env.example`, `REVA_COMTRADE_API_KEY`, `REVA_LLM_PROVIDER/MODEL/API_KEY`, `REVA_SANDBOX_*`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `PORT`). That is consistent with the spec.

| Variable(s) | settings.py | .env.example | render.yaml | Note |
|---|---|---|---|---|
| REVA_ENV, REVA_LOG_LEVEL | yes | yes | yes | `REVA_ENV` also accepts `test` (extension) |
| REVA_DB_HOST/PORT/NAME | yes | yes | fromDatabase | |
| REVA_MIGRATION_DATABASE_URL | yes (secret) | yes | API only | Worker refuses it in production (`start_worker.py`) |
| REVA_API/WORKER/SIM_DB_PASSWORD | yes | yes | generated on API; worker/sim via fromService | |
| REVA_DB_POOL_SIZE/MAX_OVERFLOW/STATEMENT_TIMEOUT_MS | yes | yes | yes | |
| REVA_OBJECT_BACKEND/ROOT | yes | yes | backend=s3 | Production forbids filesystem (`test_production_never_silently_selects_filesystem`) |
| REVA_S3_ENDPOINT_URL/REGION/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY/PREFIX | yes | prefix only | sync:false / fromService | Separate API vs worker keys are documented in the Blueprint comments |
| REVA_SESSION_SECRET, REVA_ADMIN_PASSWORD_HASH | yes | yes | generated / sync:false | Argon2id format enforced in production |
| REVA_PUBLIC_BASE_URL, REVA_ALLOWED_HOSTS, REVA_CORS_ORIGINS | yes | yes | from RENDER_EXTERNAL_* (CORS default empty) | |
| REVA_WORKER_CONCURRENCY, JOB_LEASE/HEARTBEAT/MAX_ATTEMPTS, SCHEDULER_TICK | yes | yes | yes | |
| REVA_CPU_BUDGET_THREADS, REVA_DUCKDB_MEMORY_LIMIT, REVA_CACHE_MAX_BYTES | yes | yes | yes | DuckDB limit is unused (no DuckDB use) |
| REVA_HTTP_TIMEOUT_SECONDS/MAX_BYTES/CONTACT_EMAIL | yes | yes | yes | |
| REVA_COMTRADE_API_KEY | yes | — | — | Key-enabled connector version not implemented (spec: "separate connector version"); no-key path works |
| REVA_LLM_ENABLED/MONTHLY_BUDGET_USD/PROVIDER/MODEL/API_KEY | yes | first two | first two | |
| REVA_AUTONOMY_MODE, REVA_AUTONOMY_KILL_SWITCH | yes | yes | yes | |
| REVA_SANDBOX_ENDPOINT/TOKEN | yes | — | — | |
| OTEL_EXPORTER_OTLP_ENDPOINT | **no** | — | — | Not read anywhere (see §10 traces) |
| PORT | yes | — | Render-provided | |
| OMP/OPENBLAS/MKL/POLARS threads | set by start scripts | yes | yes | |
| **Not in spec:** REVA_DB_SSLMODE, REVA_PROCESS_ROLE, REVA_FORWARDED_ALLOW_IPS, REVA_GIT_COMMIT, REVA_IMAGE_DIGEST, REVA_BACKUP_AGE_RECIPIENT | added | — | — | **REVA_DB_SSLMODE is now mandatory in production but absent from `render.yaml`** (D-13). Document all extensions in `docs/deployment.md`. |

## 18. Specification discrepancies and ambiguities, and how the code resolved them

| ID | Discrepancy / ambiguity | Resolution in code | Assessment |
|---|---|---|---|
| D-1 | The reference SQL and the §8.4 migration table group tables differently from FK dependency order: `ops.publication` is listed in 0001 but references `belief.release`; `belief.estimate.method_version_id` references `model.version`, listed in 0004; `meta.context` is not listed; `belief.input.claim_id` references a 0005 table. | `model.version` and `ops.publication` are created in 0003 (documented in its docstring). `meta.context` is in 0001. `claim_id` is added to `belief.input` in 0005 via `ALTER` plus a trigger. | Correct, since the spec says "use dependency ordering". Record it in `docs/implementation_status.md`. |
| D-2 | The Comtrade public preview accepts one `period` per request, while the spec wants "total series 2020–2024" and gives a single-period template. | One request per (reporter, flow, year): 40 totals + 8 bilateral = 48 requests in the live run. | Correct and bounded. Budget is 100/day for Comtrade (`budgets.yaml`), so a second same-day run would exhaust it if budgets were enforced on ingest (they are not; see invariant 30). |
| D-3 | "Bilateral subsets for 2023" vs "preview completeness only for bounded exact keys". | The bilateral request omits `partnerCode` (all partners), bounded by `maxRecords=500` with count, dimension and duplicate checks, and the item is `required=False`. | An all-partner query is arguably not an "exact key". Acceptable if documented: the count check rules out truncation. Alternatively enumerate a curated partner list. Owner: lead, documented in ADR/limitations. |
| D-4 | The spec and brief say DZA returned zero Comtrade rows (spec: 2023; brief: 2020–2024). The live run at 23:29 shows **DZA 2024 rows**: imports US$5,564 and exports US$14.18 bn. | The values are stored as reported, with flags. A 2023 all-four request still yields `missing_input`. | **Scientific risk:** a 2024 all-four scenario would now run with Algerian crude imports of US$5,564, an implausible provider value. The spec treats outliers as review signals, not deletions. **Remedy:** add a plausibility/outlier `QualityReport` warning surfaced in the UI and simulation applicability. Keep the negative demo pinned to 2023 and update the brief and `docs/limitations.md` (lead). |
| D-5 | §23 lists both `reva db bootstrap` and `reva db upgrade`. | `upgrade` is an alias of `bootstrap` and needs role passwords. | Ambiguous. Either make `upgrade` migrations-only (advisory lock, no role DDL) or document the alias (lead). |
| D-6 | §19 says every POST that *starts work* needs Idempotency-Key; `POST /v1/scenarios` creates a definition. | Scenarios are deduplicated by spec digest. | Reasonable. The digest is caller-supplied and trusted (strict xfail), so fix that. |
| D-7 | §9 says "Validate reference year" and the schema allows 2020–2024, while §17.1 says "V1 default requires the chosen year". | Strict same-year check per country; no auto common-year option. | Correct. |
| D-8 | The kernel `UncertaintyKind.probability` (needed for the §13 "probability" output type) is absent from the `observation.schema.json` enum. | Tracked as a strict xfail in `tests/contracts/test_json_schemas.py:110`. | Align the contract (add `probability`) or restrict the kernel. The contract is an appendix, so prefer an ADR plus a schema minor version (K/lead). |
| D-9 | The reference `model.promotion.decision` has no `keep_incumbent`; §12.2 requires "keep the incumbent and report insufficient evaluation". | `keep_incumbent` added to the CHECK. | Fine; documented extension. |
| D-10 | The reference `world.entity.referent_mode` is only real/abstract; kernel §3 mentions synthetic/hypothetical referents. | The CHECK is extended to four modes. | Fine. |
| D-11 | §16.3 says L0 is an "exact cache hit for the same full specification", but `fidelity` is part of the spec. | The cache key excludes `fidelity` only, so an L0 request reuses a prior L1/L2 result for the otherwise identical spec. | Sensible; documented in the `simulation/engine.py` docstring. Note it in the methodology doc (C). |
| D-12 | §12 says to use the *full* `render.yaml` appendix and recheck it against the current schema. The worker gets `REVA_S3_*` from the API via `fromService`, but its keys are `sync:false` (distinct credentials). | Verbatim copy; offline validation against a cached official schema dated 2026-10-14. | Fine. `render blueprints validate` is blocked-external. |
| D-13 | Settings hardening (23:26) adds production requirements not in §23.1 or the Blueprint: `REVA_DB_SSLMODE` ∈ {require, verify-ca, verify-full}, plus `REVA_PROCESS_ROLE` so non-API processes skip the session-secret rule. | Not yet propagated to `render.yaml`, the start scripts or `bootstrap_runtime.py`. Several settings/start-env tests fail at the snapshot. | **Deployment-breaking unless reconciled.** Prefer a minimal, documented Blueprint addition (`REVA_DB_SSLMODE: require` in `reva-common`, after verifying that Render internal connections accept SSL) and have each start script set `REVA_PROCESS_ROLE` (lead + I). |
| D-14 | §22 lists `adapters/postgres/models.py, grants.py`, `adapters/objects/manifests.py`, `evidence/{eligibility,quality,extraction}.py`, `belief/reconciliation.py`, `cli/{migrate,ingest,verify}.py`, `api/routes/{entities,graph,models,scenarios,replay,acquisition,ops}.py`, `operations/telemetry.py`, `autonomy/{repair,budgets}.py`, `population/...`. | Several are absent or folded into other modules (e.g. grants in migration 0007, eligibility in SQL, routes grouped into `world/evidence/beliefs/catalog/sim/system/provenance`, budgets in `autonomy/policy.py`). There is no `autonomy/repair.py` (the self-healing repair-proposal flow of §10.3). | The layout is "ownership boundaries", so consolidation is acceptable. Document the mapping (§1 item 1). **Self-healing repair proposals (§10.3) are not implemented** beyond health/drift detection; add a typed `blocked`/candidate repair proposal path or list it as a limitation (G). |
| D-15 | §5 asks to write objects "Store … sorted dimension order, row counts, source logical keys …" for *manifests*, but §8.1 allows PostgreSQL for small source panels. | Only belief releases and population artifacts have Parquet manifests; source observations stay in PostgreSQL. | Acceptable for V1 scale. State it in limitations. |
| D-16 | The brief's eligibility mapping carries `classifications: [H4,H5,H6]`, but the spec does not say whether the classification revision gates eligibility. | Not enforced. | Either enforce it (flag present in `source_flags`) or remove the key to avoid a false sense of scope (lead). |

## 19. Consolidated most important gaps

Ordered by risk to an honest P0–P7 claim. The final report repeats the top 25.

1. **D-13 settings vs Blueprint/start scripts** (`REVA_DB_SSLMODE`, `REVA_PROCESS_ROLE`). Deploy would fail. Owners: lead + I.
2. **No single 12-step demonstration workflow** and no machine-readable acceptance report (§15, §25.1). Owner: lead.
3. **`docs/implementation_status.md` missing** (first action 2; P0 honesty). Owner: lead.
4. **Suite not green**: `test_two_revisions_yield_different_as_of_answers` (P1 gate) and `test_operational_replay_job_*` fail; DB tests are flaky under `CONNECTION LIMIT 10` on shared cluster roles. Owners: H1/lead.
5. **Eurostat → live belief release → population service not proven end to end**. The live release lacks Jan-1 cells, and `population/service.py` has no DB test. Owners: G + lead.
6. **D-4 DZA 2024 implausible import value**: no plausibility/outlier signal. Owner: lead (connector quality), F (UI).
7. **`release_by_id` serves unpublished releases** (strict xfail). Owners: lead/E.
8. **`sim.create_scenario` trusts the caller's digest** (strict xfail). Owner: lead (migration).
9. **`RecordingHttp` writes secret query params into fixture manifests** (strict xfail; invariant 31). Owner: lead.
10. **`Settings()` validation errors echo secrets** (strict xfail; invariant 31). Owner: lead.
11. **Row-level schema drift crashes the connector run** instead of quarantining (strict xfail). Owner: lead.
12. **Observation export vs `observation.schema.json`** mismatch, plus the `probability` kind (strict xfails, D-8). Owners: K/lead.
13. **Invariant 13 (source-family independence)** has no implementation or test. Owners: G/lead.
14. **Invariant 30 (budgets)** not enforced on scheduled ingestion; `ops.budget_ledger` unused there. Owners: D/lead.
15. **Selector resolution** (check 8, invariant 1) is not verified beyond the artifact hash. Owner: lead.
16. **Published component schema edited in place** (`test_published_versions_match_pinned_digests` fails; invariant 16). Owner: K.
17. **CI gaps**: Playwright e2e not run; no pip-audit/npm audit/licence scan; no upgrade-from-previous-version migration test; mypy not on `ports`. Owners: I/F/lead.
18. **Lint/type state red** (ruff 50, mypy 1 at 22:59). Owners: all, lead to gate.
19. **No OpenTelemetry traces**; `OTEL_EXPORTER_OTLP_ENDPOINT` ignored. Owners: D/I.
20. **Performance measurements not recorded** (p95, RSS, queue ack); queue-ack perf test failing. Owner: I.
21. **BacktestRunner supports only retrospective mode**; strict and available-then are blocked. Owners: G/H1.
22. **No Eurostat weekly schedule; no conditional requests for Crossref/Eurostat**. Owners: G/lead.
23. **Self-healing repair proposals (§10.3)** not implemented (D-14). Owner: G.
24. **Migrations edited in place after application** (R-15); freeze before release. Owner: lead.
25. **`reva db upgrade` is bootstrap** (D-5); `fixtures load` omits Eurostat. Owner: lead.

## 20. Status counts (rows in this document)

These counts come from a script that reads the first status cell of every requirement row in §1–§17. They exclude the discrepancy table and the D-numbered items.

| Status | Count |
|---|---|
| done (includes rows marked "done, pending re-run", "done (verify)" and "done (recently fixed)") | 233 |
| partial (includes the two "partial / blocked-external" rows) | 45 |
| missing | 4: `docs/implementation_status.md`; invariant 13 enforcement and test; an orchestrated 12-step demo; a record of performance measurements |
| blocked-external | 3: `render blueprints validate`; the Render/S3 deployment itself; S3 IAM prefix policies (the last two are counted under partial above) |
| in progress (owner) | No row is still a placeholder. Every area had files appear after 23:00. Owner notes mark rows whose failures were seen while the owner was actively editing: H1 replay, G autonomy end to end, the lead's bootstrap and settings, K component digests. |

Treat the counts as an indication of the overall shape, not as a metric. A "done" row means the code and tests exist. It does not mean the test passed at the snapshot (see §0).
