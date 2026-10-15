# Data constitution

The data constitution is the set of semantic and scientific rules every value in
REVA obeys. It is binding on code, registry entries, connectors, models, the API
and the UI. Tests enforce it where a test can; where a test cannot, or covers
only some paths (invariants 9, 13, 30, 32 and 35), that is stated rather than
hidden.

## Every variable declares (§5)

* stable identifier, labels, normative definition, owner and version;
* entity/population universe, support (time, space, population), denominator,
  aggregation and permitted operations;
* unit, scale type, currency/price basis, valuation and reference period;
* measurement method, inclusion rules and **missingness categories**;
* source methodology links, licence and privacy class;
* comparison/translation rules (mapping types `exact`, `narrower`, `broader`,
  `overlap`, `weighted_crosswalk`, `approximate_transform`, `incompatible`; only
  `exact` may serve as identity);
* extensive/intensive, stock/flow/rate/ratio/categorical semantics.

Concrete registry: `registry/concepts/*.yaml`, `registry/components/*.schema.json`,
`registry/mappings/*.yaml`, policies in `registry/policies/`.

## Values

* A value is a typed kernel value or a typed `Missing` with a reason; **missing is
  never zero** and an empty provider response is never treated as zero.
* Uncertainty status is one of `quantified`, `unquantified`, `exact_by_definition`;
  interval kinds (credible, confidence, prediction, bounds, sensitivity range) are
  distinct types and are never converted into each other.
* Time: half-open validity intervals; `source_published_at` is never guessed from
  the reference period; `recorded_at` is server time; revisions append.
* Context and modality travel with every API result (reported observation,
  selected estimate, forecast, identified causal estimate,
  assumption-conditioned scenario, unsupported).

## Non-negotiable invariants and coverage (§24.1)

**The authoritative coverage map is [`tests/invariants/coverage.yaml`](../tests/invariants/coverage.yaml).**
For each invariant it records the status, the enforcement points (file + symbol:
DB constraint/trigger/function, code, import contract, policy) and the full list
of pytest node ids that fail if the invariant is broken; for a partial status it
also records the gaps and the owner who closes them.
`tests/invariants/test_coverage_map.py` checks it on every test run: exactly 35
entries, every cited test exists, every enforcement file contains its symbol,
every `tests/invariants/test_invariant_*.py` test is cited, and this table lists
the same 35 invariants. The table below is a human-readable summary that names a
few representative tests per row; where it and `coverage.yaml` disagree,
`coverage.yaml` is right and this table is stale.

Current counts (from `coverage.yaml`): 30 proven, 5 partially proven
(#9, #13, #30, #32, #35), 0 unproven, 0 not applicable.

Status legend (same vocabulary as `coverage.yaml`):
**proven** — the cited tests fail if the invariant is broken on every V1 path;
some proofs are structural (they hold because V1 has no code path that could
violate the invariant, and a test checks that the path stays absent), which the
row says. **partial** — tested for some paths; the gaps are named in the row.
**unproven** / **not applicable** — not used by any invariant today. "Proven"
is a claim about the software, never about the world (invariant 35).

| # | Invariant | Enforcement | Representative tests (full list in `coverage.yaml`) | Status |
|---|---|---|---|---|
| 1 | No published numerical result without complete provenance or an explicit missing-provenance blocker | `ops.check_publication` + `publication_verified` trigger, `meta.artifact_is_verified`, `evidence.check_acquisition_artifact`, `publish_acquisition`, provenance API | `tests/security/test_roles_grants.py::test_publication_rejects_estimates_without_provenance`, `::test_acquisition_requires_verified_raw_bytes`; `tests/integration/test_api_endpoints.py::test_provenance_reaches_raw_artifact_and_selector` | proven (UI drill-through is covered separately by `ui/e2e`, not by this map) |
| 2 | No overwriting of observations, model versions, scenarios or releases | `meta.reject_mutation`, `immutable_record` and `release_sealed` triggers, `evidence.check_revision`, never-overwrite object stores, model registry | `tests/security/test_roles_grants.py::test_runtime_roles_cannot_update_delete_truncate_history`, `::test_owner_cannot_mutate_immutable_records_either`; `tests/unit/test_storage_fs.py::test_never_overwrites_different_content`; `tests/integration/test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of` | proven |
| 3 | No scenario/evaluation result accepted as real-world evidence | `belief.check_input`, `input_exactly_one`, `activity_sim_insert` RLS, import contract | `tests/security/test_roles_grants.py::test_sim_cannot_insert_evidence_or_beliefs`, `::test_belief_input_cannot_reference_simulation_or_evaluation_outputs`; `tests/integration/test_simulation_engine.py::test_sim_role_cannot_write_evidence_or_beliefs` | proven |
| 4 | No source report labelled as certain reality | `UncertaintySpec` (exact only with a definition), connectors emit `unknown` uncertainty, modality `selected_report` | `tests/invariants/test_invariant_04_source_reports.py` (3 tests); `tests/unit/test_kernel_values.py::test_exact_by_definition_requires_definition_kind` | proven |
| 5 | No missing value converted to zero | kernel `Missing`/`require_number`, DB CHECK on missing values, `MissingInput` in the crude model | `tests/unit/test_kernel_values.py::test_missing_can_never_be_used_as_a_number`; `tests/security/test_http_connectors.py::test_comtrade_missing_flow_stays_missing_never_zero`; `tests/scientific/test_crude_exposure.py::test_missing_input_raises_never_zero`; `tests/integration/test_api_endpoints.py::test_dza_crude_import_belief_is_typed_missing` | proven |
| 6 | No unit/currency/basis change without a versioned transform | `reva.ontology.units` (`apply_currency_transform`, `mass_to_volume`, `UNIT_REGISTRY_VERSION`) | `tests/unit/test_ontology_units.py` (6 tests, e.g. `::test_kg_to_barrel_refused_without_density_and_convention`); `tests/scientific/test_crude_exposure.py::test_mixed_basis_rejected` | proven |
| 7 | No aggregation across incompatible support, classification or universe | `can_aggregate`, `can_combine_countries`, `aggregate_flow_items`, selection flags, `input_admissible` trigger | `tests/unit/test_ontology_validation.py` (5 tests); `tests/unit/test_ontology_mappings.py::test_eurostat_jan1_total_vs_wdi_midyear_is_incompatible`; `tests/scientific/test_crude_exposure.py::test_mixed_reference_years_rejected` | proven |
| 8 | No observed correlation labelled causal without an admissible causal claim | no code path emits `identified_causal` (AST scan); DB CHECK limits belief modalities; causal claims are tier T3 (human review); hypothesis cards carry no coefficients | `tests/invariants/test_invariant_08_no_causal_labels.py` (4 tests); `tests/unit/test_world_graph_core.py::test_sequence_relations_never_claim_causation` | proven — structural: V1 has no causal model, so the proof is that no path can emit a causal label |
| 9 | No loss of uncertainty/dependence metadata without an approximation record | selection copies uncertainty verbatim; DB equality at publication; export refuses what the contract cannot express; compression certificates | `tests/invariants/test_invariant_09_13_selection.py::test_selection_copies_uncertainty_and_dependence_metadata_verbatim`; `tests/contracts/test_observation_export_properties.py::test_uncertainty_the_contract_cannot_express_is_refused` | partial — no generic approximation-record type; `dependence_group` propagation through forecast and simulation outputs is not tested end to end (V1 inputs are unquantified) |
| 10 | No synthetic/private-person identity linkage in V1 | recipe attribute allowlist, `registry/policies/privacy.yaml`, `artifact_v1_public_only` | `tests/unit/population/test_population_recipe.py::test_forbidden_attributes_rejected`, `::test_any_other_attribute_rejected`; `tests/security/test_roles_grants.py::test_restricted_privacy_classes_rejected` | proven |
| 11 | No model execution fetching live inputs | import-linter contracts (`pyproject.toml`), `scripts/verify_imports.py`, `JobContext` has no HTTP client, simulation loads only a pinned release | `tests/invariants/test_invariant_11_no_live_inputs.py` (5 tests: transitive import closure via grimp, contracts kept, no HTTP client in the handler contract, L1/L2 jobs succeed with every network path disabled, population job import closure) | proven |
| 12 | No replay using unavailable revisions or later fitted preprocessing | `LeakageGuard`, `CutoffRepository`, release-manifest check, server-side `recorded_at`, `eligible_latest` | `tests/replay/test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode`; `tests/integration/test_revisions.py::test_two_revisions_yield_different_as_of_answers`; `tests/unit/forecasting/test_forecast_no_lookahead.py::test_cutoff_scoped_series_blocks_future_access` | proven |
| 13 | No source-family duplicates counted as independent evidence | selection picks one lineage by preference (no pooling/vote counting); same-family duplicates stay visible as flagged alternatives; `ops.check_gate_evaluator` | `tests/invariants/test_invariant_09_13_selection.py::test_same_family_duplicates_do_not_narrow_or_upgrade_the_selected_value`, `::test_selection_output_has_no_independence_count_or_pooled_value`; `tests/integration/test_autonomy_gates.py::test_t1_without_independent_reports_is_not_promoted` | partial — `lineage_family`/`dependency_family` are recorded but no code consumes them because V1 never pools; a family-aware independence rule must be added and tested before any multi-source combination or evidence weighting |
| 14 | No arbitrary code from documents/agents executed in production | template-only autonomy (`TrustedTemplateSandbox`), sandbox blocked unless configured, `contains_code` policy | `tests/security/test_sandbox_isolation.py::test_generated_code_is_blocked_without_external_runner`, `::test_worker_never_imports_proposed_code`; `tests/security/test_prompt_injection.py::test_injected_catalogue_and_metadata_change_nothing`; `tests/unit/test_capabilities.py::test_sandbox_blocked_unless_configured` | proven |
| 15 | No model self-promotion or editing its own gates | `PolicyPromotionGate` with pinned policy digest, `gate_independent` trigger, `model.check_promotion` | `tests/integration/test_autonomy_gates.py::test_proposer_cannot_write_its_own_gate_result`, `::test_policy_change_makes_gate_refuse`; `tests/security/test_db_hardening.py::test_model_promotion_requires_matching_replay_evaluation_and_named_reviewer`; `tests/integration/test_evaluation_service.py::test_locked_holdout_reuse_is_blocked` | proven |
| 16 | No kernel/ontology semantic mutation without version and migration | component `check_version_bump`/`verify_published` with pinned digests, `immutable_record` trigger, `CANONICAL_HASH_VERSION`, `scripts/validate_contracts.py` | `tests/unit/test_ontology_components.py::test_published_versions_are_immutable`, `::test_verify_published_detects_in_place_edit`; `tests/contracts/test_registry_components.py::test_published_versions_match_pinned_digests` | proven |
| 17 | No silent truncation of a connector response, query or graph | pagination/count checks, `possible_truncation`, bounded decoder, graph `MAX_EDGES`, `truncated` flags | `tests/security/test_http_connectors.py::test_wb_pagination_truncation_blocks`, `::test_comtrade_max_records_truncation_blocks`; `tests/integration/test_api_endpoints.py::test_relations_bounded_with_truncation_flag` | proven |
| 18 | No partial multi-object publication visible as a complete release | objects first, single metadata transaction, publication lock, GC respects locks | `tests/integration/test_storage_publication.py` (4 tests); `tests/security/test_db_hardening.py::test_publication_requires_a_transaction_scoped_lock`; `tests/integration/test_queue_reliability.py::test_kill_after_object_upload_then_retry_yields_one_acquisition` | proven |
| 19 | No queue lease holder commits after losing its fencing token | `ops.assert_lease`/`ops.complete_job` fencing, `complete_in` | `tests/integration/test_queue_reliability.py::test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit`; `tests/integration/test_simulation_engine.py::test_stale_fencing_token_cannot_publish`; `tests/replay/test_replay_job.py::test_stale_lease_cannot_publish_a_replay` | proven |
| 20 | No model applied outside its declared support without a qualified research-mode result | applicability envelopes (`check_applicability`), scenario spec validation, elasticity envelope | `tests/unit/test_model_registry.py::test_out_of_envelope_requests_are_refused_with_reasons`; `tests/scientific/test_crude_exposure.py::test_envelopes_and_horizon`; `tests/integration/test_simulation_engine.py::test_five_year_horizon_rejected`, `::test_l3_and_l4_rejected` | proven |
| 21 | No probabilities fabricated for unquantified assumptions | sensitivity ranges carry no level; unknown uncertainty kept unquantified; VOI without a decision spec is a unitless proxy | `tests/scientific/test_crude_exposure.py::test_sensitivity_range_not_probabilistic`; `tests/scientific/test_uncertainty_types.py::test_sensitivity_range_cannot_carry_a_level`; `tests/scientific/test_voi_analytic.py::test_estimate_value_without_decision_spec_is_unitless_proxy` | proven |
| 22 | No confidence/credible/prediction/sensitivity intervals conflated | distinct `UncertaintyKind`s, prediction-interval-only coverage, export contract | `tests/scientific/test_uncertainty_types.py::test_kinds_are_distinct_values_and_not_interchangeable`; `tests/unit/forecasting/test_forecast_metrics.py::test_coverage_aggregation_rejects_non_prediction_intervals` | proven |
| 23 | No source-specific country code assumed to be a universal country ID | provider-scoped `external_id`, label-independent entity ids, provider-scoped aliases | `tests/unit/test_ids_canonical.py::test_external_id_depends_on_provider_key_and_epoch`, `::test_registry_entity_ids_are_label_independent`; `tests/integration/test_world_graph.py::test_alias_resolution_is_provider_scoped` | proven |
| 24 | No numerical instability or failed conservation hidden by rounding | Decimal arithmetic (`PRECISION`), conservation checks, raking diagnostics, finite-value checks | `tests/scientific/test_crude_exposure.py::test_zero_elasticity_l2_equals_l1_exactly`, `::test_l2_matches_definition_and_is_correctly_rounded`; `tests/property/test_population_properties.py::test_exact_conservation_for_random_counts` | proven |
| 25 | No default country/geography permanently excluded from refinement | `registry/requirements/mvp.yaml` geography, gap classification keeps missing countries as gaps | `tests/invariants/test_invariant_25_no_permanent_exclusion.py` (3 tests); `tests/integration/test_simulation_engine.py::test_all_four_countries_returns_missing_input_for_dza`; `tests/integration/test_api_endpoints.py::test_coverage_and_gaps` | proven |
| 26 | No household/network coarsening without task-relevant diagnostics | the only V1 coarsening (age band x recorded sex) always carries a task-scoped compression certificate; household synthesis is a typed reserved capability; network projections are flagged derived | `tests/invariants/test_invariant_26_coarsening_diagnostics.py` (4 tests); `tests/unit/test_world_graph_core.py::test_clique_projection_is_flagged_derived` | proven — for households structural: V1 has no household synthesis |
| 27 | No calibration performance claim without disclosed denominator/holdout | tournament reports origin-series counts, holdout lock, honesty statement | `tests/unit/forecasting/test_forecast_tournament.py::test_short_panel_keeps_incumbent_insufficient_evaluation`; `tests/integration/test_evaluation_service.py::test_full_panel_stores_evaluations_promotions_and_label`, `::test_locked_holdout_reuse_is_blocked` | proven |
| 28 | No global knowledge percentage without a versioned finite catalogue | coverage computed only relative to `registry/requirements/mvp.yaml` (`catalogue_version`) | `tests/integration/test_api_endpoints.py::test_coverage_and_gaps`; `tests/integration/test_autonomy_contracts.py::test_handle_gaps_writes_report_and_outbox`; `tests/invariants/test_invariant_35_no_truth_claims.py::test_evaluation_and_coverage_statements_disclaim_reality` | proven |
| 29 | No uncontrolled concurrency based on host `os.cpu_count()` | explicit `REVA_CPU_BUDGET_THREADS` capped by cgroup quota, bounded worker concurrency, start scripts set thread variables | `tests/invariants/test_invariant_29_no_cpu_count.py` (3 tests incl. a source scan with a planted-violation control); `tests/unit/test_settings.py::test_threads_budget_ignores_cpu_count`; `tests/security/test_start_env.py::test_thread_budget_is_explicit_and_capped_by_cgroup` | proven |
| 30 | No external egress, LLM or scientific computation beyond the configured budget | per-run request budget in the connector runner, budgeted autonomy discovery, LLM disabled with a zero budget, bounded external-runner budgets | `tests/invariants/test_invariant_30_budgets.py` (3 tests); `tests/integration/test_autonomy_gates.py::test_budgets_policy_is_used`, `::test_kill_switch_blocks_everything` | partial — the daily totals in `registry/policies/budgets.yaml` (`daily.http_requests_total`, `daily.http_bytes_total`, `per_source_daily_requests`) have no persistent ledger and are not enforced across scheduled live ingest runs; `per_source_daily_requests` keys do not match connector names; no test proves a daily cap |
| 31 | No secrets in code, fixtures, logs, artifacts or generated prompts | `RedactionFilter`, `redact_url`, `redact_env`, scrubbed child environments (`build_child_env`, sandbox) | `tests/security/test_secrets_logs.py` (6 tests); `tests/security/test_start_env.py::test_worker_child_env_has_no_api_or_owner_secrets`; `tests/integration/test_queue_telemetry.py::test_json_logs_carry_correlation_and_redact_secrets` | proven |
| 32 | No claim of legal retention overriding required deletion of restricted data | V1 cannot store restricted data (`artifact_v1_public_only`); reviewed deletion path (`reva_restricted_deletion` + ticket) through the append-only trigger | `tests/invariants/test_invariant_32_deletion_overrides_retention.py` (2 tests); `tests/security/test_roles_grants.py::test_deletion_ticket_does_not_help_runtime_roles` | partial — no end-to-end restricted-data deletion workflow (object-store bytes, backups, derived artifacts) exists because V1 stores none; it must be built and tested before any restricted class is admitted |
| 33 | No dynamic economic effect inferred solely from the MVP accounting identity | scenario spec refuses multi-period dynamics, macroeconomic outputs and general equilibrium; model horizon check and `NOT_CLAIMED` | `tests/invariants/test_invariant_33_no_dynamic_effects.py` (5 tests); `tests/integration/test_simulation_engine.py::test_five_year_horizon_rejected` | proven |
| 34 | No production success status from a stub or unavailable capability | `resolve_handler`/`HandlerUnavailable`, capability registry `require`, typed gaps | `tests/invariants/test_invariant_34_no_stub_success.py` (4 tests); `tests/integration/test_queue_reliability.py::test_missing_handler_is_a_typed_failure_never_success`; `tests/unit/test_capabilities.py::test_require_raises_structured_gap` | proven |
| 35 | No claim that tests prove causal truth or completeness of reality | [limitations.md](limitations.md) disclaimer, `HONESTY_STATEMENT`, coverage never as a percentage of reality | `tests/invariants/test_invariant_35_no_truth_claims.py` (4 tests incl. a lexical overclaim scan of README, docs, `ui/src` and runtime messages) | partial — a claim about the world cannot be proven by software tests; the scan is lexical and cannot judge meaning, and covers UI source, not rendered pages |

The invariant list is also the review checklist for any change touching
`kernel/`, `ontology/`, migrations or a model. A change that weakens an
invariant needs an ADR, not a relaxed test.
