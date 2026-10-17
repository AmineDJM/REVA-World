"""FIX6 (PRED-CONFLICT): zero-by-absence resolution for event-census tables (UCDP GED country-month)."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from reva.evaluation.registry.absence import (
    ZERO_LABEL,
    absence_cell_ref,
    absence_coverage,
    absence_rule_of,
)
from reva.evaluation.registry.questions import resolution_value
from reva.evaluation.registry.service import resolve_batch

DS = "atlas.ucdp_ged.country_month"
IND = "deaths_best|tov=all"


def _frame(cells: list[tuple[str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "indicator": [IND] * len(cells),
            "provider_geo": [c[0] for c in cells],
            "period_start": [date.fromisoformat(c[1] + "-01") for c in cells],
            "value": [c[2] for c in cells],
        }
    )


def _q(period: str, rule: bool = True) -> dict[str, object]:
    res: dict[str, object] = {
        "source": DS,
        "issue_table": {"artifact_sha256": "i" * 64},
        "void_after": "2027-06-01",
    }
    if rule:
        res["absence_rule"] = "zero_within_coverage"
    y, m = (int(x) for x in period.split("-"))
    end = date(y + (m == 12), m % 12 + 1, 1)
    return {
        "question_id": "fq_" + period + str(rule),
        "family": "conflict.cm",
        "geo": "SRB",
        "frequency": "M",
        "reference_period": period,
        "period_end": end.isoformat(),
        "type": "quantity",
        "resolution": res,
    }


def test_family_rule_only_on_event_census_tables() -> None:
    assert absence_rule_of({"dataset_key": DS, "absence_rule": "zero_within_coverage"}) == "zero_within_coverage"
    assert absence_rule_of({"dataset_key": DS}) is None
    with pytest.raises(ValueError):
        absence_rule_of({"dataset_key": "atlas.wb.wdi_econ", "absence_rule": "zero_within_coverage"})
    with pytest.raises(ValueError):
        absence_rule_of({"dataset_key": DS, "absence_rule": "zero_always"})


def test_absent_cell_resolves_as_labelled_zero_only_within_coverage() -> None:
    later = _frame([("gw:666", "2026-01", "12"), ("gw:625", "2026-03", "40")])
    cov = absence_coverage(later, IND, "M")
    assert cov == {
        "first": "2026-01",
        "last": "2026-03",
        "basis": "first to last period with a published cell of the indicator (any geography)",
    }
    cell = resolution_value(_q("2026-02"), {}, cov)
    assert cell is not None and cell.value == "0"
    ref = cell.refs[0]
    assert ref["absent_cell"] is True and ref["value_label"] == ZERO_LABEL and ref["coverage"] == cov
    assert resolution_value(_q("2026-04"), {}, cov) is None  # beyond the version's coverage: wait / void
    assert resolution_value(_q("2026-02", rule=False), {}, cov) is None  # rule not sealed with the question
    assert resolution_value(_q("2026-02"), {}) is None  # no coverage given
    assert absence_cell_ref({**_q("2026-02"), "resolution": {"absence_rule": "zero_within_coverage"}}, cov) is None


def test_resolve_batch_uses_coverage_of_the_resolving_version() -> None:
    q = _q("2026-02")
    manifest = {
        "issued_at": "2026-01-15T00:00:00+00:00",
        "families": {"conflict.cm": {"dataset_key": DS, "absence_rule": "zero_within_coverage"}},
        "questions": [q],
        "forecasts": [],
    }
    issue_cov = absence_coverage(_frame([("gw:666", "2025-12", "3")]), IND, "M")
    later_cov = absence_coverage(_frame([("gw:666", "2026-01", "5"), ("gw:2", "2026-03", "1")]), IND, "M")
    pin = {"dataset_key": DS, "dataset_id": "d", "artifact_sha256": "l" * 64, "recorded_at": "2026-04-20"}
    res = resolve_batch(
        manifest,
        set(),
        {"conflict.cm": {}},
        {"conflict.cm": [(pin, {})]},
        today=date(2026, 5, 1),
        coverage={"i" * 64: issue_cov, "l" * 64: later_cov},  # type: ignore[dict-item]
    )
    (o,) = res["outcomes"]
    assert o["status"] == "resolved" and o["value"] == "0"
    assert o["source"]["cells"][0]["absent_cell"] is True
    # without the coverage the question keeps waiting (no fabricated zero)
    res2 = resolve_batch(manifest, set(), {"conflict.cm": {}}, {"conflict.cm": [(pin, {})]}, today=date(2026, 5, 1))
    assert res2["outcomes"] == []
