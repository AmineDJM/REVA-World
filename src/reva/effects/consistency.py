"""Unit / transform consistency of an estimate's cause and effect with the variable they name (FIX4B, audit M3).

Audit AUDIT3 M3: four REVA event-study estimates stored a cumulative GDP *level* response (a cumulative sum of
growth rates, ≈ % change of the level) under the GDP *growth* concept, and the decision engine showed it as
"annual growth". The rules below refuse that class of mistake. They rely on curated identifier links in
``registry/effects/variables.yaml`` (never on names):

* a level variable may declare ``growth_variable`` (the effect variable that is its growth rate) and
  ``growth_concepts`` (atlas concepts that are its growth rate, e.g. ``wb.ny.gdp.pcap.kd.zg``);
* *growth-rate ids* = every ``growth_variable``, every ``growth_concepts`` entry and the atlas concepts a growth
  variable links to (``atlas_concepts``).

Rules (reason codes, prefixed ``cause_`` / ``effect_`` by the loader):

* ``level_transform_on_growth_concept`` — a percent / log change or log level of a growth rate;
* ``percent_response_on_growth_concept`` — a ``response`` in ``percent`` / ``log_points`` on a growth rate (a
  growth rate moves in percentage points);
* ``cumulative_lhs_on_growth_concept`` — the effect definition declares a cumulative left-hand side (cumulative
  sum of growth rates) on a growth rate: that is a level response, it belongs on the level variable;
* ``growth_rate_on_level_variable`` — transform ``growth_rate`` on a level variable that has a curated
  ``growth_variable``: the estimate must name that growth variable, so that every label says "growth".

Pure module.
"""

from __future__ import annotations

import re

LEVEL_TRANSFORMS_ON_GROWTH = ("percent_change", "log_change", "log_level")
CUMULATIVE_RE = re.compile(r"cumulative sum|cumsum|cumulated growth|sum of (the )?growth", re.IGNORECASE)


def side_problem(
    *,
    transform: str | None,
    is_growth: bool,
    growth_variable: str | None,
    definition: str | None = None,
    measure: str | None = None,
    unit: str | None = None,
    is_effect: bool = True,
) -> str | None:
    """Reason code when the transform / unit contradicts what the named variable is, else None."""
    if transform in (None, "occurrence", "std_dev_shock"):
        return None
    if is_growth:
        if transform in LEVEL_TRANSFORMS_ON_GROWTH:
            return "level_transform_on_growth_concept"
        if is_effect and measure == "response" and unit in ("percent", "log_points"):
            return "percent_response_on_growth_concept"
        if is_effect and definition and CUMULATIVE_RE.search(definition):
            return "cumulative_lhs_on_growth_concept"
        return None
    if transform == "growth_rate" and growth_variable:
        return "growth_rate_on_level_variable"
    return None
