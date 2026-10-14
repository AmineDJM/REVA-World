# Data constitution

The data constitution is the set of semantic and scientific rules every value in
REVA obeys. It is binding on code, registry entries, connectors, models, the API
and the UI. Tests enforce it where a test can; where no test can (invariant 35),
that is stated rather than hidden.

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

Status legend: **tested** — an existing test fails if the invariant is broken;
**partial** — tested for the V1 paths only; **planned** — the named owner path is
reserved for the test; **structural** — enforced by construction (grants,
import contracts, schema) and checked by the cited test; **not testable** —
a claim about the world that software tests cannot establish.

| # | Invariant | Enforcement | Coverage (test paths) | Status |
|---|---|---|---|---|
| 1 | No published numerical result without complete provenance or an explicit missing-provenance blocker | activities, `belief.input` FKs, provenance endpoint | `tests/integration/test_api_endpoints.py::test_provenance_reaches_raw_artifact_and_selector`; UI drill-through `ui/e2e/*` (agent F) | partial |
| 2 | No overwriting of observations, model versions, scenarios or releases | `meta.reject_mutation` triggers + grants | `tests/security/test_roles_grants.py::test_runtime_roles_cannot_update_delete_truncate_history`, `::test_owner_cannot_mutate_immutable_records_either`; survives restore: `tests/integration/test_backup_restore.py` | tested |
| 3 | No scenario/evaluation result accepted as real-world evidence | `reva_sim` grants, RLS, `belief.input` FK targets | `tests/security/test_roles_grants.py::test_sim_cannot_insert_evidence_or_beliefs`; `tests/integration/test_simulation_engine.py::test_sim_role_cannot_write_evidence_or_beliefs` | tested |
| 4 | No source report labelled as certain reality | modality `selected_report`, uncertainty status kept | `tests/unit/test_kernel_values.py`, `tests/scientific/test_uncertainty_types.py` | partial |
| 5 | No missing value converted to zero | kernel `Missing`, connectors, models | `tests/unit/test_kernel_values.py::test_missing_can_never_be_used_as_a_number`; `tests/security/test_http_connectors.py::test_comtrade_missing_flow_stays_missing_never_zero`; `tests/scientific/test_crude_exposure.py::test_missing_input_raises_never_zero`; `tests/unit/population/test_population_recipe.py::test_leaf_absent_from_source_is_missing`; `tests/integration/test_api_endpoints.py::test_dza_crude_import_belief_is_typed_missing` | tested |
| 6 | No unit/currency/basis change without a versioned transform | `reva.ontology.units` | `tests/unit/test_ontology_units.py` | tested |
| 7 | No aggregation across incompatible support, classification or universe | concept semantic profiles, model input checks | `tests/scientific/test_crude_exposure.py::test_mixed_reference_years_rejected`, `::test_mixed_basis_rejected`; WDI mid-year vs Eurostat 1 January: `tests/integration/test_eurostat*.py` (agent G) | partial |
| 8 | No observed correlation labelled causal without an admissible causal claim | modality enum; evidence-card kinds; no causal model in V1 | `tests/scientific/test_uncertainty_types.py`; card schema validation in `reva.evidence.cards` | partial |
| 9 | No loss of uncertainty/dependence metadata without an approximation record | uncertainty specs immutable, propagated by selection | `tests/scientific/test_uncertainty_types.py::test_specs_are_immutable` | partial |
| 10 | No synthetic/private-person identity linkage in V1 | privacy policy, recipe attribute allowlist, `artifact_v1_public_only` check | `tests/unit/population/test_population_recipe.py::test_forbidden_attributes_rejected` | tested |
| 11 | No model execution fetching live inputs | import-linter contract on `reva.models`/`reva.simulation` | `scripts/verify_imports.py` (CI); `tests/scientific/test_crude_exposure_coupling_rng.py` | structural |
| 12 | No replay using unavailable revisions or later fitted preprocessing | cutoff repositories, leakage checks | `tests/replay/test_leakage_cases.py`; `tests/integration/test_revisions.py`; `tests/unit/forecasting/test_forecast_no_lookahead.py` | tested |
| 13 | No source-family duplicates counted as independent evidence | lineage family on sources; promotion gate | `tests/integration/test_autonomy*.py` (agent G) | planned |
| 14 | No arbitrary code from documents/agents executed in production | template-only autonomy, sandbox interface blocked unless configured | `tests/unit/test_capabilities.py::test_sandbox_blocked_unless_configured`; `tests/security/test_sandbox*.py`, `tests/security/test_prompt_injection*.py` (agent G) | partial |
| 15 | No model self-promotion or editing its own gates | promotion policy digest pinned; separate evaluator | `tests/integration/test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent`, `::test_locked_holdout_reuse_is_blocked`; `tests/integration/test_autonomy*.py` (agent G) | partial |
| 16 | No kernel/ontology semantic mutation without version and migration | immutable `ontology.concept_version`; duplicate-version check | `tests/security/test_roles_grants.py`; `scripts/validate_contracts.py` (CI) | structural |
| 17 | No silent truncation of a connector response, query or graph | pagination/count checks, truncation flags | `tests/security/test_http_connectors.py::test_wb_pagination_truncation_blocks`, `::test_comtrade_max_records_truncation_blocks`; `tests/integration/test_api_endpoints.py::test_relations_bounded_with_truncation_flag` | tested |
| 18 | No partial multi-object publication visible as a complete release | objects-first, single metadata transaction | `tests/integration/test_storage_publication.py` | tested |
| 19 | No queue lease holder commits after losing its fencing token | `ops.assert_lease`/`ops.complete_job` fencing | `tests/integration/test_queue_reliability.py::test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit`; `tests/integration/test_simulation_engine.py::test_stale_fencing_token_cannot_publish`; `tests/replay/test_replay_job.py::test_stale_lease_cannot_publish_a_replay` | tested |
| 20 | No model applied outside its declared support without a qualified research-mode result | applicability envelopes | `tests/scientific/test_crude_exposure.py::test_envelopes_and_horizon`; `tests/integration/test_simulation_engine.py::test_five_year_horizon_rejected`, `::test_l3_and_l4_rejected` | tested |
| 21 | No probabilities fabricated for unquantified assumptions | sensitivity ranges carry no level | `tests/scientific/test_crude_exposure.py::test_sensitivity_range_not_probabilistic`; `tests/scientific/test_uncertainty_types.py::test_sensitivity_range_cannot_carry_a_level` | tested |
| 22 | No confidence/credible/prediction/sensitivity intervals conflated | distinct interval kinds | `tests/scientific/test_uncertainty_types.py`; `tests/unit/forecasting/test_forecast_metrics.py::test_coverage_aggregation_rejects_non_prediction_intervals` | tested |
| 23 | No source-specific country code assumed to be a universal country ID | provider-scoped aliases, label-independent entity ids | `tests/unit/test_ids_canonical.py::test_external_id_depends_on_provider_key_and_epoch`, `::test_registry_entity_ids_are_label_independent` | tested |
| 24 | No numerical instability or failed conservation hidden by rounding | Decimal arithmetic, conservation checks | `tests/scientific/test_crude_exposure.py::test_zero_elasticity_l2_equals_l1_exactly`, `::test_l2_matches_definition_and_is_correctly_rounded`; `tests/property/test_population_properties.py` | tested |
| 25 | No default country/geography permanently excluded from refinement | gaps keep DZA visible; exploration quotas | `tests/integration/test_simulation_engine.py::test_all_four_countries_returns_missing_input_for_dza`; `tests/integration/test_api_endpoints.py::test_coverage_and_gaps` | partial |
| 26 | No household/network coarsening without task-relevant diagnostics | V1 has no households/networks; categorical coarsening reports error | `tests/unit/population/test_population_compression.py`; household synthesis refuses: `tests/unit/test_extensions_interfaces.py::test_household_synthesis_refuses_and_validates_sources` | partial |
| 27 | No calibration performance claim without disclosed denominator/holdout | tournament reports origin-series counts, holdout lock | `tests/unit/forecasting/test_forecast_tournament.py`; `tests/integration/test_evaluation_service.py` | tested |
| 28 | No global knowledge percentage without a versioned finite catalogue | coverage relative to `registry/requirements/mvp.yaml` | `tests/integration/test_api_endpoints.py::test_coverage_and_gaps` | partial |
| 29 | No uncontrolled concurrency based on host `os.cpu_count()` | explicit `REVA_CPU_BUDGET_THREADS` capped by cgroup quota; thread variables set by start scripts | `tests/security/test_start_env.py::test_thread_budget_is_explicit_and_capped_by_cgroup`; `tests/unit/test_settings.py::test_threads_budget_ignores_cpu_count`; `tests/integration/test_queue_reliability.py::test_thread_budget_ignores_cpu_count` | tested |
| 30 | No external egress, LLM or scientific computation beyond the configured budget | budgets policy, ledger, LLM disabled by default | `registry/policies/budgets.yaml`; `tests/integration/test_autonomy*.py` (agent G) | planned |
| 31 | No secrets in code, fixtures, logs, artifacts or generated prompts | redaction, secret scans, scrubbed child environments | `tests/security/test_secrets_logs.py`; `tests/security/test_start_env.py`; `tests/integration/test_queue_telemetry.py::test_json_logs_carry_correlation_and_redact_secrets` | tested |
| 32 | No claim of legal retention overriding required deletion of restricted data | V1 stores no restricted data (`artifact_v1_public_only`); reviewed deletion ticket path in `meta.reject_mutation` | `tests/security/test_roles_grants.py::test_deletion_ticket_does_not_help_runtime_roles` | partial |
| 33 | No dynamic economic effect inferred solely from the MVP accounting identity | model scope/horizon envelope | `tests/integration/test_simulation_engine.py::test_five_year_horizon_rejected`; `tests/scientific/test_crude_exposure.py::test_envelopes_and_horizon` | tested |
| 34 | No production success status from a stub or unavailable capability | capability registry, typed gaps, missing handler is a failure | `tests/unit/test_capabilities.py`; `tests/unit/test_extensions_interfaces.py`; `tests/integration/test_queue_reliability.py::test_missing_handler_is_a_typed_failure_never_success` | tested |
| 35 | No claim that tests prove causal truth or completeness of reality | documentation and UI wording | none possible; see [limitations.md](limitations.md) | not testable |

The invariant list is also the review checklist for any change touching
`kernel/`, `ontology/`, migrations or a model. A change that weakens an
invariant needs an ADR, not a relaxed test.
