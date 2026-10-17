"""Vintage-aware accessor (V5, PRED-VINTAGE): proofs that no as-of answer uses information published later.

Synthetic vintages make the boundaries exact; a property test over real captured WEO / SPF vintages checks
the same invariants on provider data.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import atlas_vintage_philfed as pf
from reva.adapters.connectors import atlas_vintage_weo as weo
from reva.adapters.connectors.vintage_common import Rows, vintage_flags, year_bounds
from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.vintage_backtests import weo_backtest
from reva.evaluation.vintages import (
    CELL_SCHEMA,
    FINAL_VINTAGE_LABEL,
    VintageAccessor,
    cells_from_atlas_frame,
)
from reva.kernel.errors import CapabilityGap, LeakageBlocked, ValidationProblem

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "source"
SHA = "a" * 64
REC = datetime(2026, 10, 17, 12, tzinfo=UTC)


def _t(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)  # type: ignore[misc]


def _cells(spec: list[tuple[str, datetime | None, int, str, str]], geo: str = "FRA") -> pl.DataFrame:
    """(vintage, available_at, year, value, estimate_kind) -> CELL_SCHEMA frame of family imf_weo."""
    rows = Rows()
    for v, avail, y, val, kind in spec:
        s, e = year_bounds(y)
        rows.add(
            source="vintage_weo",
            dataset="weo_dbnomics",
            indicator=f"NGDP_RPCH|v={v}",
            provider_geo=geo,
            start=s,
            end=e,
            value=val,
            unit="pcent",
            raw_sha256=SHA,
            selector={"v": v, "y": y},
            flags=vintage_flags(
                family="imf_weo",
                vintage=v,
                available_at=avail,
                basis="weo_release_month_end" if avail else None,
                variable="real_gdp_growth",
                provider_variable="NGDP_RPCH",
                estimate_kind=kind,
            ),
        )
    return cells_from_atlas_frame(rows.frame(), dataset_key="atlas.vintage_weo.weo_dbnomics", table_recorded_at=REC)


BASE = [
    ("2008-04", _t(2008, 5, 1), 2007, "1.9", "published_history_boundary_unknown"),
    ("2008-04", _t(2008, 5, 1), 2008, "1.4", "projection"),
    ("2008-10", _t(2008, 11, 1), 2007, "2.2", "published_history_boundary_unknown"),
    ("2008-10", _t(2008, 11, 1), 2008, "0.8", "projection"),
    ("2009-04", _t(2009, 5, 1), 2008, "0.7", "published_history_boundary_unknown"),
]


def _value(scope, year: int) -> str | None:  # type: ignore[no-untyped-def]
    r = scope.rows.filter(pl.col("period_start") == date(year, 1, 1))
    return r["value"][0] if r.height else None


def test_value_changes_exactly_at_the_availability_instant() -> None:
    cells = _cells(BASE)
    just_before = _t(2008, 11, 1) - timedelta(microseconds=1)
    assert _value(VintageAccessor(cells, _t(2008, 6, 1)).latest(), 2007) == "1.9"
    assert _value(VintageAccessor(cells, just_before).latest(), 2007) == "1.9"
    assert _value(VintageAccessor(cells, _t(2008, 11, 1)).latest(), 2007) == "2.2"
    assert VintageAccessor(cells, _t(2008, 4, 30)).latest().rows.height == 0  # nothing published yet
    acc = VintageAccessor(cells, _t(2008, 6, 1))
    assert acc.vintage_in_force("imf_weo")["vintage"] == "2008-04"
    assert acc.latest().exclusions["available_after_cutoff"] == 3


def test_later_vintages_never_change_an_earlier_answer() -> None:
    a = VintageAccessor(_cells(BASE), _t(2008, 12, 15)).latest(include_projections=True).rows
    more = BASE + [
        ("2009-10", _t(2009, 11, 1), 2007, "9.9", "published_history_boundary_unknown"),
        ("2009-10", _t(2009, 11, 1), 2008, "-9.9", "published_history_boundary_unknown"),
    ]
    b = VintageAccessor(_cells(more), _t(2008, 12, 15)).latest(include_projections=True).rows
    assert a.equals(b)


def test_projection_is_never_returned_as_an_observation() -> None:
    acc = VintageAccessor(_cells(BASE), _t(2008, 12, 1))
    obs = acc.latest()
    assert _value(obs, 2008) is None and obs.exclusions["only_projection_at_cutoff"] == 1
    fc = acc.forecasts(family="imf_weo")
    assert _value(fc, 2008) == "0.8" and set(fc.rows["estimate_kind"]) == {"projection"}
    # once a later vintage publishes 2008 as data, the observation appears (and the projection is superseded)
    acc2 = VintageAccessor(_cells(BASE), _t(2009, 6, 1))
    assert _value(acc2.latest(), 2008) == "0.7" and _value(acc2.forecasts(family="imf_weo"), 2008) is None


def test_no_availability_proof_means_exclusion_never_a_guess() -> None:
    cells = _cells(
        [("1989Q4", None, 1990, "2.0", "projection"), ("2008-04", _t(2008, 5, 1), 2008, "1.4", "projection")]
    )
    scope = VintageAccessor(cells, _t(2026, 10, 1)).forecasts(family="imf_weo")
    assert scope.rows["vintage"].to_list() == ["2008-04"]
    assert scope.exclusions["no_availability_proof"] == 1
    assert scope.exclusion_report()[0]["reason"] == "no_availability_proof"


def test_as_known_mode_needs_reva_to_have_recorded_the_table() -> None:
    cells = _cells(BASE)
    old = VintageAccessor(cells, _t(2010, 1, 1), CutoffMode.as_known).latest()
    assert old.rows.height == 0 and old.exclusions["recorded_after_cutoff"] == len(BASE)
    now = VintageAccessor(cells, _t(2026, 10, 18), CutoffMode.as_known).latest()
    assert _value(now, 2008) == "0.7"


def test_retrospective_mode_is_explicit_and_labelled() -> None:
    cells = _cells(BASE)
    retro = VintageAccessor(cells, _t(2008, 6, 1), CutoffMode.revised_data_retrospective)
    with pytest.raises(CapabilityGap):
        retro.latest()
    fv = retro.final_vintage()
    assert fv.label == FINAL_VINTAGE_LABEL and _value(fv, 2008) == "0.7"  # uses the 2009 vintage: labelled
    with pytest.raises(CapabilityGap):
        VintageAccessor(cells, _t(2008, 6, 1)).final_vintage()


def test_guard_blocks_a_late_cell_even_if_selection_were_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    acc = VintageAccessor(_cells(BASE), _t(2008, 6, 1))
    monkeypatch.setattr(VintageAccessor, "_admissible", lambda self, df: (df, __import__("collections").Counter()))
    with pytest.raises(LeakageBlocked):
        acc.latest(include_projections=True)


def test_history_and_first_release() -> None:
    acc = VintageAccessor(_cells(BASE), _t(2009, 6, 1))
    h = acc.history(family="imf_weo", variable="real_gdp_growth", geo="FRA", period_start=date(2007, 1, 1))
    assert h.rows["vintage"].to_list() == ["2008-04", "2008-10"] and h.rows["value"].to_list() == ["1.9", "2.2"]
    fr = acc.first_release(family="imf_weo")
    assert _value(fr, 2007) == "1.9" and _value(fr, 2008) == "0.7"
    early = VintageAccessor(_cells(BASE), _t(2008, 12, 1)).first_release(family="imf_weo")
    assert _value(early, 2008) is None  # not yet released at that cutoff


def test_inputs_are_validated() -> None:
    with pytest.raises(ValidationProblem):
        VintageAccessor(_cells(BASE), datetime(2008, 6, 1))  # naive datetime
    with pytest.raises(ValidationProblem):
        VintageAccessor(pl.DataFrame({"x": [1]}), _t(2008, 6, 1))
    assert set(_cells(BASE).columns) == set(CELL_SCHEMA.names())


# ---------------------------------------------------------------------------------------------- real vintages
def _fixture(conn: str, url_part: str) -> bytes:
    man = json.loads((FIX / conn / "manifest.json").read_text())
    (hit,) = [e for e in man if url_part in e["url"]]
    return (FIX / conn / hit["file"]).read_bytes()


def _real_cells() -> pl.DataFrame:
    rows: Rows = Rows()
    for v in weo.FIXTURE_DBN_VINTAGES:
        docs, _ = weo.parse_dbn_page(_fixture("atlas.vintage_weo_dbn", f"WEO:{v}"), v)
        weo.parse_dbn(docs, v, SHA, rows, {})
    weo.parse_imf(
        _fixture("atlas.vintage_weo_imf", "WEO_2025_OCT_VINTAGE"), SHA, "WEO_2025_OCT_VINTAGE", "1.0.0", rows, {}
    )
    a = cells_from_atlas_frame(rows.frame(), dataset_key="atlas.vintage_weo.mixed", table_recorded_at=REC)
    spf = Rows()
    dates = pf.parse_release_dates(_fixture("atlas.vintage_philfed", "spf-release-dates.txt"))
    pf.parse_spf(_fixture("atlas.vintage_philfed", "medianlevel.xlsx"), "level", SHA, dates, spf, {})
    b = cells_from_atlas_frame(spf.frame(), dataset_key="atlas.vintage_us.spf_median", table_recorded_at=REC)
    return pl.concat([a, b])


def test_property_no_selected_cell_is_published_after_the_cutoff() -> None:
    cells = _real_cells()
    rng = random.Random(20261017)
    instants = sorted(set(cells["available_at"].drop_nulls().to_list()))
    cutoffs = [_t(1985, 1, 1), _t(2026, 10, 17)]
    for a in instants:  # around every publication instant
        cutoffs += [a - timedelta(microseconds=1), a, a + timedelta(days=rng.randint(1, 200))]
    for c in cutoffs:
        acc = VintageAccessor(cells, c)
        for fam in ("imf_weo", "philfed_spf"):
            for scope in (acc.latest(family=fam, include_projections=True), acc.forecasts(family=fam)):
                got = scope.rows
                assert got.filter(pl.col("available_at").is_null() | (pl.col("available_at") > c)).height == 0
                # each selected cell is the latest admissible vintage of its series key
                adm = cells.filter((pl.col("family") == fam) & (pl.col("available_at") <= c))
                best = adm.group_by("variable", "provider_geo", "period_start", "period_end").agg(
                    pl.col("available_at").max()
                )
                j = got.join(best, on=["variable", "provider_geo", "period_start", "period_end"], suffix="_best")
                assert j.filter(pl.col("available_at") != pl.col("available_at_best")).height == 0


def test_spf_as_of_a_release_date_uses_that_survey_not_the_next() -> None:
    cells = _real_cells()
    # 2008:Q2 survey released 5/13/08 -> available 2008-05-14; the day before, 2008:Q1 is in force
    before = VintageAccessor(cells, _t(2008, 5, 13, 23)).forecasts(family="philfed_spf", variable="real_gdp_level")
    after = VintageAccessor(cells, _t(2008, 5, 14)).forecasts(family="philfed_spf", variable="real_gdp_level")
    q3 = date(2008, 7, 1)
    assert before.rows.filter(pl.col("period_start") == q3)["vintage"].to_list() == ["2008Q1"]
    assert after.rows.filter(pl.col("period_start") == q3)["vintage"].to_list() == ["2008Q2"]


def test_weo_backtest_uses_only_the_vintage_in_force() -> None:
    spec = BASE + [
        ("2009-10", _t(2009, 11, 1), 2008, "0.4", "published_history_boundary_unknown"),
        ("2009-10", _t(2009, 11, 1), 2009, "-2.0", "published_history_boundary_unknown"),
        ("2008-10", _t(2008, 11, 1), 2009, "0.5", "projection"),
        ("2009-04", _t(2009, 5, 1), 2009, "-3.0", "projection"),
        ("2010-10", _t(2010, 11, 1), 2009, "-2.5", "published_history_boundary_unknown"),
    ]
    out = weo_backtest(_cells(spec), origins=[_t(2008, 6, 1), _t(2008, 12, 1)], horizons=(0, 1))
    det = {(d["origin"][:10], d["horizon"]): d for d in out["details"]}
    assert det[("2008-06-01", 0)]["vintage_in_force"] == "2008-04"
    assert det[("2008-12-01", 0)]["vintage_in_force"] == "2008-10"
    h0 = out["scores"]["h0"]["all"]
    # June 2008: forecast 1.4 vs persistence 1.9 (2007 as then published), outcome 0.4 (Oct 2009 vintage)
    # Dec 2008: forecast 0.8 vs persistence 2.2, outcome 0.4
    assert h0["n"] == 2 and h0["mae_forecast"] == pytest.approx((1.0 + 0.4) / 2)
    assert h0["mae_baseline"] == pytest.approx((1.5 + 1.8) / 2)
    assert out["scores"]["h1"]["all"]["n"] == 1  # Dec 2008 forecast for 2009 (0.5) scored against Oct 2010
    assert out["scored_as_of"] == _t(2010, 11, 1).isoformat()
