"""Versioned, machine-readable capability catalogue.

REVA must answer an unsupported request with an explicit capability gap, never a
plausible-looking output (architecture §1, §16.3, §19, invariant 34). This module
is the single declaration of what the running build can and cannot do:

* ``implemented`` — executable code exists and its acceptance tests run in CI.
* ``reserved``    — a typed extension interface exists (or is planned) but the
  capability is not available; requests receive ``capability_not_available``.
* ``blocked``     — unavailable because an external precondition (configuration,
  infrastructure, licensed data) is absent, even though an interface exists.

Later-phase capabilities (P8–P12) are *never* ``implemented`` here. A few pure
verification helpers that those phases' acceptance gates need (an analytic
field-aggregation check, a double-entry ledger verifier, a BMI lifecycle checker)
are listed separately with ids under ``verification.*`` and are explicitly
labelled verification-only: they do not provide the domain capability.

The catalogue reads one configuration value (whether ``REVA_SANDBOX_ENDPOINT`` is
set) and never exposes the endpoint or any token.

``require(capability_id)`` raises :class:`reva.kernel.errors.CapabilityGap`
with structured details for anything not implemented; ``as_dict()`` is the JSON
shape for API exposure (e.g. ``GET /v1/models``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from reva.kernel.errors import CapabilityGap
from reva.kernel.ids import logical_hash

CATALOGUE_VERSION = "1.0.0"
CATALOGUE_SCHEMA = "reva.capability_catalogue/1"

Status = Literal["implemented", "reserved", "blocked"]
PHASES: tuple[str, ...] = (*(f"P{i}" for i in range(13)), "V2", "unscheduled")

SANDBOX_ENDPOINT_ENV = "REVA_SANDBOX_ENDPOINT"


@dataclass(frozen=True)
class Capability:
    capability_id: str
    title: str
    status: Status
    phase: str
    owner_module: str
    """Module that owns (or, per architecture §22/§25, will own) the capability."""
    required_evidence: tuple[str, ...]
    """Acceptance gates / evidence that must pass before status may become implemented
    (for implemented entries: the gates that currently hold it there)."""
    reason: str | None = None
    """Why the capability is not available. Required unless implemented."""
    interface: str | None = None
    """Dotted path of the typed extension interface present in this build, if any."""
    requires_config: tuple[str, ...] = ()
    verification_only: bool = False
    notes: str | None = None
    available_instead: tuple[str, ...] = field(default=())
    tests: tuple[str, ...] = field(default=())
    """Acceptance tests that hold an implemented capability there, as ``path::test_name``
    (repository-relative). ``implemented`` without a cited, existing test is refused by
    tests/unit/test_capabilities_v2.py (V2 honesty rule 5)."""
    docs: tuple[str, ...] = field(default=())
    """Repository-relative documentation entries (required for implemented V2 entries)."""

    def __post_init__(self) -> None:
        if self.status not in ("implemented", "reserved", "blocked"):
            raise ValueError(f"invalid status {self.status!r} for {self.capability_id}")
        if self.phase not in PHASES:
            raise ValueError(f"invalid phase {self.phase!r} for {self.capability_id}")
        if self.status != "implemented" and not (self.reason and self.reason.strip()):
            raise ValueError(f"{self.capability_id}: a {self.status} capability needs a reason")
        if not self.required_evidence:
            raise ValueError(f"{self.capability_id}: required_evidence must name at least one gate")

    @property
    def available(self) -> bool:
        return self.status == "implemented"

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "title": self.title,
            "status": self.status,
            "available": self.available,
            "phase": self.phase,
            "owner_module": self.owner_module,
            "interface": self.interface,
            "required_evidence": list(self.required_evidence),
            "reason": self.reason,
            "requires_config": list(self.requires_config),
            "verification_only": self.verification_only,
            "notes": self.notes,
            "available_instead": list(self.available_instead),
            "tests": list(self.tests),
            "docs": list(self.docs),
        }


@dataclass(frozen=True)
class CapabilityNotAvailable:
    """Typed result returned (instead of an output) for a non-implemented capability."""

    capability_id: str
    status: Literal["reserved", "blocked", "unknown"]
    phase: str | None
    reason: str
    required_evidence: tuple[str, ...]
    catalogue_version: str = CATALOGUE_VERSION
    kind: Literal["capability_not_available"] = "capability_not_available"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capability_id": self.capability_id,
            "status": self.status,
            "phase": self.phase,
            "reason": self.reason,
            "required_evidence": list(self.required_evidence),
            "catalogue_version": self.catalogue_version,
        }

    def to_error(self) -> CapabilityGap:
        return CapabilityGap(
            f"capability {self.capability_id!r} is not available ({self.status}): {self.reason}",
            **{k: v for k, v in self.as_dict().items() if k != "capability_id"},
            capability=self.capability_id,
        )


_ROADMAP = "architecture §25 phase table"

#: Tests holding the V1 implemented entries (path::test_name); V2 entries cite theirs inline.
_V1_TESTS: dict[str, tuple[str, ...]] = {
    "fidelity.L0": ("tests/integration/test_simulation_engine.py::test_l0_cache_miss_then_hit_after_l1",),
    "fidelity.L1": (
        "tests/integration/test_simulation_engine.py::test_l1_exposure_matches_hand_computation",
        "tests/unit/test_simulation_v2_plugins.py::test_crude_plugin_is_bit_identical_to_v1_engine",
    ),
    "fidelity.L2": ("tests/integration/test_simulation_engine.py::test_l2_sensitivity_range",),
    "coupling.acyclic": (
        "tests/scientific/test_crude_exposure_coupling_rng.py::test_acyclic_graph_schedules_and_cycle_rejected",
    ),
    "forecasting.baselines": ("tests/unit/forecasting/test_forecast_models.py::test_models_satisfy_protocol",),
    "verification.analytic_field_aggregation": (
        "tests/unit/test_extensions_physical.py::test_linear_in_latitude_matches_closed_form_spherical_integral",
    ),
    "verification.double_entry_balance": (
        "tests/unit/test_extensions_interfaces.py::test_balanced_ledger_passes",
        "tests/unit/test_extensions_interfaces.py::test_imbalance_detected_with_residual",
    ),
    "verification.external_model_lifecycle": (
        "tests/unit/test_extensions_physical.py::test_lifecycle_conformance_passes_for_conforming_model",
    ),
}

_BASE: tuple[Capability, ...] = (
    # ------------------------------------------------------------------ P4/P5: V1 executable envelope
    Capability(
        "fidelity.L0",
        "Exact full-specification cache lookup",
        "implemented",
        "P4",
        "reva.simulation.engine",
        ("identical spec digest + pinned manifests return the cached result; otherwise an explicit cache miss",),
        notes="No surrogate: a near-identical specification is a cache miss.",
    ),
    Capability(
        "fidelity.L1",
        "Deterministic fixed-quantity crude-value exposure",
        "implemented",
        "P4",
        "reva.models.economic.crude_exposure",
        ("hand-computed cases match", "p=1 gives zero", "missing input blocks"),
        notes="Assumption-conditioned accounting model; not GDP loss, inflation, unemployment or welfare.",
    ),
    Capability(
        "fidelity.L2",
        "Labelled elasticity sensitivity of crude-value exposure",
        "implemented",
        "P4",
        "reva.models.economic.crude_exposure",
        ("elasticity 0 equals L1", "registered elasticity envelope enforced"),
        notes="Sensitivity grid over registered elasticities; not an uncertainty distribution.",
    ),
    Capability(
        "fidelity.L3",
        "Paired Monte Carlo uncertainty propagation",
        "reserved",
        "P4",
        "reva.simulation.engine",
        (
            "every parameter distribution has justified provenance (registered evidence)",
            "named counter-based random streams verified scheduling-independent",
            "Monte Carlo error reported with the result",
        ),
        reason=(
            "no model parameter has a distribution with justified provenance; sampling invented "
            "distributions would fabricate uncertainty"
        ),
        available_instead=("fidelity.L2",),
    ),
    Capability(
        "fidelity.L4",
        "Competing models, global sensitivity and deep research",
        "reserved",
        "P11",
        "reva.simulation.engine",
        (
            "more than one approved model for the estimand with locked-holdout tournament evidence",
            "named sensitivity method valid for the input dependence structure",
            "secure external research execution (autonomy.external_sandbox_runner)",
        ),
        reason="research-grade multi-model execution and global sensitivity are not implemented",
        available_instead=("fidelity.L1", "fidelity.L2"),
    ),
    Capability(
        "coupling.acyclic",
        "Acyclic one-way model coupling with port compatibility and SCC detection",
        "implemented",
        "P4",
        "reva.simulation.coupling",
        ("port stock/flow/rate compatibility enforced", "strongly connected components detected and rejected"),
    ),
    Capability(
        "coupling.feedback",
        "Feedback-block coupling solver",
        "reserved",
        "P9",
        "reva.simulation.feedback",
        (
            "explicit convergence contract (step size, criterion, damping, iteration cap, conservative exchange, "
            "error bounds)",
            "solver converges on reference problems",
            "non-convergence fails or returns qualified partial results, never the last iterate as equilibrium",
        ),
        reason="V1 executes acyclic coupling only; no feedback solver has passed reference-problem convergence",
        interface="reva.extensions.feedback",
        available_instead=("coupling.acyclic",),
    ),
    Capability(
        "horizon.multi_period",
        "Dynamic multi-period simulation",
        "reserved",
        "P9",
        "reva.simulation.engine",
        ("stock/flow ledger and dynamic model with validated transition dynamics",),
        reason="the V1 model is a one-annual-period accounting identity; dynamics are not modelled",
    ),
    Capability(
        "outputs.macroeconomic",
        "GDP loss, unemployment, consumer inflation or household welfare outputs",
        "reserved",
        "P9",
        "reva.models.economic",
        ("structural macro/IO model with identification review", "independent causal policy model review"),
        reason="exposure arithmetic is not a macroeconomic or welfare model",
    ),
    Capability(
        "forecasting.baselines",
        "last_value_v1, log_drift_v1 and local_level_log_v1 forecasts",
        "implemented",
        "P5",
        "reva.models.forecasting",
        ("fit inside fold", "insufficient observations produce an ineligible report", "naive baseline shown"),
    ),
    # ------------------------------------------------------------------ verification-only helpers
    Capability(
        "verification.analytic_field_aggregation",
        "Conservative spherical area-weighted aggregation of an analytic field (verification only)",
        "implemented",
        "P8",
        "reva.models.physical.verification",
        (
            "constant field returns its exact value",
            "separable analytic field matches closed-form spherical integral to 1e-9 relative",
            "partition of regions conserves the global integral",
            "antimeridian-crossing region handled",
        ),
        interface="reva.models.physical.verification",
        verification_only=True,
        notes=(
            "Numerical verification utility for the P8 acceptance gate. It does not read datasets and provides "
            "no weather, climate or hazard capability."
        ),
    ),
    Capability(
        "verification.double_entry_balance",
        "Double-entry and stock-flow consistency verifier (verification only)",
        "implemented",
        "P9",
        "reva.extensions.io_sfc",
        ("every transaction balances per unit", "instrument claims net to zero", "opening + flows = closing"),
        interface="reva.extensions.io_sfc",
        verification_only=True,
        notes="Pure checker of supplied ledgers; there is no SFC or IO model in this build.",
    ),
    Capability(
        "verification.external_model_lifecycle",
        "BMI-style external-model lifecycle conformance checker",
        "implemented",
        "P8",
        "reva.models.physical.adapter",
        ("initialize/update/get_value/finalize lifecycle exercised", "declared units and monotone model time"),
        interface="reva.models.physical.adapter",
        verification_only=True,
        notes="Checks an in-process object against the port contract; no external model is integrated.",
    ),
    # ------------------------------------------------------------------ P8 physical fields
    Capability(
        "physical.field_adapter",
        "CF/COG/Zarr physical-field adapter and STAC asset catalogue",
        "reserved",
        "P8",
        "reva.models.physical.fields",
        (
            "CF metadata (standard name, units, calendar, CRS, axes, cell methods) validated on ingest",
            "antimeridian and CRS tests",
            "same concept/support contract works without kernel change",
        ),
        reason="no field format reader, asset catalogue or field storage is implemented",
        interface="reva.models.physical.fields",
    ),
    Capability(
        "physical.field_to_region",
        "Conservative field-to-region aggregation of real datasets",
        "reserved",
        "P8",
        "reva.world.regridding",
        (
            "known analytic field integrates correctly (see verification.analytic_field_aggregation)",
            "real region geometries with versioned boundaries",
            "regridding error recorded as a versioned model component",
        ),
        reason="only the verification helper on rectangular regions exists; no polygon support or real fields",
        interface="reva.models.physical.adapter",
        available_instead=("verification.analytic_field_aggregation",),
    ),
    Capability(
        "physical.hazard_exposure",
        "Bounded historical heat/drought exposure dataset",
        "reserved",
        "P8",
        "reva.adapters.physical",
        ("withheld temporal/spatial validation", "no weather forecast claimed from climate averages"),
        reason="no hazard dataset has been acquired, validated or licensed",
    ),
    Capability(
        "physical.external_model",
        "Coupled external physical model via BMI/FMI port",
        "reserved",
        "P8",
        "reva.models.physical.adapter",
        ("external-model port conformance", "calendar/timestep/support translation as versioned components"),
        reason="no external model is integrated; only the conformance protocol and checker exist",
        interface="reva.models.physical.adapter",
        available_instead=("verification.external_model_lifecycle",),
    ),
    # ------------------------------------------------------------------ P9 economics
    Capability(
        "economic.input_output",
        "Multi-region sector input-output model with capacity constraints",
        "reserved",
        "P9",
        "reva.models.economic.io",
        (
            "accounting balances",
            "nonnegative feasible production",
            "no double counting between totals and bilateral data",
            "IO multipliers not applied to arbitrarily large supply shocks",
        ),
        reason="no licensed multi-region IO table is ingested and no IO model is implemented",
        interface="reva.extensions.io_sfc",
    ),
    Capability(
        "economic.sfc_ledger",
        "Stock-flow-consistent financial ledger model",
        "reserved",
        "P9",
        "reva.models.economic.ledger",
        ("double-entry balance on every period", "stock-flow reconciliation", "revaluation separated from flows"),
        reason="no sector balance-sheet data or SFC behavioural model exists; only a ledger verifier",
        interface="reva.extensions.io_sfc",
        available_instead=("verification.double_entry_balance",),
    ),
    Capability(
        "causal.policy_model",
        "Independently reviewed causal policy model",
        "reserved",
        "P9",
        "reva.models.economic",
        ("SCM/identification profile", "independent identification review"),
        reason="no causal identification profile has been reviewed; interventions remain typed assumptions",
    ),
    # ------------------------------------------------------------------ P10 population
    Capability(
        "population.households",
        "Household-aware synthetic population",
        "reserved",
        "P10",
        "reva.population.households",
        (
            "licensed/approved joint household data",
            "structural constraints exact",
            "uncertainty over unidentified joints",
            "held-out joints scored",
        ),
        reason=(
            "no approved joint person x household data: households cannot be linked to synthetic persons "
            "(member ages/sexes), household-aware IPU and multiple completed populations are not implemented; "
            "V2 provides household units from public composition x children cells only "
            "(population.household_margins)"
        ),
        interface="reva.extensions.population",
        available_instead=("population.household_margins",),
    ),
    Capability(
        "population.privacy_accounting",
        "Differential-privacy accounting for population releases",
        "reserved",
        "P10",
        "reva.population.privacy",
        ("explicit epsilon/delta and neighbouring relation", "privacy attack assessment"),
        reason="no sensitive microdata is processed and no DP mechanism is implemented",
        interface="reva.extensions.population",
    ),
    Capability(
        "compression.network",
        "Network-aware population compression and rare-event benchmarks",
        "reserved",
        "P10",
        "reva.compression.network",
        ("intervention/subgroup/tail compression certificates pass",),
        reason="household/contact networks are not represented",
    ),
    # ------------------------------------------------------------------ P11 autonomy
    Capability(
        "autonomy.external_sandbox_runner",
        "Disposable VM/microVM runner for generated code",
        "blocked",
        "P11",
        "reva.autonomy.external_runner",
        (
            "deliberate privilege/egress/resource attacks blocked",
            "no secrets, read-only inputs, egress blocked by default, bounded CPU/RAM/disk/time",
            "independent gate evidence",
        ),
        reason=f"no secure external runner is configured ({SANDBOX_ENDPOINT_ENV} unset); proposals remain unexecuted",
        interface="reva.extensions.runner",
        requires_config=(SANDBOX_ENDPOINT_ENV,),
    ),
    Capability(
        "autonomy.parser_repair",
        "Autonomous parser-repair branches",
        "reserved",
        "P11",
        "reva.autonomy.code_changes",
        ("isolated branch + external runner evidence", "named review for T2"),
        reason="generated parser code cannot run without autonomy.external_sandbox_runner",
    ),
    Capability(
        "models.shadow_promotion",
        "Shadow models, locked-holdout tournament and signed promotion",
        "reserved",
        "P11",
        "reva.operations.releases",
        ("no holdout access by proposer", "rollback exercised", "prospective improvement or rollback"),
        reason="V1 has a locked-holdout tournament (evaluation.tournament_service) but no shadow "
        "deployment, signed promotion or exercised rollback",
    ),
    # ------------------------------------------------------------------ P12 decisions
    Capability(
        "decision.search",
        "Robust multi-objective policy search",
        "reserved",
        "P12",
        "reva.decision.optimise",
        (
            "utility and constraints explicit in a DecisionSpec",
            "frontier robust across scenarios and models",
            "external intervention tests and distributional reporting",
        ),
        reason="no validated decision-relevant model set or robust optimiser; prediction does not choose values",
        interface="reva.extensions.decision",
    ),
    Capability(
        "decision.multi_actor",
        "Multi-actor reaction models",
        "reserved",
        "P12",
        "reva.decision.reactions",
        ("bounded two-actor test solved", "multiple equilibria retained", "no unsupported equilibrium claim"),
        reason="no reaction model or equilibrium solver has been validated",
        interface="reva.extensions.decision",
    ),
    Capability(
        "domains.ecological_social",
        "Ecological, health, governance and language domain plugins",
        "reserved",
        "P12",
        "reva.models.ecological",
        ("concepts, sources, model/measurement contract, tests and a benefiting task per domain",),
        reason="no planetary domain beyond the crude-exposure vertical slice has entered production",
    ),
    # ------------------------------------------------------------------ V2 roadmap (docs/roadmap/v2_design.md)
    # Model agents flip an entry to implemented only together with ``tests=`` (passing,
    # cited as path::test_name) and ``docs=`` entries; tests/unit/test_capabilities_v2.py
    # enforces both.
    Capability(
        "simulation.platform.dataset_inputs",
        "Release-pinned dataset inputs for simulation models",
        "implemented",
        "V2",
        "reva.simulation.datasets",
        (
            "datasets derive from archived acquisitions with the release origin",
            "a release pins datasets eligible at its cutoff, sealed with the release",
            "simulations read only pinned, re-hashed tables; unpinned is a typed missing input",
        ),
        tests=(
            "tests/integration/test_simulation_v2_datasets.py::test_release_pins_dataset_by_digest_and_seals_it",
            "tests/integration/test_simulation_v2_datasets.py::test_dataset_registration_requires_archived_sources",
            "tests/integration/test_simulation_v2_datasets.py::test_unpinned_dataset_is_missing_input_never_zero",
            "tests/integration/test_simulation_v2_datasets.py::test_dataset_recorded_after_cutoff_is_not_pinned",
        ),
        docs=("docs/dev/v2_eng.md",),
        notes="Platform capability only: no model consumes dataset inputs until its own entry is implemented.",
    ),
    Capability(
        "simulation.platform.period_results",
        "Half-open multi-period simulation result rows",
        "implemented",
        "V2",
        "reva.simulation.plugins",
        (
            "period both null or both set with start < end",
            "L0 reuse and output digests cover periods",
            "single-period digests unchanged",
        ),
        tests=(
            "tests/integration/test_simulation_v2_datasets.py::test_multi_period_dataset_model_runs_through_the_job_path",
            "tests/integration/test_simulation_v2_datasets.py::test_result_period_constraints",
            "tests/unit/test_simulation_v2_plugins.py::test_result_row_period_is_half_open_and_extends_digest_only_when_set",
        ),
        docs=("docs/dev/v2_eng.md",),
        notes="Platform capability only: the crude model stays single-period (horizon.multi_period is reserved).",
    ),
    Capability(
        "demography.cohort_component",
        "Cohort-component population projection (conditional projection, not a forecast)",
        "implemented",
        "V2",
        "reva.models.population.cohort_component",
        (
            "Eurostat demography inputs as pinned dataset inputs",
            "back-test 2013 -> 2023 by age and sex with published error metrics",
            "labelled conditional projection under stated assumptions",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/integration/test_demography_cohort.py::test_ingest_publishes_observations_and_registers_dataset_inputs",
            "tests/integration/test_demography_cohort.py::test_l1_projection_runs_through_the_job_path",
            "tests/integration/test_demography_cohort.py::test_l2_sensitivity_grid_and_range_rows",
            "tests/integration/test_demography_cohort.py::test_backtest_evaluation_is_stored_from_pinned_datasets",
            "tests/unit/test_population_cohort.py::test_residual_migration_reconstructs_observed_population_exactly",
            "tests/unit/test_population_cohort.py::test_no_information_after_jump_off",
            "tests/property/test_population_cohort_properties.py::"
            "test_population_balancing_equation_exact_and_negative_cohorts_flagged",
        ),
        docs=("docs/dev/v2_pop.md", "docs/dev/v2_pop_backtest.md"),
        notes=(
            "Experimental model (maintainer review pending). EU-27 + EFTA from Eurostat demo_pjan/demo_magec/"
            "demo_fasec; constant base-period mortality (no improvement); migration and fertility as labelled "
            "assumptions (zero, residual average, EUROPOP2023 baseline); L2 is an assumption grid without "
            "probability levels. Back-test errors are published, including countries where it does worse "
            "than a no-change benchmark."
        ),
    ),
    Capability(
        "economic.io_price",
        "Multi-region input-output cost-push price model (first-round cost-push; not an inflation forecast)",
        "implemented",
        "V2",
        "reva.models.economic.io_price",
        (
            "Eurostat FIGARO inter-country tables as pinned dataset inputs with accounting identities verified "
            "(OECD ICIO is not implemented)",
            "Leontief price solution checked against a hand-computed small system",
            "labelled first-round cost-push, full pass-through",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/test_io_tables.py::test_identity_violation_refuses_publication",
            "tests/unit/test_io_price.py::test_industry_prices_match_the_hand_computed_solution",
            "tests/unit/test_io_price.py::test_l2_pass_through_grid_and_one_sided_sensitivity_range",
            "tests/integration/test_io_price_pipeline.py::test_l1_scenario_end_to_end_matches_hand_computation",
            "tests/integration/test_io_price_pipeline.py::test_identity_failure_refuses_registration",
            "tests/unit/test_io_price.py::"
            "test_basket_is_basic_prices_and_carries_a_purchasers_price_reading_with_fixed_unit_taxes",
        ),
        docs=("docs/dev/v2_io.md", "docs/methodology.md", "docs/limitations.md"),
        notes=(
            "Experimental model (maintainer review pending). Eurostat FIGARO industry-by-industry table; "
            "exogenous output-price change of NACE B, C19 or D35 (B is mining and quarrying, broader than "
            "crude oil); fixed coefficients, no substitution or demand response, one period. Household "
            "basket at basic prices (taxes less subsidies on products excluded; each basket row carries a "
            "labelled purchasers'-price reading with product taxes fixed per unit). Not an inflation "
            "forecast, CPI or macro outcome (outputs.macroeconomic stays reserved)."
        ),
    ),
    Capability(
        "social.retirement_age",
        "Retirement-age module (France; Germany, Spain, Netherlands where sourced)",
        "implemented",
        "V2",
        "reva.models.social.retirement_age",
        (
            "legal-age rule tables with per-row citations verified against archived legal text (transcribed by an "
            "agent; maintainer review pending)",
            "literature parameters as cited assumptions with a sensitivity grid (verified values only)",
            "2010-reform back-test 2012 -> 2019 reported, including its weaknesses (placebo 55-59)",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/social/test_retirement_rules.py::test_every_row_is_cited_and_its_locator_is_in_the_archived_source",
            "tests/unit/social/test_retirement_age_model.py::test_change_equals_difference_of_levels_and_signs",
            "tests/unit/social/test_retirement_age_model.py::test_l2_grid_range_contains_every_point_and_no_response_reference",
            "tests/unit/social/test_retirement_age_model.py::test_countries_without_rule_tables_are_capability_gaps",
            "tests/unit/social/test_retirement_age_model.py::test_macro_outputs_are_capability_gaps",
            "tests/unit/social/test_retirement_backtest.py::test_no_grid_estimate_overlaps_the_backtested_reform_or_period",
            "tests/unit/social/test_retirement_backtest.py::test_backtest_is_reproducible_and_matches_the_committed_artifact",
            "tests/integration/test_labour_pensions_retirement_pipeline.py::test_retirement_scenario_runs_through_the_job_path_with_pinned_datasets",
        ),
        docs=("docs/dev/v2_pen.md", "docs/dev/v2_pen_backtest.md"),
        notes=(
            "Experimental model (maintainer review pending); conditional projection under stated assumptions, "
            "not a forecast. Outputs: employed persons 55-69, pension recipients, approximate pension "
            "expenditure change, old-age dependency ratio (from demography.cohort_component). No GDP, "
            "unemployment or welfare output. ITA, BEL, SWE, POL are refused (no sourced rule table); Spain is "
            "refused by its own data (contribution-track heterogeneity). The back-test fits well partly by "
            "offsetting errors; see docs/dev/v2_pen_backtest.md."
        ),
    ),
    # --- V3 SIMWORLD: the V2 simulations for every country (second registered model versions) ---
    Capability(
        "simulation.platform.model_versions",
        "Several registered versions of one model key (1.0.0 stays the default; later versions by name)",
        "implemented",
        "V2",
        "reva.simulation.plugins",
        (
            "a spec naming model_version runs exactly that version; unknown versions are capability gaps",
            "single-version keys keep their exported scenario-schema branch unchanged",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/test_simworld_models.py::test_registry_keeps_1_0_0_as_default_and_serves_1_1_0_by_version",
            "tests/unit/test_simworld_models.py::test_spec_validation_routes_by_version",
        ),
        docs=("docs/dev/v3_simworld.md",),
        notes="Agent SIMWORLD (V3). plugins.get(key, version); the scenario schema gets a '<key>@<version>' branch.",
    ),
    Capability(
        "demography.cohort_component.world",
        "Cohort-component projection for every UN WPP 2024 country (demography.cohort_component@1.1.0)",
        "implemented",
        "V2",
        "reva.models.population.cohort_world",
        (
            "UN WPP 2024 single-age inputs as pinned dataset inputs derived from archived raw bytes",
            "Eurostat path of 1.1.0 identical to 1.0.0",
            "2013 -> 2023 back-test against WPP estimates for every country, published including bad results",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/test_simworld_connectors.py::test_wpp_tables_are_exact_complete_and_filtered",
            "tests/unit/test_simworld_models.py::test_wpp_projection_rows_are_labelled_and_exact",
            "tests/unit/test_simworld_models.py::test_eurostat_path_of_1_1_0_equals_1_0_0_exactly",
            "tests/unit/test_simworld_models.py::test_wpp_refusals_are_typed",
            "tests/integration/test_simworld_pipeline.py::test_cohort_1_1_0_runs_for_non_eu_countries",
            "tests/integration/test_simworld_pipeline.py::test_cohort_all_countries_resolves_from_pinned_datasets",
        ),
        docs=("docs/dev/v3_simworld.md",),
        notes=(
            "Experimental (maintainer review pending). WPP estimates are model-based: the back-test checks "
            "consistency with WPP, not truth. Small territories whose open age would fall below 85 are refused "
            "(capability_gap). EUROPOP2023 assumptions remain EU/EFTA only."
        ),
    ),
    Capability(
        "social.retirement_age.world",
        "Retirement-age module outside the Eurostat LFS (social.retirement_age@1.1.0, ages 55-64)",
        "implemented",
        "V2",
        "reva.models.social.retirement_world",
        (
            "rule tables with per-row citations checked against an archived excerpt (WBL 2026 transcription, "
            "maintainer review pending) or archived law text",
            "ILOSTAT rates and WPP population as pinned dataset inputs",
            "typed refusals for missing inputs, ages outside 55-64 and outputs without data",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/test_simworld_models.py::test_wbl_tables_are_separate_cited_and_1_0_0_is_unchanged",
            "tests/unit/test_simworld_models.py::test_retirement_world_rows_and_signs",
            "tests/unit/test_simworld_models.py::test_retirement_world_l2_range_and_refusals",
            "tests/integration/test_simworld_pipeline.py::test_retirement_1_1_0_runs_for_non_eu_countries",
            "tests/integration/test_simworld_pipeline.py::test_retirement_refusals_are_typed",
        ),
        docs=("docs/dev/v3_simworld.md",),
        notes=(
            "Experimental (maintainer review pending); no back-test outside France. WBL ages are held constant "
            "as a labelled counterfactual reference; the employment response is a transferred European estimate; "
            "pension recipient levels and expenditure are not computed on this path."
        ),
    ),
    # --- V3 SIMFIX: pension coverage in the world retirement module (social.retirement_age@1.2.0) ---
    Capability(
        "social.retirement_age.pension_coverage",
        "Retirement-age changes applied to the pension-covered share only (social.retirement_age@1.2.0, "
        "ILOSTAT SDG 1.3.1 old-age pension coverage)",
        "implemented",
        "V2",
        "reva.models.social.retirement_coverage",
        (
            "coverage is a release-pinned dataset input from a reviewed built-in connector "
            "(ilostat.social_protection, archived raw bytes, Parquet)",
            "the source's own value and year are stated in every row (by sex when published); no invented "
            "coverage variant",
            "countries without coverage are refused with missing_input (never assumed covered)",
            "1.0.0 and 1.1.0 are unchanged",
        ),
        interface="reva.simulation.plugins",
        tests=(
            "tests/unit/test_simfix_coverage.py::test_parse_keeps_the_pension_function_by_sex_verbatim",
            "tests/unit/test_simfix_coverage.py::test_coverage_selection_by_sex_total_and_year",
            "tests/unit/test_simfix_coverage.py::test_effects_scale_with_coverage_and_full_coverage_equals_1_1_0",
            "tests/unit/test_simfix_coverage.py::test_missing_coverage_is_refused_never_assumed",
            "tests/integration/test_simfix_pipeline.py::test_coverage_ingest_registers_dataset_inputs",
            "tests/integration/test_simfix_pipeline.py::test_retirement_1_2_0_scales_with_coverage_through_the_job_path",
            "tests/integration/test_simfix_pipeline.py::test_retirement_1_2_0_refuses_countries_without_coverage",
        ),
        docs=("docs/dev/v3_simworld.md", "docs/limitations.md"),
        notes=(
            "Experimental (maintainer review pending). Coverage (persons above pensionable age receiving a "
            "pension) includes non-contributory pensions and is applied uniformly to ages 55-64: results can "
            "remain overstated where a social pension with its own age drives coverage. ILOSTAT publishes no "
            "contributor series; the ILO World Social Protection Database was not reachable (HTTP 403)."
        ),
    ),
    Capability(
        "economic.io_price.world",
        "Input-output price model beyond the FIGARO economies (a global multi-region IO table)",
        "reserved",
        "unscheduled",
        "reva.models.economic.io_price",
        (
            "an open global MRIO with an archivable bulk download and a licence allowing redistribution of "
            "derived tables",
            "accounting identities verified on the archived table",
        ),
        reason=(
            "No usable open global table (checked 2026-10-15): OECD ICIO bulk files on oecd.org answer HTTP 403 "
            "to this environment's clients and are not in the OECD SDMX API; EXIOBASE >= 3.9 is licensed "
            "non-commercial (customised CC BY-NC-SA) and covers 44 countries + 5 rest-of-world regions only; "
            "Eora is not open. economic.io_price stays limited to the 49 FIGARO economies + rest of world."
        ),
        docs=("docs/dev/v3_simworld.md",),
    ),
    Capability(
        "population.household_margins",
        "Weighted representative households from public composition x children cells (no person linkage)",
        "implemented",
        "V2",
        "reva.population.households",
        (
            "approved public joint household cells (Eurostat lfst_hhnhtych composition x number of children; "
            "no microdata)",
            "structural constraints exact (impossible cells refused, exact rational weights, no support violations)",
            "unidentified joints kept as bounds (open household sizes never imputed)",
            "held-out EU-SILC size distribution and average size scored; discrepancies reported, not corrected",
        ),
        tests=(
            "tests/unit/social/test_households.py::test_france_recipe_cells_structural_zeros_and_totals",
            "tests/unit/social/test_households.py::test_exact_weight_conservation_and_no_support_violations",
            "tests/unit/social/test_households.py::test_structural_zero_with_a_reported_value_is_refused",
            "tests/unit/social/test_households.py::test_missing_leaf_stays_missing_and_totals_gate_reports_remainder",
            "tests/unit/social/test_households.py::test_held_out_silc_scoring_is_reported_including_misses",
            "tests/unit/social/test_households.py::test_records_carry_no_person_attributes",
        ),
        docs=("docs/dev/v2_pen.md",),
        notes=(
            "V2 step 4 (agent PEN). Household units only: not linked to synthetic persons, no member age, sex or "
            "other attribute. The full P10 capability population.households stays reserved."
        ),
    ),
    Capability(
        "nl.scenario_drafts",
        "Natural-language scenario drafts (LLM drafts only; user confirms)",
        "implemented",
        "V2",
        "reva.nl",
        (
            "draft validated by the same schema and compiler",
            "unsupported asks become capability_gap items",
            "recorded-response tests; prompt-injection hardening; bounded LLM budget",
        ),
        interface="reva.nl.ports",
        requires_config=(
            "REVA_LLM_ENABLED=true",
            "egress proxy injecting the provider credentials",
            "ops.reserve_llm_budget / ops.settle_llm_budget (shipped in migration 0008_v2)",
        ),
        tests=(
            "tests/unit/test_nl_drafts.py::test_french_sensitivity_request_is_a_valid_draft_checked_by_the_same_validator",
            "tests/unit/test_nl_drafts.py::test_gdp_loss_and_unemployment_become_typed_gaps_never_another_output",
            "tests/unit/test_nl_drafts.py::test_stale_recorded_capability_gap_never_runs",
            "tests/unit/test_nl_drafts.py::test_hostile_multiplier_1000_is_rejected_by_the_validator_not_clamped",
            "tests/unit/test_nl_drafts.py::test_synthetic_outputs_referencing_ids_outside_the_allow_list_are_rejected",
            "tests/unit/test_nl_drafts.py::test_over_budget_refuses_before_any_provider_call",
            "tests/unit/test_nl_drafts.py::test_a_draft_never_runs_without_confirmation",
            "tests/integration/test_nl_drafts_api.py::test_draft_returns_a_valid_spec_and_runs_nothing",
            "tests/integration/test_nl_drafts_api.py::test_over_budget_is_429_and_nothing_is_sent",
            "tests/integration/test_nl_drafts_api.py::test_audit_row_has_hash_and_length_but_never_the_text",
            "tests/unit/test_nl_crosschecks.py::test_h2_all_countries_except_china_is_never_a_ready_draft",
            "tests/unit/test_nl_crosschecks.py::test_h3_oil_price_is_not_defaulted_to_french_refineries",
            "tests/unit/test_nl_crosschecks.py::test_m6_stated_age_level_is_never_converted_into_a_shift",
            "tests/unit/test_nl_crosschecks.py::test_restatement_never_contains_llm_written_text",
        ),
        docs=("docs/dev/v2_nlc.md", "docs/methodology.md", "docs/limitations.md"),
        notes="Drafts only: nothing runs until the user submits the reviewed spec to POST /v1/scenarios. Disabled "
        "unless REVA_LLM_ENABLED; budget llm.scenario_drafts (200 requests, 2,000,000 tokens per day); "
        "without the two ledger functions the API role fails closed (503). Lexical server cross-checks "
        "(French/English) withhold a draft that excludes countries, names a region with ALL, leaves an "
        "IO shock origin unstated or turns a stated retirement age into a shift; they can miss "
        "paraphrases, so the server restatement must still be reviewed.",
    ),
    Capability(
        "scenario.all_countries",
        "Crude border-price scenario for every country with data",
        "implemented",
        "V2",
        "reva.simulation.crude_plugin",
        (
            "M49/ISO3 crosswalk from archived provider lists; aggregates and composite reporters never mapped silently",
            "per-country typed gaps",
        ),
        tests=(
            "tests/unit/test_country_crosswalk.py::test_composite_and_special_codes_are_flagged_and_never_mapped",
            "tests/unit/test_country_crosswalk.py::test_committed_crosswalk_is_a_rebuild_of_the_archived_provider_lists",
            "tests/unit/test_comtrade_worldbank_all_connectors.py::"
            "test_rotation_covers_the_sweep_within_the_daily_budget",
            "tests/integration/test_comtrade_worldbank_all_countries.py::"
            "test_all_country_fixture_ingest_keeps_gaps_typed",
            "tests/integration/test_scenario_all_countries.py::"
            "test_all_countries_with_skip_policy_runs_complete_countries_and_lists_the_rest",
            "tests/integration/test_scenario_all_countries.py::"
            "test_all_countries_with_fail_policy_refuses_listing_every_missing_input",
        ),
        docs=("docs/dev/v2_allc.md",),
        notes="Engine/job path. POST /v1/simulations still pre-checks inputs as policy 'fail' and refuses "
        'countries ["ALL"] with a typed missing_input until the API preflight change requested in '
        "docs/dev/v2_allc.md lands. Crosswalk aliases are provider_declared (pending maintainer review).",
    ),
    Capability(
        "scenario.skip_missing_countries",
        "Explicit missing-input policy that runs complete countries and reports skipped ones",
        "implemented",
        "V2",
        "reva.simulation.crude_plugin",
        ("skipped countries listed with typed missing inputs", "never zero-filled"),
        tests=(
            "tests/integration/test_scenario_all_countries.py::"
            "test_explicit_list_with_skip_policy_and_all_missing_is_refused",
            "tests/integration/test_scenario_all_countries.py::"
            "test_skip_policy_gives_the_same_numbers_as_fail_for_complete_countries",
            "tests/integration/test_scenario_all_countries.py::test_v1_specs_keep_the_v1_contract",
        ),
        docs=("docs/dev/v2_allc.md",),
        notes="Engine/job path; the API preflight change is requested in docs/dev/v2_allc.md.",
    ),
    # ------------------------------------------------------------------ later
    Capability(
        "query.ask_reva",
        "Conversational 'Ask REVA' bounded query compiler",
        "reserved",
        "unscheduled",
        "reva.api",
        (
            "prose compiled only into a validated QuerySpec",
            "deterministic tools only; model applicability cannot be bypassed",
            "no prose-to-executable-code path",
        ),
        reason="later capability; V1 has no LLM budget and exposes typed QuerySpec/ScenarioSpec only",
    ),
)


def _sandbox_configured(settings: Any | None) -> bool:
    if settings is not None:
        value = getattr(settings, "reva_sandbox_endpoint", None)
    else:
        value = os.environ.get(SANDBOX_ENDPOINT_ENV)
    return bool(value and str(value).strip())


def catalogue(settings: Any | None = None) -> tuple[Capability, ...]:
    """Return the catalogue, resolving configuration-dependent statuses.

    With a sandbox endpoint configured the runner moves from ``blocked`` to
    ``reserved`` — never ``implemented``: no runner client has passed the P11
    attack gates in this build.
    """
    out: list[Capability] = []
    for cap in _BASE:
        if cap.capability_id in _V1_TESTS and not cap.tests:
            cap = replace(cap, tests=_V1_TESTS[cap.capability_id])
        if cap.capability_id == "autonomy.external_sandbox_runner" and _sandbox_configured(settings):
            cap = replace(
                cap,
                status="reserved",
                reason="an external runner endpoint is configured, but no runner client has passed the P11 "
                "privilege/egress/resource attack gates; generated code remains unexecuted",
            )
        out.append(cap)
    return tuple(out)


def get(capability_id: str, settings: Any | None = None) -> Capability | None:
    for cap in catalogue(settings):
        if cap.capability_id == capability_id:
            return cap
    return None


def not_available(capability_id: str, settings: Any | None = None) -> CapabilityNotAvailable | None:
    """Typed unavailable result, or ``None`` when the capability is implemented."""
    cap = get(capability_id, settings)
    if cap is None:
        return CapabilityNotAvailable(
            capability_id=capability_id,
            status="unknown",
            phase=None,
            reason="capability is not declared in the catalogue",
            required_evidence=(),
        )
    if cap.available:
        return None
    assert cap.reason is not None and cap.status != "implemented"
    return CapabilityNotAvailable(
        capability_id=cap.capability_id,
        status=cap.status,
        phase=cap.phase,
        reason=cap.reason,
        required_evidence=cap.required_evidence,
    )


def require(capability_id: str, settings: Any | None = None, **context: Any) -> Capability:
    """Return the implemented capability or raise a structured :class:`CapabilityGap`.

    ``context`` (JSON-safe values) is attached to the error details, e.g. the
    request digest that hit the gap.
    """
    gap = not_available(capability_id, settings)
    if gap is None:
        cap = get(capability_id, settings)
        assert cap is not None
        return cap
    err = gap.to_error()
    if context:
        err.details["context"] = context
    cap = get(capability_id, settings)
    if cap is not None:
        err.details["owner_module"] = cap.owner_module
        err.details["interface"] = cap.interface
        err.details["available_instead"] = list(cap.available_instead)
    raise err


def as_dict(settings: Any | None = None) -> dict[str, Any]:
    """JSON-serialisable catalogue for API exposure."""
    caps = [c.as_dict() for c in catalogue(settings)]
    body = {"schema": CATALOGUE_SCHEMA, "catalogue_version": CATALOGUE_VERSION, "capabilities": caps}
    counts = {s: sum(1 for c in caps if c["status"] == s) for s in ("implemented", "reserved", "blocked")}
    return {**body, "summary": counts, "digest": logical_hash(body)}
