"""Zero-by-absence resolution for event-census tables (FIX6, for PRED-CONFLICT country-month families).

UCDP GED / Candidate aggregates (``atlas.ucdp_ged.*_month``) write a cell only where GED records at least one
event: the atlas never writes a zero (``atlas_ucdp_ged`` module doc). A question about a country-month therefore
has no cell to resolve on when nothing happened. The rule below lets such a question resolve, explicitly:

* the family declares ``absence_rule: zero_within_coverage`` in its spec, and the question carries it in its sealed
  ``resolution`` (a question sealed without it never resolves by absence);
* the family's table is an event census listed in :data:`ZERO_BY_ABSENCE_TABLES` (any other table refuses the rule);
* the table version used for resolution *covers* the question's period: covered periods are the contiguous range
  from the first to the last period in which that version publishes a cell of the family's indicator for *any*
  geography (GED is a global census of UCDP-coded organised violence: every state is covered in every month of the
  window; the question was issued for a current state of the family's universe);
* then an absent cell resolves as ``0`` with the cell reference ``{"absent_cell": true, ...}`` labelled "0 recorded
  (no event recorded by the source in a covered period)". The zero exists only in the resolution record, never in
  the atlas table.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import polars as pl

from reva.evaluation.registry import periods as P

ZERO_WITHIN_COVERAGE = "zero_within_coverage"

#: event-census tables whose absent cells mean "no recorded event" (connector documentation, quoted in the rule)
ZERO_BY_ABSENCE_TABLES: dict[str, str] = {
    f"atlas.ucdp_ged.{t}": (
        "UCDP GED / Candidate aggregate: cells are emitted only where GED records >= 1 event; an absent cell in the "
        "covered period means GED records no event (GED inclusion rules: UCDP-coded organised violence, >= 1 death)"
    )
    for t in (
        "country_month",
        "adm1_month",
        "priogrid_month",
        "cand_country_month",
        "cand_adm1_month",
        "cand_priogrid_month",
    )
}

ABSENCE_RULE_TEXT = (
    "zero_within_coverage: within the periods the resolving table version covers (first to last period in which it "
    "publishes a cell of this indicator for any geography), an absent cell for the question's geography means no "
    "event was recorded by the source; the question resolves as 0 recorded, labelled as such; the zero is never "
    "written into the atlas"
)
ZERO_LABEL = "0 recorded (no cell: no event recorded by the source in a covered period)"


def absence_rule_of(family_doc: Mapping[str, Any]) -> str | None:
    """The family's declared absence rule, validated against its table (None when undeclared)."""
    rule = family_doc.get("absence_rule")
    if rule is None:
        return None
    if rule != ZERO_WITHIN_COVERAGE:
        raise ValueError(f"unknown absence_rule {rule!r}")
    ds = str(family_doc.get("dataset_key") or "")
    if ds not in ZERO_BY_ABSENCE_TABLES:
        raise ValueError(f"absence_rule {rule} refused: {ds or 'no table'} is not an event-census table")
    return str(rule)


def absence_coverage(frame: pl.DataFrame, indicator: str, frequency: str) -> dict[str, Any] | None:
    """Covered period range of one table version for one indicator (None when it publishes no such cell)."""
    f = frame.filter(pl.col("indicator") == indicator).select("period_start")
    if f.height == 0:
        return None
    lo = P.index_of(frequency, f["period_start"].min())  # type: ignore[arg-type]
    hi = P.index_of(frequency, f["period_start"].max())  # type: ignore[arg-type]
    return {
        "first": P.label_of(frequency, lo),
        "last": P.label_of(frequency, hi),
        "basis": "first to last period with a published cell of the indicator (any geography)",
    }


def absence_cell_ref(q: Mapping[str, Any], coverage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The labelled zero for ``q`` when its sealed resolution declares the rule and ``coverage`` covers its period;
    None otherwise (the question then waits for a cell, or voids by its void date)."""
    res = q.get("resolution") or {}
    if res.get("absence_rule") != ZERO_WITHIN_COVERAGE or not coverage:
        return None
    if str(res.get("source") or "") not in ZERO_BY_ABSENCE_TABLES:
        return None
    freq = str(q["frequency"])
    idx = P.parse_label(freq, str(q["reference_period"]))
    if not P.parse_label(freq, str(coverage["first"])) <= idx <= P.parse_label(freq, str(coverage["last"])):
        return None
    return {
        "absent_cell": True,
        "value_label": ZERO_LABEL,
        "rule": ABSENCE_RULE_TEXT,
        "table_semantics": ZERO_BY_ABSENCE_TABLES[str(res["source"])],
        "coverage": dict(coverage),
    }


__all__ = [
    "ABSENCE_RULE_TEXT",
    "ZERO_BY_ABSENCE_TABLES",
    "ZERO_LABEL",
    "ZERO_WITHIN_COVERAGE",
    "absence_cell_ref",
    "absence_coverage",
    "absence_rule_of",
]
