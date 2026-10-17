"""Closed vocabularies of the effect-estimate layer (migration ``0019_effect_estimates``; contract
``docs/dev/v5_effect_interface.md``). The migration restates these lists in SQL; a unit test keeps them equal."""

from __future__ import annotations

import re

CAUSE_KINDS = ("atlas_concept", "variable", "event_type", "event")
EFFECT_KINDS = ("atlas_concept", "variable")
TRANSFORMS = (
    "log_change",
    "percent_change",
    "pp_change",
    "level_change",
    "share_of_gdp_change",
    "std_dev_shock",
    "occurrence",
    "level",
    "log_level",
    "growth_rate",
    "gap",
)
CAUSE_ONLY_TRANSFORMS = ("occurrence", "std_dev_shock")
EFFECT_TRANSFORMS = tuple(t for t in TRANSFORMS if t not in CAUSE_ONLY_TRANSFORMS)
#: measure -> allowed units (None: any unit).
MEASURE_UNITS: dict[str, tuple[str, ...] | None] = {
    "elasticity": ("ratio", "percent_per_percent"),
    "pass_through": ("ratio", "percent_per_percent"),
    "semi_elasticity": ("percent_per_pp", "percent_per_unit"),
    "slope": ("pp_per_pp", "pp_per_percent", "unit_per_unit"),
    "multiplier": ("currency_per_currency", "ratio"),
    "response": ("percent", "percentage_points", "log_points"),
    "identity_coefficient": None,
}
MEASURES = tuple(MEASURE_UNITS)
UNITS = (
    "ratio",
    "percent_per_percent",
    "percent_per_pp",
    "percent_per_unit",
    "pp_per_pp",
    "pp_per_percent",
    "unit_per_unit",
    "currency_per_currency",
    "percent",
    "percentage_points",
    "log_points",
)
SIGNS = ("positive", "negative", "zero", "ambiguous")
#: How the cause moves in the experiment the number refers to (PRED-ENGINE reads lag profiles as responses to it):
#: one_time_impulse = a structural shock at t=0, the cause then follows its own estimated dynamics;
#: sustained = the cause is held at the new value over the horizon; permanent_level_shift = a static/permanent
#: change of the level (e.g. input-output price computations); state_indicator = an episode/status that is on
#: (dummy = 1) while it lasts; contemporaneous_relation = a regression slope between co-moving variables, no
#: shock path; cross_sectional = differences across units, no time path; unspecified = the source does not say.
SHOCK_PROFILES = (
    "one_time_impulse",
    "sustained",
    "permanent_level_shift",
    "state_indicator",
    "contemporaneous_relation",
    "cross_sectional",
    "unspecified",
)
POINT_KINDS = ("point", "mean", "median")
INTERVAL_KINDS = ("confidence", "credible", "range_across_estimates", "interquartile", "min_max")
UNCERTAINTY_STATUSES = ("reported", "partial", "not_reported")
HORIZON_KINDS = ("impact", "at_horizon", "cumulative", "peak", "average", "long_run", "unspecified")
HORIZON_UNITS = ("day", "month", "quarter", "year")
METHODS = (
    "meta_analysis",
    "rct",
    "iv",
    "did",
    "rdd",
    "synthetic_control",
    "event_study",
    "local_projection",
    "var",
    "svar",
    "bvar",
    "dsge",
    "structural_model",
    "input_output",
    "panel_regression",
    "time_series_regression",
    "cross_section_regression",
    "gravity",
    "calibration",
    "accounting_identity",
    "physical_law",
    "other",
)
SOURCE_KINDS = ("publication", "reva_run", "identity")
TRANSCRIPTIONS = ("machine_readable", "transcribed", "transcribed_double_checked", "computed")
RETRACTION_REASONS = ("withdrawn_by_curator", "removed_at_source", "absent_from_delivery", "run_invalidated")
ORIGINS = ("live", "test_fixture")

KEY_RE = re.compile(r"^(pub|run|idn):[A-Za-z0-9_.:-]{3,300}$")
PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
VAR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,199}$")
ENTITY_ID_RE = re.compile(r"^(Q[1-9][0-9]*|[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+)$")
SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
ISO3_RE = re.compile(r"^[A-Z]{3}$")

#: Plain-language unit phrases for the attributed wording (en, fr).
UNIT_WORDS: dict[str, tuple[str, str]] = {
    "ratio": ("", ""),
    "percent_per_percent": ("% per 1%", "% pour 1 %"),
    "percent_per_pp": ("% per percentage point", "% par point de pourcentage"),
    "percent_per_unit": ("% per unit", "% par unité"),
    "pp_per_pp": ("percentage point per percentage point", "point par point de pourcentage"),
    "pp_per_percent": ("percentage point per 1%", "point de pourcentage pour 1 %"),
    "unit_per_unit": ("units per unit", "unités par unité"),
    "currency_per_currency": ("per currency unit", "par unité monétaire"),
    "percent": ("%", " %"),
    "percentage_points": (" percentage points", " point(s) de pourcentage"),
    "log_points": (" log points", " points log"),
}
