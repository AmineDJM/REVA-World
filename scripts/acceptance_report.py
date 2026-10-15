#!/usr/bin/env python3
"""Machine-readable acceptance report (implementation prompt §13/§14, architecture §17.4, §25, §25.1).

Builds ``var/acceptance/acceptance_report.json`` and ``docs/dev/acceptance_report.md``:

* every P0–P7 phase gate of architecture §25 split into its individual acceptance
  criteria, and every mandatory acceptance test of implementation prompt §13 and
  MVP gate of §17.4, each mapped to concrete evidence: pytest node ids, UI
  (vitest) cases, command checks, the §17.3 demonstration workflow
  (``scripts/demo_workflow.py``), and recorded artefacts
  (``var/live/live_checks.json``, ``var/perf/perf_results.json``);
* pytest is run per group with ``--junitxml`` (live and perf tests are excluded by
  default; ``--with-perf`` / ``--with-live`` add them); results are read back
  from the JUnit files, never assumed;
* status vocabulary: ``passed`` / ``failed`` / ``blocked`` / ``missing``. A
  criterion without any automated evidence is ``missing`` (never ``passed``); a
  criterion whose evidence could not run here (deployment credentials, live
  network, a browser) is ``blocked`` with the reason. Aggregation per criterion:
  failed > blocked > missing > passed; checks that were merely excluded from this
  run (``not_run``) do not count unless they are the only evidence.

Usage::

    uv run python scripts/acceptance_report.py                  # all default groups + fixture demo
    uv run python scripts/acceptance_report.py --with-perf --with-live
    uv run python scripts/acceptance_report.py --reuse          # rebuild from existing JUnit/demo files
    uv run python scripts/acceptance_report.py --groups unit,security --demo reuse

Exit status 0 when nothing failed (blocked/missing allowed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "var" / "acceptance"
JUNIT_DIR = OUT_DIR / "junit"
LOG_DIR = OUT_DIR / "logs"
REPORT_JSON = OUT_DIR / "acceptance_report.json"
REPORT_MD = ROOT / "docs" / "dev" / "acceptance_report.md"
LIVE_CHECKS = ROOT / "var" / "live" / "live_checks.json"
PERF_RESULTS = ROOT / "var" / "perf" / "perf_results.json"
SCHEMA = "reva-acceptance-report/1"

STATUS_ORDER = {"failed": 4, "blocked": 3, "missing": 2, "passed": 1}

# --------------------------------------------------------------------------- groups
PYTEST_GROUPS: dict[str, dict[str, Any]] = {
    "unit": {"paths": ["tests/unit"], "marker": "not live and not perf"},
    "contracts": {"paths": ["tests/contracts"], "marker": "not live and not perf"},
    "property": {"paths": ["tests/property"], "marker": "not live and not perf"},
    "scientific": {"paths": ["tests/scientific"], "marker": "not live and not perf"},
    "replay": {"paths": ["tests/replay"], "marker": "not live and not perf"},
    "security": {"paths": ["tests/security"], "marker": "not live and not perf"},
    "integration": {"paths": ["tests/integration"], "marker": "not live and not perf"},
    "e2e": {"paths": ["tests/e2e"], "marker": "not live and not perf"},
    "perf": {"paths": ["tests/performance"], "marker": "perf", "flag": "with_perf"},
    "live": {"paths": ["tests"], "marker": "live", "flag": "with_live"},
}
DEFAULT_GROUPS = [g for g, d in PYTEST_GROUPS.items() if "flag" not in d]


# --------------------------------------------------------------------------- evidence constructors
def T(*nodes: str) -> dict[str, Any]:
    """pytest node ids (``path::test_name``; parametrised cases are aggregated)."""
    return {"kind": "pytest", "nodes": list(nodes)}


def UI(file: str, name: str) -> dict[str, Any]:
    """A vitest case in ui/ (file relative to ui/, name substring)."""
    return {"kind": "vitest", "file": file, "name": name}


def PW(name: str) -> dict[str, Any]:
    """Playwright smoke case (needs a browser + running deployment): supplementary, never counted when not run."""
    return {"kind": "playwright", "name": name, "optional": True}


def DEMO(step: str, mode: str = "fixtures") -> dict[str, Any]:
    return {"kind": "demo", "mode": mode, "step": step}


def DEMO_EV(step: str, key: str, predicate: str, desc: str) -> dict[str, Any]:
    """A predicate over one evidence value recorded by a demo step (fixtures mode)."""
    return {"kind": "demo_evidence", "step": step, "key": key, "predicate": predicate, "desc": desc}


def CMD(name: str) -> dict[str, Any]:
    return {"kind": "command", "name": name}


def LIVE(key: str) -> dict[str, Any]:
    return {"kind": "live_checks", "key": key}


def PERF(key: str) -> dict[str, Any]:
    return {"kind": "perf", "key": key}


def DOC(path: str, what: str) -> dict[str, Any]:
    return {"kind": "doc", "path": path, "what": what}


def MANUAL(reason: str, status: str = "missing") -> dict[str, Any]:
    return {"kind": "manual", "status": status, "reason": reason}


TU, TC, TSC, TSE, TI, TR, TP = (
    "tests/unit/",
    "tests/contracts/",
    "tests/scientific/",
    "tests/security/",
    "tests/integration/",
    "tests/replay/",
    "tests/property/",
)
POPU = "tests/unit/population/"
CE = TSC + "test_crude_exposure.py::"
SIM = TI + "test_simulation_engine.py::"
REV = TI + "test_revisions.py::"
QR = TI + "test_queue_reliability.py::"
API = TI + "test_api_endpoints.py::"
RG = TSE + "test_roles_grants.py::"
HC = TSE + "test_http_connectors.py::"
SSRF = TSE + "test_ssrf_http.py::"
EUR = TI + "test_eurostat_discovery.py::"
WG = TI + "test_world_graph.py::"
OV = TU + "test_ontology_validation.py::"
NO_DEPLOY = (
    "no deployment was performed from this environment: Render API access, the managed PostgreSQL "
    "instance and the private S3 bucket credentials are not available here (see docs/deployment.md)"
)

# --------------------------------------------------------------------------- P0–P7 (architecture §25)
PHASES: list[dict[str, Any]] = [
    {
        "phase": "P0",
        "title": "repository and constitution",
        "gates": [
            ("Clean local install (lockfile consistent)", [CMD("uv_lock_check")]),
            ("/health/live works", [T(TSE + "test_auth_session.py::test_health_is_public")]),
            ("Unit tests pass", [{"kind": "group", "group": "unit"}]),
            ("Lint passes (ruff + import-linter boundaries)", [CMD("ruff"), CMD("import_linter")]),
            (
                "Package imports without network/secrets",
                [CMD("import_without_secrets"), T(TU + "test_settings.py::test_local_defaults_are_not_production")],
            ),
            (
                "Published build status is honest",
                [
                    DOC("docs/implementation_status.md", "implementation status"),
                    MANUAL("honesty of the published status is a human review item; no automated check"),
                ],
            ),
        ],
    },
    {
        "phase": "P1",
        "title": "kernel, ontology, provenance, storage, safe HTTP",
        "gates": [
            (
                "Re-fetch idempotency",
                [
                    T(
                        REV + "test_aba_creates_three_revisions_identical_polls_create_none",
                        TI + "test_fixture_ingest.py::test_worldbank_fixture_replay_is_fixture_origin",
                    )
                ],
            ),
            (
                "Correction appends (supersedes chain)",
                [
                    T(
                        REV + "test_supersedes_chain_constraints_are_enforced_by_the_database",
                        REV + "test_two_revisions_yield_different_as_of_answers",
                    )
                ],
            ),
            (
                "Earlier cutoff sees original",
                [
                    T(
                        REV + "test_two_revisions_yield_different_as_of_answers",
                        API + "test_timeseries_revision_filtering",
                        TI + "test_evidence_fixes.py::test_r20_release_as_of_never_returns_release_with_later_cutoff",
                    ),
                    DEMO("8"),
                ],
            ),
            (
                "Missing/zero distinction",
                [
                    T(
                        TU + "test_kernel_values.py::test_missing_can_never_be_used_as_a_number",
                        HC + "test_comtrade_missing_flow_stays_missing_never_zero",
                        HC + "test_wb_null_values_stay_missing",
                        RG + "test_missing_value_cannot_carry_a_number",
                    )
                ],
            ),
            (
                "Failed upload invisible",
                [
                    T(
                        TI + "test_storage_publication.py::test_failure_mid_upload_leaves_no_object_and_no_rows",
                        TI + "test_storage_publication.py::"
                        "test_crash_after_upload_before_commit_is_invisible_then_retry_publishes_once",
                        TI + "test_storage_s3.py::test_upload_failure_leaves_no_object",
                    )
                ],
            ),
            (
                "Wrong unit rejected",
                [
                    T(
                        TU + "test_ontology_units.py::test_observation_unit_must_match_concept_and_missing_values_pass",
                        TU + "test_ontology_units.py::test_observation_unit_missing_or_unknown_rejected",
                    )
                ],
            ),
            (
                "SQL roles deny invalid writes",
                [
                    T(
                        RG + "test_api_cannot_insert_directly",
                        RG + "test_runtime_roles_cannot_update_delete_truncate_history",
                        RG + "test_sim_cannot_insert_evidence_or_beliefs",
                        TSE + "test_db_hardening.py::test_worker_insert_grants_are_the_reviewed_set",
                    )
                ],
            ),
        ],
    },
    {
        "phase": "P2",
        "title": "real source adapters, fixtures, durable queue",
        "gates": [
            (
                "Fetch each source live once",
                [
                    LIVE("worldbank.wdi.indicators"),
                    LIVE("un.comtrade.public_exact"),
                    LIVE("naturalearth.admin0_110m"),
                    LIVE("crossref.works.doi"),
                    T("tests/live/test_live_sources.py::test_full_live_run_release_and_coverage"),
                ],
            ),
            (
                "Offline fixture replay equivalent",
                [
                    T(
                        TI + "test_fixture_ingest.py::test_worldbank_fixture_replay_is_fixture_origin",
                        TI + "test_evidence_fixes.py::test_r04_live_ingest_identical_to_fixture_creates_live_lineage",
                    ),
                    DEMO("1"),
                ],
            ),
            (
                "Pagination complete",
                [
                    T(
                        HC + "test_wb_multi_page_header_schedules_all_pages",
                        HC + "test_wb_pagination_truncation_blocks",
                        HC + "test_comtrade_max_records_truncation_blocks",
                    )
                ],
            ),
            (
                "429 / HTML / API-error handled",
                [
                    T(
                        SSRF + "test_429_retry_after_is_honoured_then_succeeds",
                        SSRF + "test_html_error_page_with_200_is_a_failure",
                        HC + "test_wb_api_error_payload_is_a_failure",
                        HC + "test_comtrade_error_and_drift_payloads_fail",
                    )
                ],
            ),
            ("Comtrade missing weight remains missing", [T(HC + "test_comtrade_unusable_weight_stays_missing")]),
            (
                "Crash/retry produces one visible acquisition",
                [
                    T(
                        QR + "test_kill_after_object_upload_then_retry_yields_one_acquisition",
                        QR + "test_kill_after_db_commit_before_ack_yields_one_acquisition",
                    )
                ],
            ),
        ],
    },
    {
        "phase": "P3",
        "title": "beliefs, aliases, n-ary graph, cards",
        "gates": [
            (
                "Every displayed value traces to raw source",
                [T(API + "test_provenance_reaches_raw_artifact_and_selector"), DEMO("7")],
            ),
            (
                "Graph preserves participant roles",
                [
                    T(
                        WG + "test_participant_roles_and_directions_preserved",
                        TU + "test_world_graph_core.py::test_incidence_preserves_roles_and_repeated_participants",
                    ),
                    DEMO("4"),
                ],
            ),
            (
                "World view is release-pinned",
                [
                    T(WG + "test_release_pinned_context", API + "test_beliefs_are_release_pinned_and_as_of"),
                    DEMO_EV(
                        "4",
                        "release_pinned_edges",
                        "gt0",
                        "a real published release pins its trade hyperedges (GraphContext.release returns edges)",
                    ),
                ],
            ),
            (
                "WDI/Eurostat incompatible support cannot reconcile",
                [
                    T(
                        OV + "test_jan1_support_does_not_satisfy_midyear_concept",
                        TU + "test_ontology_mappings.py::test_eurostat_jan1_total_vs_wdi_midyear_is_incompatible",
                        EUR + "test_end_to_end_promotion_ingestion_and_belief_separation",
                    ),
                    DEMO("9"),
                ],
            ),
            (
                "No scenario input allowed into beliefs",
                [T(RG + "test_belief_input_cannot_reference_simulation_or_evaluation_outputs"), DEMO("5")],
            ),
        ],
    },
    {
        "phase": "P4",
        "title": "scenario contract, crude exposure L1/L2, simulation queue",
        "gates": [
            (
                "Hand-computed cases match",
                [T(CE + "test_hand_computed_l1_exact", SIM + "test_l1_exposure_matches_hand_computation"), DEMO("5")],
            ),
            ("p=1 gives zero", [T(CE + "test_unit_multipliers_give_zero")]),
            (
                "epsilon=0 equals L1",
                [T(CE + "test_zero_elasticity_l2_equals_l1_exactly", SIM + "test_l2_sensitivity_range"), DEMO("6")],
            ),
            (
                "Missing input blocks",
                [
                    T(
                        SIM + "test_all_four_countries_returns_missing_input_for_dza",
                        CE + "test_missing_input_raises_never_zero",
                    ),
                    DEMO("N"),
                ],
            ),
            ("5-year GDP request rejected", [T(SIM + "test_five_year_horizon_rejected")]),
            (
                "Scenario writes cannot change beliefs",
                [
                    T(
                        SIM + "test_sim_role_cannot_write_evidence_or_beliefs",
                        RG + "test_sim_cannot_insert_evidence_or_beliefs",
                    )
                ],
            ),
            (
                "Cancellation/checkpoint/retry verified",
                [
                    T(
                        SIM + "test_cancellation_before_publication",
                        QR + "test_cancel_running_job_is_honoured_cooperatively",
                        QR + "test_retry_with_exponential_backoff_then_dead_letter",
                        TI + "test_worker_loop.py::test_sigterm_stops_claiming_signals_handler_and_drains",
                    )
                ],
            ),
        ],
    },
    {
        "phase": "P5",
        "title": "replay, forecasting, scorecards, UI",
        "gates": [
            (
                "Future revisions/features/weights blocked",
                [
                    T(
                        TR + "test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode",
                        TU
                        + "forecasting/test_forecast_no_lookahead.py::test_cutoff_scoped_series_blocks_future_access",
                        RG + "test_belief_input_rejects_observation_recorded_after_cutoff",
                    )
                ],
            ),
            (
                "Revised-data mode visibly labelled",
                [
                    T(
                        TR + "test_leakage_cases.py::test_revised_data_retrospective_is_labelled_everywhere",
                        TR + "test_replay_job.py::test_retrospective_job_result_carries_label_and_disclaimer",
                        API + "test_evaluation_job_and_scorecard_labels",
                    ),
                    DEMO("11"),
                ],
            ),
            (
                "Uncertainty types correct",
                [
                    T(
                        TSC + "test_uncertainty_types.py::test_kinds_are_distinct_values_and_not_interchangeable",
                        TSC + "test_uncertainty_types.py::test_sensitivity_range_cannot_carry_a_level",
                        CE + "test_sensitivity_range_not_probabilistic",
                    ),
                    UI("src/components/UncertaintyPanel.test.tsx", "sensitivity range"),
                ],
            ),
            (
                "UI drill-through works",
                [
                    T(API + "test_provenance_reaches_raw_artifact_and_selector"),
                    DEMO("7"),
                    MANUAL(
                        "no UI-level test opens the provenance drawer and follows it to the raw "
                        "artifact (ui/src has no provenance drawer test; the Playwright smoke does "
                        "not exercise drill-through)"
                    ),
                ],
            ),
            (
                "No-result/missing/auth flows tested",
                [
                    UI("src/components/MissingValue.test.tsx", "never as zero"),
                    UI("src/api/client.test.ts", "notifies listeners on 401"),
                    UI("src/api/client.test.ts", "network failure as unreachable"),
                    T(TSE + "test_auth_session.py::test_unauthenticated_is_401"),
                ],
            ),
            (
                "Naive baseline always shown",
                [
                    T(
                        TU + "forecasting/test_forecast_tournament.py::test_naive_baseline_added_when_not_a_candidate",
                        TI + "test_evaluation_service.py::test_full_panel_stores_evaluations_promotions_and_label",
                    ),
                    DEMO("11"),
                ],
            ),
        ],
    },
    {
        "phase": "P6",
        "title": "autonomy, Eurostat, demographic recipe, gaps",
        "gates": [
            (
                "Discover demo_pjan from official catalogue",
                [T(EUR + "test_discovers_demo_pjan_from_captured_catalogue"), DEMO("9")],
            ),
            (
                "Validate live sample",
                [
                    LIVE("eurostat_demo_pjan"),
                    T(
                        EUR + "test_live_catalogue_and_sample_still_conform",
                        "tests/live/test_live_sources.py::test_eurostat_demo_pjan_fr_2023",
                    ),
                ],
            ),
            (
                "Semantic mismatch rejected",
                [
                    T(
                        EUR + "test_wdi_midyear_equivalence_is_incompatible",
                        EUR + "test_validator_fails_mutated_sample",
                    ),
                    DEMO("9"),
                ],
            ),
            (
                "100k weighted records preserve leaf totals",
                [
                    T(
                        POPU + "test_population_materialise.py::test_leaf_totals_conserved_exactly",
                        "tests/performance/test_population_perf.py::test_materialise_100k_records_france_like",
                    ),
                    PERF("population_100k_wall_s"),
                    PERF("population_100k_peak_rss_mib"),
                    DEMO("10"),
                ],
            ),
            (
                "Coarsen/refine preserves mass",
                [
                    T(
                        POPU + "test_population_compression.py::test_coarsen_conserves_exactly",
                        POPU + "test_population_compression.py::test_coarsen_then_refine_round_trip",
                    ),
                    DEMO("10"),
                ],
            ),
            (
                "Generated code cannot run in worker",
                [
                    T(
                        TSE + "test_sandbox_isolation.py::test_worker_never_imports_proposed_code",
                        TSE + "test_sandbox_isolation.py::test_generated_code_is_blocked_without_external_runner",
                    )
                ],
            ),
            (
                "Hostile document cannot alter policy",
                [
                    T(
                        TSE + "test_prompt_injection.py::test_injected_paper_text_yields_only_a_candidate",
                        TSE + "test_prompt_injection.py::test_injected_catalogue_and_metadata_change_nothing",
                    )
                ],
            ),
        ],
    },
    {
        "phase": "P7",
        "title": "image, Render config, monitoring, backup, release",
        "gates": [
            (
                "Current schema/CLI validation",
                [CMD("validate_contracts"), CMD("validate_blueprint"), {"kind": "group", "group": "contracts"}],
            ),
            ("Deploy only when access exists", [MANUAL(NO_DEPLOY, status="blocked")]),
            (
                "Actual roles and credentials verified",
                [
                    T(
                        RG + "test_runtime_roles_are_unprivileged_noinherit_logins",
                        TSE + "test_db_hardening.py::test_provisioned_verifier_lets_the_runtime_role_log_in",
                        TSE + "test_start_env.py::test_worker_child_env_has_no_api_or_owner_secrets",
                    ),
                    MANUAL(
                        "production role/credential verification needs the deployed database: " + NO_DEPLOY,
                        status="blocked",
                    ),
                ],
            ),
            (
                "Restore from backups",
                [T(TI + "test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of")],
            ),
            (
                "No filesystem persistence assumptions",
                [
                    T(
                        TU + "test_settings.py::test_production_never_silently_selects_filesystem",
                        TI + "test_storage_s3.py::test_factory_builds_s3_store_for_production_settings",
                    )
                ],
            ),
            (
                "p95/RSS measurements recorded",
                [
                    PERF("ordinary_api_p95_ms"),
                    PERF("cached_metadata_p95_ms"),
                    PERF("l1_four_country_ms"),
                    PERF("queue_ack_p95_ms"),
                    PERF("population_100k_peak_rss_mib"),
                    DOC("docs/dev/perf_report.md", "performance report"),
                ],
            ),
            ("Complete live demo", [DEMO("*", mode="live")]),
        ],
    },
]

# --------------------------------------------------------------------------- §13 mandatory acceptance tests
ACCEPTANCE: list[tuple[str, str, list[dict[str, Any]]]] = [
    (
        "Scientific/semantic",
        "M=100, X=40, G=1000, multipliers 1.25 -> dM=25, dX=10, dnet=15, 1.5% exposure",
        [T(CE + "test_hand_computed_l1_exact")],
    ),
    ("Scientific/semantic", "All multipliers 1 give zero", [T(CE + "test_unit_multipliers_give_zero")]),
    (
        "Scientific/semantic",
        "Zero elasticities exactly recover the fixed-quantity calculation",
        [T(CE + "test_zero_elasticity_l2_equals_l1_exactly", SIM + "test_l2_sensitivity_range")],
    ),
    (
        "Scientific/semantic",
        "Quantity/valuation missingness blocks unsupported conversions",
        [
            T(
                TU + "test_ontology_units.py::test_kg_to_barrel_refused_without_density_and_convention",
                OV + "test_cif_and_fob_values_cannot_be_aggregated",
                OV + "test_currency_amount_without_basis_blocks_aggregation",
                CE + "test_missing_input_raises_never_zero",
                HC + "test_comtrade_unusable_weight_stays_missing",
            )
        ],
    ),
    (
        "Scientific/semantic",
        "Countries with different reference years or GDP bases cannot be silently combined",
        [
            T(
                OV + "test_countries_with_different_reference_years_cannot_be_combined",
                OV + "test_countries_with_different_gdp_bases_cannot_be_combined",
                CE + "test_mixed_reference_years_rejected",
                CE + "test_mixed_basis_rejected",
            )
        ],
    ),
    (
        "Scientific/semantic",
        "January and mid-year population are not identical concepts",
        [
            T(
                OV + "test_january_and_midyear_population_are_not_identical_concepts",
                TU + "test_ontology_mappings.py::test_eurostat_jan1_total_vs_wdi_midyear_is_incompatible",
                EUR + "test_wdi_midyear_equivalence_is_incompatible",
            ),
            DEMO("9"),
        ],
    ),
    (
        "Scientific/semantic",
        "Probability, credible/prediction intervals, bounds and sensitivity types remain distinct",
        [
            T(
                TSC + "test_uncertainty_types.py::test_kinds_are_distinct_values_and_not_interchangeable",
                TSC + "test_uncertainty_types.py::test_interval_kinds_require_a_level",
                TSC + "test_uncertainty_types.py::test_bounds_are_non_probabilistic_and_levelless",
                TSC + "test_uncertainty_types.py::test_probability_kind_is_probabilistic",
                TSC + "test_uncertainty_types.py::test_sensitivity_range_cannot_carry_a_level",
            ),
            DEMO("6"),
        ],
    ),
    (
        "Temporal/storage",
        "Two revisions yield different as-of answers at appropriate cutoffs",
        [
            T(
                REV + "test_two_revisions_yield_different_as_of_answers",
                API + "test_beliefs_are_release_pinned_and_as_of",
            ),
            DEMO("8"),
        ],
    ),
    (
        "Temporal/storage",
        "A->B->A creates three revisions; identical consecutive polls do not",
        [
            T(
                REV + "test_aba_creates_three_revisions_identical_polls_create_none",
                REV + "test_aba_with_byte_identical_return_creates_third_revision",
            )
        ],
    ),
    (
        "Temporal/storage",
        "Replayed old raw data with new parser versions is distinguished from an original vintage",
        [
            T(
                REV + "test_replayed_raw_with_new_parser_is_distinguished_from_original_vintage",
                TI + "test_evidence_fixes.py::test_r28_parser_version_change_reprocesses_same_bytes",
            )
        ],
    ),
    (
        "Temporal/storage",
        "Staged/uncommitted objects are invisible",
        [
            T(
                TI + "test_storage_publication.py::"
                "test_crash_after_upload_before_commit_is_invisible_then_retry_publishes_once",
                TI + "test_storage_publication.py::test_failure_mid_upload_leaves_no_object_and_no_rows",
                TU + "test_storage_fs.py::test_mid_stream_failure_leaves_no_object",
            )
        ],
    ),
    (
        "Temporal/storage",
        "Missing source publication time is never filled with the statistical reference date",
        [T(REV + "test_missing_source_publication_time_is_never_the_reference_date")],
    ),
    (
        "Temporal/storage",
        "Scientific UPDATE/DELETE fails under runtime roles",
        [
            T(
                RG + "test_runtime_roles_cannot_update_delete_truncate_history",
                RG + "test_owner_cannot_mutate_immutable_records_either",
            )
        ],
    ),
    (
        "Isolation/security",
        "Simulation role cannot insert evidence or beliefs",
        [T(RG + "test_sim_cannot_insert_evidence_or_beliefs", SIM + "test_sim_role_cannot_write_evidence_or_beliefs")],
    ),
    (
        "Isolation/security",
        "Generated code cannot get production credentials or network by default",
        [
            T(
                TSE + "test_sandbox_isolation.py::test_generated_code_is_blocked_without_external_runner",
                TSE
                + "test_sandbox_isolation.py::test_trusted_template_env_has_no_secrets_and_network_is_template_only",
                TSE + "test_sandbox_isolation.py::test_worker_never_imports_proposed_code",
            )
        ],
    ),
    (
        "Isolation/security",
        "Prompt-injection text in a paper cannot change a policy/allowlist",
        [
            T(
                TSE + "test_prompt_injection.py::test_injected_paper_text_yields_only_a_candidate",
                TSE + "test_prompt_injection.py::test_injected_catalogue_and_metadata_change_nothing",
            )
        ],
    ),
    (
        "Isolation/security",
        "SSRF",
        [
            T(
                SSRF + "test_hosts_resolving_to_non_public_addresses_rejected",
                SSRF + "test_literal_ip_hosts_rejected_even_if_allowlisted",
                SSRF + "test_disallowed_urls_never_reach_the_network",
            )
        ],
    ),
    ("Isolation/security", "Redirect to private IP", [T(SSRF + "test_redirect_hop_is_checked")]),
    (
        "Isolation/security",
        "Zip bomb",
        [
            T(
                SSRF + "test_zip_bomb_ratio_rejected",
                SSRF + "test_zip_total_expansion_limit",
                SSRF + "test_gzip_bomb_beyond_size_limit_rejected",
                SSRF + "test_gzip_bomb_within_size_limit_rejected_by_ratio",
            )
        ],
    ),
    (
        "Isolation/security",
        "SQL injection",
        [
            T(
                TSE + "test_sql_injection.py::test_entity_listing_filters_treat_input_as_data",
                TSE + "test_sql_injection.py::test_repository_lookups_treat_input_as_data",
                TSE + "test_sql_injection.py::test_hostile_strings_written_through_narrow_paths_round_trip_verbatim",
            )
        ],
    ),
    (
        "Isolation/security",
        "Secret-log",
        [
            T(
                TSE + "test_secrets_logs.py::test_http_client_logs_do_not_contain_secret_query_values",
                TSE + "test_secrets_logs.py::test_http_errors_do_not_echo_secret_query_values",
                TI + "test_queue_telemetry.py::test_json_logs_carry_correlation_and_redact_secrets",
            )
        ],
    ),
    (
        "Isolation/security",
        "App startup environment contains no migration owner password/URL",
        [
            T(
                TSE + "test_start_env.py::test_api_child_env_keeps_only_api_credentials",
                TSE + "test_start_env.py::test_worker_child_env_has_no_api_or_owner_secrets",
                TSE + "test_start_env.py::test_start_api_subprocess_child_environment",
                TSE + "test_start_env.py::test_start_worker_subprocess_child_environment",
            )
        ],
    ),
    (
        "Reliability",
        "Kill worker after object upload and after DB commit; retry yields one visible result",
        [
            T(
                QR + "test_kill_after_object_upload_then_retry_yields_one_acquisition",
                QR + "test_kill_after_db_commit_before_ack_yields_one_acquisition",
            )
        ],
    ),
    (
        "Reliability",
        "Expired lease/fencing holder cannot publish",
        [
            T(
                QR + "test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit",
                SIM + "test_stale_fencing_token_cannot_publish",
                TR + "test_replay_job.py::test_stale_lease_cannot_publish_a_replay",
            )
        ],
    ),
    (
        "Reliability",
        "Two workers claiming a due job do not duplicate visible effects",
        [
            T(
                QR + "test_concurrent_claims_never_return_the_same_job",
                QR + "test_two_schedulers_create_exactly_one_job_per_slot",
            )
        ],
    ),
    (
        "Reliability",
        "Rate limiting, HTML error page with HTTP 200 and schema drift lead to retries/quarantine",
        [
            T(
                SSRF + "test_429_retry_after_is_honoured_then_succeeds",
                SSRF + "test_html_error_page_with_200_is_a_failure",
                HC + "test_runner_quarantines_truncated_page_and_html",
                HC + "test_runner_quarantines_row_level_schema_drift",
                HC + "test_wb_schema_drift_is_a_failure",
            )
        ],
    ),
    (
        "Reliability",
        "Restore reconstructs identical logical manifests and as-of results",
        [T(TI + "test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of")],
    ),
    (
        "Population/compression",
        "All leaf-cell totals conserved; no double-counted source totals",
        [
            T(
                POPU + "test_population_materialise.py::test_leaf_totals_conserved_exactly",
                POPU + "test_population_recipe.py::test_only_disjoint_leaves_are_counted",
                TP + "test_population_properties.py::test_exact_conservation_for_random_counts",
            ),
            DEMO("10"),
        ],
    ),
    (
        "Population/compression",
        "Structural-zero and unknown-category handling",
        [
            T(
                POPU + "test_population_recipe.py::test_observed_zero_is_not_structural_zero_and_not_missing",
                POPU + "test_population_partition.py::test_observed_zero_cells_are_not_structural_zeros",
                POPU + "test_population_recipe.py::test_unknown_and_top_coded_retained_explicitly",
                POPU + "test_population_materialise.py::test_every_nonzero_cell_represented_and_zero_cell_empty",
            )
        ],
    ),
    (
        "Population/compression",
        "Same seed/config produces same logical cohort",
        [
            T(
                POPU + "test_population_materialise.py::test_same_inputs_identical_outputs",
                POPU + "test_population_recipe.py::test_digest_is_deterministic_and_seed_sensitive",
            )
        ],
    ),
    (
        "Population/compression",
        "Coarsen/refine preserves total weights and reports unsupported new detail",
        [
            T(
                POPU + "test_population_compression.py::test_coarsen_conserves_exactly",
                POPU + "test_population_compression.py::test_coarsen_then_refine_round_trip",
                POPU + "test_population_compression.py::test_refine_along_unsupported_dimension_raises",
            ),
            DEMO("10"),
        ],
    ),
    (
        "Evaluation/UI",
        "Six future-information leakage cases blocked",
        [T(TR + "test_leakage_cases.py::test_six_leakage_cases_blocked_in_strict_mode")],
    ),
    (
        "Evaluation/UI",
        "Naive baseline present; insufficient evidence does not promote challenger",
        [
            T(
                TU + "forecasting/test_forecast_tournament.py::test_naive_baseline_added_when_not_a_candidate",
                TU
                + "forecasting/test_forecast_tournament.py::test_short_panel_keeps_incumbent_insufficient_evaluation",
                TI + "test_evaluation_service.py::test_insufficient_evidence_keeps_incumbent",
            ),
            DEMO("11"),
        ],
    ),
    (
        "Evaluation/UI",
        "Provenance drawer reaches source bytes/selectors",
        [
            T(API + "test_provenance_reaches_raw_artifact_and_selector"),
            DEMO("7"),
            MANUAL("the UI provenance drawer itself has no automated test (API + demo evidence only)"),
        ],
    ),
    (
        "Evaluation/UI",
        "Every simulation screen and export has scenario context",
        [
            UI("src/pages/SimulationPage.test.tsx", "persistent SIMULATION banner"),
            UI("src/components/ContextBanner.test.tsx", "SIMULATION banner for a simulation context"),
            UI("src/lib/scenario.test.ts", "full scenario context in exports"),
            T(API + "test_scenarios_validate"),
            PW("every screen shows context and knowledge cutoff"),
            PW("simulation screen carries a persistent SIMULATION banner"),
        ],
    ),
    (
        "Evaluation/UI",
        "Negative/degraded states render clearly; no stub success",
        [
            UI("src/components/MissingValue.test.tsx", "never as zero"),
            UI("src/pages/SimulationPage.test.tsx", "missing_input rejection"),
            UI("src/api/client.test.ts", "network failure as unreachable"),
            T(
                QR + "test_missing_handler_is_a_typed_failure_never_success",
                TU + "test_capabilities.py::test_not_available_typed_result",
            ),
            DEMO("N"),
        ],
    ),
    (
        "Performance",
        "Measure on the named target resources",
        [
            PERF("ordinary_api_p95_ms@0.5cpu"),
            PERF("population_100k_wall_s"),
            MANUAL(
                "measurements ran on this host with emulated CPU quotas, not on the named Render instance types: "
                + NO_DEPLOY,
                status="blocked",
            ),
        ],
    ),
    (
        "Performance",
        "100k-record memory/time budget and API/L1 targets validated",
        [
            PERF("population_100k_wall_s"),
            PERF("population_100k_peak_rss_mib"),
            PERF("ordinary_api_p95_ms"),
            PERF("cached_metadata_p95_ms"),
            PERF("l1_four_country_ms"),
            PERF("queue_ack_p95_ms"),
            T(
                "tests/performance/test_population_perf.py::test_materialise_100k_records_france_like",
                "tests/performance/test_perf_simulation.py::test_l1_four_country_calculation_under_2s",
                "tests/performance/test_perf_api.py::test_api_latency_one_cpu",
                "tests/performance/test_perf_queue.py::test_queue_acknowledgement_under_1s",
            ),
        ],
    ),
    (
        "Performance",
        "Actual values reported; no thresholds weakened without an ADR",
        [DOC("docs/dev/perf_report.md", "performance report"), PERF("l1_four_country_ms")],
    ),
]

# --------------------------------------------------------------------------- §17.4 MVP gates
MVP_GATES: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "Real live acquisitions for the live acceptance run; deterministic CI uses captured fixtures",
        [LIVE("live_release_published"), LIVE("no_fixture_origin_in_db"), DEMO("1"), DEMO("1", mode="live")],
    ),
    (
        "Missing source/credential/rate limit is a visible blocked/degraded gate, never fake success",
        [
            T(
                QR + "test_missing_handler_is_a_typed_failure_never_success",
                TU + "test_capabilities.py::test_not_available_typed_result",
                SSRF + "test_retry_after_beyond_cap_raises_without_sleeping",
            )
        ],
    ),
    (
        "All four country profiles exist even if one scenario input is unavailable",
        [T(API + "test_entities_list_and_detail", API + "test_dza_crude_import_belief_is_typed_missing"), DEMO("N")],
    ),
    (
        "No scenario number can be retrieved through a canonical observation endpoint",
        [T(RG + "test_belief_input_cannot_reference_simulation_or_evaluation_outputs"), DEMO("5")],
    ),
    (
        "No synthetic record is linked to a real individual",
        [
            T(
                POPU + "test_population_recipe.py::test_forbidden_attributes_rejected",
                TC + "test_registry_components.py::test_component_privacy_is_v1_admissible_and_no_gender_field",
            ),
            DEMO("10"),
        ],
    ),
    (
        "MVP operational only after complete live demonstration and restore test pass",
        [
            DEMO("*", mode="live"),
            T(TI + "test_backup_restore.py::test_backup_restore_reconstructs_manifests_and_as_of"),
        ],
    ),
]


# --------------------------------------------------------------------------- command checks
def _import_without_secrets() -> list[str]:
    code = (
        "import importlib, pkgutil, reva; mods=[m.name for m in pkgutil.walk_packages(reva.__path__, 'reva.') "
        "if 'sandbox_child' not in m.name]; [importlib.import_module(m) for m in mods]; print(len(mods), 'modules')"
    )
    return [sys.executable, "-c", code]


COMMANDS: dict[str, dict[str, Any]] = {
    "uv_lock_check": {"argv": lambda: [shutil.which("uv") or "uv", "lock", "--check"], "timeout": 300},
    "ruff": {"argv": lambda: [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"], "timeout": 300},
    "import_linter": {"argv": lambda: [sys.executable, "scripts/verify_imports.py"], "timeout": 300},
    "import_without_secrets": {"argv": _import_without_secrets, "timeout": 300, "scrub_env": True},
    "validate_contracts": {"argv": lambda: [sys.executable, "scripts/validate_contracts.py"], "timeout": 300},
    "validate_blueprint": {"argv": lambda: [sys.executable, "scripts/validate_blueprint.py"], "timeout": 300},
}


def run_command(name: str) -> dict[str, Any]:
    spec = COMMANDS[name]
    argv = spec["argv"]()
    env = dict(os.environ)
    cwd = ROOT
    if spec.get("scrub_env"):
        # no .env, no REVA_* secrets, no database URLs, run outside the checkout so .env cannot be read
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR") or k.startswith("UV_")
        }
        env["HOME"] = tempfile.mkdtemp(prefix="reva_import_check_")
        env["PYTHONPATH"] = str(ROOT / "src")
        cwd = Path(env["HOME"])
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=spec["timeout"])
        rc, out = p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        rc, out = 124, f"timeout after {spec['timeout']} s"
    except FileNotFoundError as e:
        rc, out = 127, str(e)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"cmd_{name}.log").write_text(out)
    tail = [ln for ln in out.strip().splitlines() if "UV_NATIVE_TLS" not in ln][-6:]
    return {
        "name": name,
        "argv": [Path(a).name if a.startswith("/") else a for a in argv[:4]],
        "exit_code": rc,
        "duration_s": round(time.monotonic() - t0, 2),
        "output_tail": tail,
        "log": str((LOG_DIR / f"cmd_{name}.log").relative_to(ROOT)),
    }


# --------------------------------------------------------------------------- pytest groups
def run_group(group: str) -> dict[str, Any]:
    spec = PYTEST_GROUPS[group]
    JUNIT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    junit = JUNIT_DIR / f"{group}.xml"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *spec["paths"],
        "-m",
        spec["marker"],
        "-o",
        "junit_family=xunit1",
        f"--junitxml={junit}",
        "-q",
        "-rfEs",
        "-p",
        "no:cacheprovider",
    ]
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=spec.get("timeout", 5400))
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = 124, f"timeout\n{e.stdout or ''}"
    (LOG_DIR / f"pytest_{group}.log").write_text(out if isinstance(out, str) else str(out))
    summary = [ln for ln in out.splitlines() if re.search(r"\d+ (passed|failed|error|skipped|deselected)", ln)][-1:]
    return {
        "group": group,
        "command": "python -m pytest " + " ".join(spec["paths"]) + f" -m '{spec['marker']}'",
        "exit_code": rc,
        "duration_s": round(time.monotonic() - t0, 1),
        "junit": str(junit.relative_to(ROOT)),
        "log": str((LOG_DIR / f"pytest_{group}.log").relative_to(ROOT)),
        "summary": summary[0] if summary else None,
    }


def parse_pytest_junit(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    """file::function -> list of case results (parametrised cases kept separately)."""
    cases: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            root = ET.parse(path).getroot()  # noqa: S314 - JUnit XML written by this script's own runs
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            file = tc.get("file") or ""
            name = tc.get("name") or ""
            func = name.split("[", 1)[0]
            if not file:
                cls = tc.get("classname") or ""
                file = cls.replace(".", "/") + ".py"
            status, msg = "passed", None
            for child in tc:
                if child.tag in ("failure", "error"):
                    status, msg = "failed", (child.get("message") or "")[:400]
                elif child.tag == "skipped":
                    status, msg = "skipped", (child.get("message") or "")[:400]
            cases.setdefault(f"{file}::{func}", []).append(
                {"case": name, "status": status, "message": msg, "time": tc.get("time"), "junit": path.name}
            )
    return cases


def parse_vitest_junit(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - JUnit XML written by this script's own runs
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        status, msg = "passed", None
        for child in tc:
            if child.tag in ("failure", "error"):
                status, msg = "failed", (child.get("message") or "")[:400]
            elif child.tag == "skipped":
                status, msg = "skipped", None
        out.append({"file": tc.get("classname") or "", "name": tc.get("name") or "", "status": status, "message": msg})
    return out


def run_vitest() -> dict[str, Any]:
    ui = ROOT / "ui"
    junit = JUNIT_DIR / "ui_vitest.xml"
    vitest = ui / "node_modules" / ".bin" / "vitest"
    if not vitest.exists():
        return {"group": "ui", "exit_code": None, "skipped_reason": "ui/node_modules not installed (npm ci in ui/)"}
    JUNIT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            [str(vitest), "run", "--reporter=junit", f"--outputFile={junit}"],
            cwd=ui,
            capture_output=True,
            text=True,
            timeout=900,
        )
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        rc, out = 124, "timeout"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "ui_vitest.log").write_text(out)
    return {
        "group": "ui",
        "command": "ui/node_modules/.bin/vitest run --reporter=junit",
        "exit_code": rc,
        "duration_s": round(time.monotonic() - t0, 1),
        "junit": str(junit.relative_to(ROOT)),
        "log": str((LOG_DIR / "ui_vitest.log").relative_to(ROOT)),
    }


# --------------------------------------------------------------------------- evaluation of evidence
class Evaluator:
    def __init__(
        self,
        *,
        cases: dict[str, list[dict[str, Any]]],
        groups_run: dict[str, dict[str, Any]],
        vitest: list[dict[str, Any]],
        vitest_ran: bool,
        commands: dict[str, dict[str, Any]],
        demos: dict[str, dict[str, Any] | None],
        live: dict[str, Any] | None,
        perf: dict[str, Any] | None,
        excluded_markers: list[str],
    ) -> None:
        self.cases, self.groups_run, self.vitest, self.vitest_ran = cases, groups_run, vitest, vitest_ran
        self.commands, self.demos, self.live, self.perf = commands, demos, live, perf
        self.excluded_markers = excluded_markers
        self._defs: dict[str, bool] = {}

    # -- helpers
    def _defined(self, node: str) -> bool:
        if node not in self._defs:
            file, _, func = node.partition("::")
            p = ROOT / file
            self._defs[node] = (
                p.exists() and re.search(rf"^\s*(async\s+)?def {re.escape(func)}\b", p.read_text(), re.M) is not None
            )
        return self._defs[node]

    def _group_of(self, node: str) -> str | None:
        file = node.split("::")[0]
        for g, spec in PYTEST_GROUPS.items():
            if g in ("live",):
                continue
            if any(file.startswith(pth.rstrip("/") + "/") for pth in spec["paths"]):
                return g
        return None

    def _node_marked(self, node: str, marker: str) -> bool:
        file, _, func = node.partition("::")
        src = (ROOT / file).read_text()
        if re.search(rf"pytestmark\s*=.*mark\.{marker}\b", src):
            return True
        m = re.search(rf"((?:^\s*@[^\n]*\n)*)^\s*(async\s+)?def {re.escape(func)}\b", src, re.M)
        return bool(m and f"mark.{marker}" in m.group(1))

    def pytest_node(self, node: str) -> dict[str, Any]:
        if not self._defined(node):
            return {"node": node, "status": "missing", "reason": "test not found in the repository"}
        res = self.cases.get(node)
        if res:
            if any(r["status"] == "failed" for r in res):
                return {
                    "node": node,
                    "status": "failed",
                    "cases": len(res),
                    "message": next(r["message"] for r in res if r["status"] == "failed"),
                }
            if all(r["status"] == "skipped" for r in res):
                return {"node": node, "status": "blocked", "reason": f"skipped: {res[0]['message']}"}
            return {"node": node, "status": "passed", "cases": len(res)}
        for marker in ("live", "perf"):
            if self._node_marked(node, marker):
                if marker in self.excluded_markers:
                    return {
                        "node": node,
                        "status": "not_run",
                        "reason": f"{marker} tests excluded from this run (use --with-{marker})",
                    }
        g = self._group_of(node)
        if g and g not in self.groups_run:
            return {"node": node, "status": "not_run", "reason": f"pytest group {g!r} not run in this report"}
        return {
            "node": node,
            "status": "failed",
            "reason": "expected in the JUnit results but absent (collection error or crash?)",
        }

    def check(self, ev: dict[str, Any]) -> dict[str, Any]:
        kind = ev["kind"]
        if kind == "pytest":
            nodes = [self.pytest_node(n) for n in ev["nodes"]]
            sts = [n["status"] for n in nodes]
            st = "not_run" if all(x == "not_run" for x in sts) else aggregate(sts)
            return {"kind": kind, "status": st, "nodes": nodes}
        if kind == "group":
            g = self.groups_run.get(ev["group"])
            if g is None:
                return {"kind": kind, "group": ev["group"], "status": "not_run", "reason": "group not run"}
            prefix = tuple(pth.rstrip("/") + "/" for pth in PYTEST_GROUPS[ev["group"]]["paths"])
            mine = [r for k, rs in self.cases.items() if k.startswith(prefix) for r in rs]
            failed = [r["case"] for r in mine if r["status"] == "failed"]
            st = "failed" if failed or not mine else "passed"
            return {
                "kind": kind,
                "group": ev["group"],
                "status": st,
                "cases": len(mine),
                "failed": failed[:20],
                "summary": g.get("summary"),
            }
        if kind == "vitest":
            if not self.vitest_ran:
                return {**ev, "status": "not_run", "reason": "UI tests not run"}
            hits = [c for c in self.vitest if c["file"].endswith(ev["file"]) and ev["name"] in c["name"]]
            if not hits:
                return {**ev, "status": "missing", "reason": "no matching vitest case"}
            st = (
                "failed"
                if any(h["status"] == "failed" for h in hits)
                else ("blocked" if all(h["status"] == "skipped" for h in hits) else "passed")
            )
            return {**ev, "status": st, "cases": [h["name"] for h in hits]}
        if kind == "playwright":
            return {
                **ev,
                "status": "not_run",
                "reason": "Playwright smoke needs a browser and a running deployment (REVA_E2E_BASE_URL); not run",
            }
        if kind == "command":
            c = self.commands.get(ev["name"])
            if c is None:
                return {**ev, "status": "not_run", "reason": "command not run"}
            return {
                **ev,
                "status": "passed" if c["exit_code"] == 0 else "failed",
                "exit_code": c["exit_code"],
                "output_tail": c["output_tail"],
            }
        if kind == "demo":
            doc = self.demos.get(ev["mode"])
            if doc is None:
                return {
                    **ev,
                    "status": "blocked" if ev["mode"] == "live" else "not_run",
                    "reason": f"no {ev['mode']} demonstration result (scripts/demo_workflow.py --{ev['mode']})",
                }
            if doc.get("setup_error"):
                return {**ev, "status": "failed", "reason": doc["setup_error"]}
            if ev["step"] == "*":
                sts = [s["status"] for s in doc["steps"]]
                st = "failed" if "fail" in sts else ("blocked" if "blocked" in sts else "passed")
                return {
                    **ev,
                    "status": st,
                    "overall": doc.get("overall"),
                    "summary": doc.get("summary"),
                    "finished_at": doc.get("finished_at"),
                }
            s = next((s for s in doc["steps"] if s["id"] == ev["step"]), None)
            if s is None:
                return {**ev, "status": "missing", "reason": "step not in demo result"}
            st = {"pass": "passed", "fail": "failed", "blocked": "blocked"}[s["status"]]
            return {
                **ev,
                "status": st,
                "title": s["title"],
                "reason": s.get("blocked_reason")
                or s.get("error")
                or ([c["name"] for c in s["checks"] if c["status"] == "fail"] or None),
            }
        if kind == "demo_evidence":
            doc = self.demos.get("fixtures")
            s = next((s for s in (doc or {}).get("steps", []) if s["id"] == ev["step"]), None)
            if s is None or s["status"] == "blocked":
                return {**ev, "status": "not_run", "reason": "demo step not available"}
            val = s["evidence"].get(ev["key"])
            ok = {"gt0": lambda v: isinstance(v, (int, float)) and v > 0, "true": lambda v: v is True}[ev["predicate"]](
                val
            )
            return {**ev, "status": "passed" if ok else "failed", "value": val, "note": s["evidence"].get("note")}
        if kind == "live_checks":
            if self.live is None:
                return {
                    **ev,
                    "status": "blocked",
                    "reason": "var/live/live_checks.json absent (run scripts/live_source_checks.py)",
                }
            v = (self.live.get("gates") or {}).get(ev["key"])
            st = "passed" if v in ("pass", True) else ("missing" if v is None else "failed")
            return {
                **ev,
                "status": st,
                "value": v,
                "recorded_at": self.live.get("finished_at"),
                "git_head": self.live.get("git_head"),
            }
        if kind == "perf":
            if self.perf is None:
                return {
                    **ev,
                    "status": "blocked",
                    "reason": "var/perf/perf_results.json absent (run scripts/measure_performance.py)",
                }
            c = (self.perf.get("checks") or {}).get(ev["key"])
            if c is None:
                return {**ev, "status": "missing", "reason": "measurement not recorded"}
            st = {"pass": "passed", "fail": "failed"}.get(c.get("status"), "blocked")
            return {
                **ev,
                "status": st,
                "value": c.get("value"),
                "limit": c.get("limit"),
                "measured_as": c.get("measured_as"),
                "recorded_at": self.perf.get("generated_at"),
            }
        if kind == "doc":
            p = ROOT / ev["path"]
            return {**ev, "status": "passed" if p.exists() else "missing"}
        if kind == "manual":
            return {**ev}
        raise ValueError(f"unknown evidence kind {kind}")


def aggregate(statuses: list[str]) -> str:
    counted = [s for s in statuses if s != "not_run"]
    if not counted:
        return "blocked" if statuses else "missing"
    return max(counted, key=lambda s: STATUS_ORDER[s])


def evaluate_criterion(ev: Evaluator, name: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    checks = [ev.check(e) for e in evidence]
    statuses = [c["status"] for c in checks if not (c.get("optional") and c["status"] == "not_run")]
    st = aggregate(statuses)
    reasons: list[str] = []
    for c in checks:
        if c["status"] in ("failed", "blocked", "missing"):
            if c["kind"] == "pytest":
                reasons += [
                    f"{n['node']}: {n['status']}"
                    + (f" ({n.get('message') or n.get('reason')})" if n.get("message") or n.get("reason") else "")
                    for n in c["nodes"]
                    if n["status"] in ("failed", "blocked", "missing")
                ]
            else:
                label = c.get("name") or c.get("node") or c.get("step") or c.get("key") or c.get("path") or c["kind"]
                why = c.get("reason") or c.get("note") or c.get("output_tail")
                reasons.append(f"{c['kind']}:{label}: {c['status']}" + (f" ({why})" if why else ""))
    if st == "blocked" and all(c["status"] == "not_run" for c in checks):
        reasons.append("all evidence excluded from this run")
    return {"name": name, "status": st, "checks": checks, "reasons": reasons}


# --------------------------------------------------------------------------- main
def git_info() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        return {}

    def q(*a: str) -> str:
        return subprocess.run([git, *a], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()

    return {
        "head": q("rev-parse", "HEAD"),
        "branch": q("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_paths": len(q("status", "--porcelain").splitlines()),
    }


def load_json(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def run_demo(mode: str) -> dict[str, Any] | None:
    out = OUT_DIR / f"demo_{mode}.json"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [sys.executable, "scripts/demo_workflow.py", f"--{mode}", "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    (LOG_DIR / f"demo_{mode}.log").write_text(p.stdout + p.stderr)
    return load_json(out)


def build(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(UTC)
    groups = [g.strip() for g in args.groups.split(",")] if args.groups else list(DEFAULT_GROUPS)
    if args.with_perf and "perf" not in groups:
        groups.append("perf")
    if args.with_live and "live" not in groups:
        groups.append("live")
    unknown = [g for g in groups if g not in PYTEST_GROUPS and g != "ui"]
    if unknown:
        raise SystemExit(f"unknown groups {unknown}; known: {sorted(PYTEST_GROUPS)} + ui")
    groups = [g for g in groups if g in PYTEST_GROUPS]
    runs: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    if args.reuse:
        prev = load_json(REPORT_JSON) or {}
        runs = {g: r for g, r in (prev.get("pytest_runs") or {}).items() if g in groups or not args.groups}
        groups = sorted(set(groups) | set(runs), key=list(PYTEST_GROUPS).index)
        commands = prev.get("commands") or {}
        vit = prev.get("ui_run")
    else:
        vit = None
        if args.groups:  # partial refresh: keep the other groups/commands/UI results of the previous report
            prev = load_json(REPORT_JSON) or {}
            runs = dict(prev.get("pytest_runs") or {})
            commands = dict(prev.get("commands") or {})
            vit = prev.get("ui_run")
        if not args.groups or args.commands:
            for name in COMMANDS:
                print(f"[acceptance] command {name} ...", flush=True)
                commands[name] = run_command(name)
        for g in groups:
            print(f"[acceptance] pytest group {g} ...", flush=True)
            runs[g] = run_group(g)
            print(f"[acceptance]   {runs[g]['summary']} ({runs[g]['duration_s']} s)", flush=True)
        if not args.no_ui and (not args.groups or "ui" in args.groups.split(",")):
            print("[acceptance] ui vitest ...", flush=True)
            vit = run_vitest()
        groups = sorted(set(groups) | set(runs), key=list(PYTEST_GROUPS).index)
    excluded = [m for m in ("live", "perf") if m not in runs]
    cases = parse_pytest_junit([JUNIT_DIR / f"{g}.xml" for g in runs])
    vitest_cases = parse_vitest_junit(JUNIT_DIR / "ui_vitest.xml") if vit and vit.get("junit") else []
    demos: dict[str, dict[str, Any] | None] = {"fixtures": None, "live": None}
    for mode in ("fixtures", "live"):
        want = args.demo if mode == "fixtures" else ("run" if args.with_live and args.demo != "skip" else "reuse")
        if want == "run" and not args.reuse:
            print(f"[acceptance] demo workflow ({mode}) ...", flush=True)
            demos[mode] = run_demo(mode)
        elif want in ("run", "reuse"):
            demos[mode] = load_json(OUT_DIR / f"demo_{mode}.json")
    live, perf = load_json(LIVE_CHECKS), load_json(PERF_RESULTS)
    ev = Evaluator(
        cases=cases,
        groups_run=runs,
        vitest=vitest_cases,
        vitest_ran=bool(vitest_cases),
        commands=commands,
        demos=demos,
        live=live,
        perf=perf,
        excluded_markers=excluded,
    )
    phases = []
    for ph in PHASES:
        gates = [evaluate_criterion(ev, name, evidence) for name, evidence in ph["gates"]]
        phases.append(
            {
                "phase": ph["phase"],
                "title": ph["title"],
                "status": aggregate([g["status"] for g in gates]),
                "gates": gates,
            }
        )
    acceptance = []
    for section, item, evidence in ACCEPTANCE:
        acceptance.append({"section": section, **evaluate_criterion(ev, item, evidence)})
    mvp = [evaluate_criterion(ev, name, evidence) for name, evidence in MVP_GATES]
    all_gates = [g for ph in phases for g in ph["gates"]]

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        return {k: sum(1 for i in items if i["status"] == k) for k in ("passed", "failed", "blocked", "missing")}

    totals = {"passed": 0, "failed": 0, "skipped": 0}
    for rs in cases.values():
        for r in rs:
            totals[r["status"]] = totals.get(r["status"], 0) + 1
    vt = {k: sum(1 for c in vitest_cases if c["status"] == k) for k in ("passed", "failed", "skipped")}
    failing_tests = sorted(
        f"{k}" + (f" [{r['case']}]" if r["case"] != k.split("::")[1] else "")
        for k, rs in cases.items()
        for r in rs
        if r["status"] == "failed"
    )
    docs = {
        name: {"path": f"docs/dev/{name}.md", "present": (ROOT / "docs" / "dev" / f"{name}.md").exists()}
        for name in (
            "review_core",
            "spec_audit",
            "perf_report",
            "live_checks",
            "review_api_sim_ops",
            "review_science",
            "test_triage",
        )
    }
    demo_summary = {}
    for mode, d in demos.items():
        if d is None:
            demo_summary[mode] = {"status": "not_run"}
            continue
        demo_summary[mode] = {
            "overall": d.get("overall"),
            "summary": d.get("summary"),
            "setup_error": d.get("setup_error"),
            "finished_at": d.get("finished_at"),
            "database": (d.get("database") or {}).get("name"),
            "steps": [
                {
                    "id": s["id"],
                    "title": s["title"],
                    "status": s["status"],
                    "reason": s.get("blocked_reason")
                    or s.get("error")
                    or [c["name"] for c in s["checks"] if c["status"] == "fail"]
                    or None,
                }
                for s in d["steps"]
            ],
            "file": f"var/acceptance/demo_{mode}.json",
        }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "started_at": started.isoformat(),
        "git": git_info(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "options": {
            "groups": groups,
            "excluded_markers": excluded,
            "with_perf": args.with_perf,
            "with_live": args.with_live,
            "reuse": args.reuse,
            "demo": args.demo,
            "ui": not args.no_ui,
        },
        "status_vocabulary": {
            "passed": "all counted evidence passed",
            "failed": "at least one piece of evidence failed",
            "blocked": "evidence could not run here (credentials, deployment, network, browser) "
            "or was excluded from this run",
            "missing": "no automated evidence exists for (part of) the criterion",
        },
        "summary": {
            "phase_gates": counts(all_gates),
            "acceptance_tests": counts(acceptance),
            "mvp_gates": counts(mvp),
            "phases": {p["phase"]: p["status"] for p in phases},
            "pytest_cases": totals,
            "ui_cases": vt,
            "failing_tests": failing_tests,
        },
        "phases": phases,
        "acceptance_tests": acceptance,
        "mvp_gates": mvp,
        "demo": demo_summary,
        "pytest_runs": runs,
        "ui_run": vit,
        "commands": commands,
        "artifacts": {
            "live_checks": {
                "path": "var/live/live_checks.json",
                "present": live is not None,
                "finished_at": (live or {}).get("finished_at"),
                "git_head": (live or {}).get("git_head"),
                "gates": (live or {}).get("gates"),
            },
            "perf_results": {
                "path": "var/perf/perf_results.json",
                "present": perf is not None,
                "generated_at": (perf or {}).get("generated_at"),
            },
        },
        "documents": docs,
        "deployment": {"status": "not_deployed", "reason": NO_DEPLOY},
    }
    return report


def md_escape(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


ICON = {"passed": "passed", "failed": "**FAILED**", "blocked": "_blocked_", "missing": "_missing_"}


def evidence_brief(c: dict[str, Any]) -> str:
    k = c["kind"]
    if k == "pytest":
        ok = sum(1 for n in c["nodes"] if n["status"] == "passed")
        return f"pytest {ok}/{len(c['nodes'])}"
    if k == "group":
        return f"group {c['group']} ({c.get('cases', 0)} cases)"
    if k == "vitest":
        return "vitest"
    if k == "demo":
        return f"demo[{c['mode']}] step {c['step']}"
    if k == "demo_evidence":
        return f"demo evidence {c['key']}={c.get('value')}"
    if k == "command":
        return f"cmd {c['name']}"
    if k == "perf":
        return f"perf {c['key']}={c.get('value')}"
    if k == "live_checks":
        return f"live {c['key']}"
    return k


def render_md(r: dict[str, Any]) -> str:
    L: list[str] = []
    s = r["summary"]
    L.append("# REVA World — acceptance report")
    L.append("")
    L.append(
        f"Generated {r['generated_at']} by `scripts/acceptance_report.py` (machine-readable: "
        f"`var/acceptance/acceptance_report.json`, schema `{r['schema']}`). Git `{r['git'].get('head', '?')[:12]}` "
        f"on `{r['git'].get('branch')}` with {r['git'].get('dirty_paths')} uncommitted paths. "
        "This file is generated; do not edit by hand."
    )
    L.append("")
    L.append(
        "Status vocabulary: **passed** (all counted evidence passed), **failed**, **blocked** (could not run "
        "here: credentials, deployment, network, browser, or excluded from this run), **missing** (no automated "
        "evidence exists). A criterion is never reported passed without evidence."
    )
    L.append("")
    L.append("**Not deployed.** " + r["deployment"]["reason"] + ".")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("| Ledger | passed | failed | blocked | missing |")
    L.append("|---|---|---|---|---|")
    for key, label in (
        ("phase_gates", "P0–P7 gate criteria"),
        ("acceptance_tests", "§13 mandatory acceptance tests"),
        ("mvp_gates", "§17.4 MVP gates"),
    ):
        c = s[key]
        L.append(f"| {label} | {c['passed']} | {c['failed']} | {c['blocked']} | {c['missing']} |")
    L.append("")
    L.append("Phases: " + ", ".join(f"{p} {ICON[v]}" for p, v in s["phases"].items()))
    L.append("")
    pc, uc = s["pytest_cases"], s["ui_cases"]
    L.append(
        f"pytest cases: {pc.get('passed', 0)} passed, {pc.get('failed', 0)} failed, {pc.get('skipped', 0)} "
        f"skipped (groups: {', '.join(r['options']['groups'])}; excluded markers: "
        f"{', '.join(r['options']['excluded_markers']) or 'none'}). UI vitest: {uc['passed']} passed, "
        f"{uc['failed']} failed."
    )
    L.append("")
    if s["failing_tests"]:
        L.append("Failing tests:")
        L.append("")
        for t in s["failing_tests"][:60]:
            L.append(f"- `{t}`")
        L.append("")
    L.append("## Demonstration workflow (§17.3)")
    L.append("")
    for mode, d in r["demo"].items():
        if d.get("status") == "not_run":
            L.append(f"- **{mode}**: not run" + (" (run `scripts/demo_workflow.py --live`)" if mode == "live" else ""))
            continue
        L.append(
            f"- **{mode}** (`{d['file']}`): overall `{d['overall']}`, {d['summary']}"
            + (f", setup error: {d['setup_error']}" if d.get("setup_error") else "")
        )
    L.append("")
    fx = r["demo"].get("fixtures") or {}
    if fx.get("steps"):
        L.append("| Step | Status | Title | Reason |")
        L.append("|---|---|---|---|")
        for st in fx["steps"]:
            L.append(f"| {st['id']} | {st['status']} | {md_escape(st['title'])} | {md_escape(st['reason'] or '')} |")
        L.append("")
    L.append("## Phase gates (architecture §25)")
    L.append("")
    for ph in r["phases"]:
        L.append(f"### {ph['phase']} — {ph['title']}: {ICON[ph['status']]}")
        L.append("")
        L.append("| Gate criterion | Status | Evidence | Reasons |")
        L.append("|---|---|---|---|")
        for g in ph["gates"]:
            L.append(
                f"| {md_escape(g['name'])} | {ICON[g['status']]} | "
                f"{md_escape('; '.join(evidence_brief(c) for c in g['checks']))} | "
                f"{md_escape('; '.join(g['reasons'])[:600])} |"
            )
        L.append("")
    L.append("## Mandatory acceptance tests (implementation prompt §13)")
    L.append("")
    L.append("| Section | Test | Status | Evidence | Reasons |")
    L.append("|---|---|---|---|---|")
    for a in r["acceptance_tests"]:
        L.append(
            f"| {a['section']} | {md_escape(a['name'])} | {ICON[a['status']]} | "
            f"{md_escape('; '.join(evidence_brief(c) for c in a['checks']))} | "
            f"{md_escape('; '.join(a['reasons'])[:600])} |"
        )
    L.append("")
    L.append("## MVP gates (architecture §17.4)")
    L.append("")
    L.append("| Gate | Status | Evidence | Reasons |")
    L.append("|---|---|---|---|")
    for g in r["mvp_gates"]:
        L.append(
            f"| {md_escape(g['name'])} | {ICON[g['status']]} | "
            f"{md_escape('; '.join(evidence_brief(c) for c in g['checks']))} | "
            f"{md_escape('; '.join(g['reasons'])[:600])} |"
        )
    L.append("")
    L.append("## Commands")
    L.append("")
    for name, c in r["commands"].items():
        L.append(f"- `{name}`: exit {c['exit_code']} ({c['duration_s']} s, log `{c['log']}`)")
    L.append("")
    L.append("## Recorded artefacts and reports")
    L.append("")
    a = r["artifacts"]
    L.append(
        f"- Live source checks: `{a['live_checks']['path']}` "
        + (
            f"(finished {a['live_checks']['finished_at']}, git {str(a['live_checks']['git_head'])[:12]})"
            if a["live_checks"]["present"]
            else "(absent)"
        )
    )
    L.append(
        f"- Performance results: `{a['perf_results']['path']}` "
        + (f"(generated {a['perf_results']['generated_at']})" if a["perf_results"]["present"] else "(absent)")
    )
    for name, d in r["documents"].items():
        L.append(f"- [{name}]({name}.md)" if d["present"] else f"- {name}: absent")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```")
    L.append("uv run python scripts/demo_workflow.py              # §17.3 demo -> var/acceptance/demo_fixtures.json")
    L.append("uv run python scripts/demo_workflow.py --live       # live demo -> var/acceptance/demo_live.json")
    L.append("uv run python scripts/acceptance_report.py          # this report (add --with-perf --with-live)")
    L.append("```")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--with-perf", action="store_true", help="also run the perf-marked tests (tests/performance)")
    ap.add_argument(
        "--with-live",
        action="store_true",
        help="also run live-marked tests and the live demonstration (real provider requests)",
    )
    ap.add_argument("--groups", help=f"comma-separated pytest groups (default: {','.join(DEFAULT_GROUPS)})")
    ap.add_argument(
        "--demo",
        choices=("run", "reuse", "skip"),
        default="run",
        help="fixture-mode demonstration: run it, reuse var/acceptance/demo_fixtures.json, or skip",
    )
    ap.add_argument("--no-ui", action="store_true", help="do not run the UI vitest suite")
    ap.add_argument(
        "--commands",
        action="store_true",
        help="with --groups: also re-run the command checks (lint, contracts, imports)",
    )
    ap.add_argument(
        "--reuse",
        action="store_true",
        help="do not run anything; rebuild the report from existing JUnit/demo/command results",
    )
    args = ap.parse_args(argv)
    report = build(args)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(render_md(report))
    s = report["summary"]
    print(f"\nacceptance report -> {REPORT_JSON.relative_to(ROOT)} and {REPORT_MD.relative_to(ROOT)}")
    print(f"  phase gates      {s['phase_gates']}")
    print(f"  acceptance tests {s['acceptance_tests']}")
    print(f"  mvp gates        {s['mvp_gates']}")
    print(f"  phases           {s['phases']}")
    print(f"  pytest cases     {s['pytest_cases']}  ui {s['ui_cases']}")
    failed = s["phase_gates"]["failed"] + s["acceptance_tests"]["failed"] + s["mvp_gates"]["failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
