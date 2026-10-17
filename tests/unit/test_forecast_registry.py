"""Prospective forecast registry (PRED-REGISTRY): periods, specs, questions, sealing, resolution, scores."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import pytest

from reva.atlas.tables import ATLAS_SCHEMA
from reva.evaluation.ledger import manifest_sha256
from reva.evaluation.registry import periods as P
from reva.evaluation.registry import scores as S
from reva.evaluation.registry.forecasts import forecast_baseline, predictive_from_record
from reva.evaluation.registry.questions import extract_series, yoy_pct
from reva.evaluation.registry.service import backtest_family, build_batch, resolve_batch
from reva.evaluation.registry.specs import SpecSet, load_specs, parse_specs
from reva.kernel.errors import ValidationProblem
from reva.models.forecasting.registry_baselines import Predictive

RAW = "a" * 64


def cells(
    dataset: str, indicator: str, freq: str, data: dict[tuple[str, int], str], flags: dict[Any, Any] | None = None
):
    src, ds = dataset.split(".")
    rows = []
    for (geo, idx), v in data.items():
        rows.append(
            {
                "source": src,
                "dataset": ds,
                "indicator": indicator,
                "concept_id": None,
                "iso3": geo,
                "provider_geo": geo,
                "entity_qid": None,
                "period_start": P.start_of(freq, idx),
                "period_end": P.end_of(freq, idx),
                "value": v,
                "value_text": None,
                "unit": "%",
                "flags": json.dumps((flags or {}).get((geo, idx), {})),
                "raw_sha256": RAW,
                "selector": json.dumps({"row": idx}),
            }
        )
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


def monthly(geo: str, n: int, last: int, f=lambda i: 2.0 + math.sin(i / 3.0)) -> dict[tuple[str, int], str]:
    return {(geo, last - n + 1 + i): format(f(i), ".6f") for i in range(n)}


SPECS: SpecSet = parse_specs(
    {
        "schema": "reva-forecast-question-specs/1",
        "version": "t",
        "interval_level": 0.8,
        "panels": {"p": ["FRA", "DEU", "ITA"]},
        "families": [
            {
                "key": "t.food",
                "labels": {"fr": "Inflation alimentaire", "en": "Food inflation"},
                "dataset_key": "atlas.fao.cp",
                "indicator": "23014|6121",
                "frequency": "M",
                "value_rule": "cell",
                "unit": "%",
                "panel": "p",
                "questions": [
                    {"type": "interval", "horizons": [1, 3]},
                    {"type": "binary", "horizons": [3], "threshold_rule": "above_last_observed"},
                ],
                "publication_lag_days": 200,
                "void_after_days": 400,
                "backtest_lag_periods": 2,
            },
            {
                "key": "t.weo",
                "labels": {"fr": "Croissance", "en": "Growth"},
                "dataset_key": "atlas.imf.weo",
                "indicator": "NGDP_RPCH",
                "frequency": "A",
                "value_rule": "cell",
                "unit": "%",
                "panel": "p",
                "questions": [{"type": "quantity", "horizons": [0, 1]}],
                "reference_forecasters": ["forecast.reference_imf_weo"],
                "publication_lag_days": 120,
                "void_after_days": 700,
            },
            {
                "key": "t.wait",
                "labels": {"fr": "x", "en": "x"},
                "dataset_key": None,
                "awaiting": "PRED-HF",
                "frequency": "M",
                "questions": [{"type": "quantity", "horizons": [1]}],
                "publication_lag_days": 5,
                "void_after_days": 100,
            },
        ],
    }
)
ISSUED = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
CYCLE = P.cycle_of(ISSUED.date())
LAST_M = P.index_of("M", date(2026, 3, 1))


def test_periods_roundtrip_and_classify() -> None:
    for f, d, lab in (
        ("M", date(2027, 1, 1), "2027-01"),
        ("Q", date(2027, 4, 1), "2027-Q2"),
        ("A", date(2027, 1, 1), "2027"),
    ):
        i = P.index_of(f, d)
        assert P.label_of(f, i) == lab and P.parse_label(f, lab) == i and P.start_of(f, i) == d
        assert P.classify(P.start_of(f, i), P.end_of(f, i)) == (f, i)
    assert P.classify(date(2020, 4, 1), date(2021, 4, 1)) is None  # fiscal year
    assert P.cycle_of(date(2026, 10, 17)) == "2026-Q4" and P.cycle_anchor("2026-Q4") == date(2026, 10, 1)


def test_repository_specs_load_and_bad_specs_fail() -> None:
    specs = load_specs()
    keys = {f.key for f in specs.families}
    assert {
        "weo.cpi_inflation",
        "weo.gdp_growth",
        "weo.unemployment",
        "fao.food_inflation",
        "eia.brent_monthly",
    } <= keys
    assert all(f.dataset_key or f.awaiting for f in specs.families)
    bad = {"schema": "reva-forecast-question-specs/1", "families": [{"key": "x", "frequency": "M", "dataset_key": "atlas.a.b",
           "indicator": "I", "questions": [{"type": "quantity", "horizons": [13]}], "publication_lag_days": 1,
           "void_after_days": 1}]}  # fmt: skip
    with pytest.raises(ValidationProblem):
        parse_specs(bad)
    bad["families"][0]["questions"] = [{"type": "binary", "horizons": [1]}]
    with pytest.raises(ValidationProblem):
        parse_specs(bad)


def test_yoy_pct_decimal() -> None:
    assert yoy_pct("110", "100") == "10.00000000"
    assert yoy_pct("1", "0") is None


def test_extract_series_splits_projections_and_derives_yoy() -> None:
    weo = SPECS.family("t.weo")
    frame = cells("imf.weo", "NGDP_RPCH", "A", {("FRA", 2024): "1.1", ("FRA", 2025): "0.9", ("FRA", 2026): "1.0"},
                  {("FRA", 2026): {"estimate_kind": "projection"}})  # fmt: skip
    s, _ = extract_series(frame, weo)
    assert [c.idx for c in s["FRA"].observed] == [2024, 2025] and list(s["FRA"].projections) == [2026]
    fam = SPECS.family("t.food")
    yoy = fam.__class__(**{**fam.__dict__, "value_rule": "yoy_pct_from_index", "indicator": "IDX"})
    fr = cells("fao.cp", "IDX", "M", {("FRA", LAST_M - 12): "100", ("FRA", LAST_M): "103"})
    s2, _ = extract_series(fr, yoy)
    assert [(c.idx, c.value) for c in s2["FRA"].observed] == [(LAST_M, "3.00000000")]
    assert len(s2["FRA"].observed[0].refs) == 2  # both index cells cited


def _frames(extra_last: int = 0) -> tuple[dict[str, Any], dict[str, Any]]:
    food = cells("fao.cp", "23014|6121", "M", {**monthly("FRA", 120, LAST_M), **monthly("DEU", 120, LAST_M),
                                                 **monthly("ITA", 30, LAST_M - 40)})  # fmt: skip
    wd = {("FRA", y): format(1.0 + 0.1 * (y % 4), ".1f") for y in range(1990, 2026)}
    wd[("FRA", 2026)] = "1.4"
    wd[("FRA", 2027)] = "1.2"
    weo = cells("imf.weo", "NGDP_RPCH", "A", wd, {("FRA", 2026): {"estimate_kind": "projection"},
                                                  ("FRA", 2027): {"estimate_kind": "projection"}})  # fmt: skip
    pins = {
        "atlas.fao.cp": {
            "dataset_key": "atlas.fao.cp",
            "dataset_id": "d1",
            "artifact_sha256": "f" * 64,
            "recorded_at": "x",
        },
        "atlas.imf.weo": {
            "dataset_key": "atlas.imf.weo",
            "dataset_id": "d2",
            "artifact_sha256": "e" * 64,
            "recorded_at": "x",
        },
    }
    return pins, {"f" * 64: food, "e" * 64: weo}


def test_generate_and_seal_batch_is_deterministic_with_reasons() -> None:
    pins, frames = _frames()
    m1 = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture", already=set())
    m2 = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture", already=set())
    assert manifest_sha256(m1) == manifest_sha256(m2)
    qs = m1["questions"]
    food = [q for q in qs if q["family"] == "t.food"]
    assert {q["geo"] for q in food} == {"FRA", "DEU"}  # ITA stale, listed with its reason
    assert any(s["geo"] == "ITA" and "stale" in s["reason"] for s in m1["generation"]["skipped"])
    assert sorted({q["reference_period"] for q in food}) == ["2026-11", "2027-01"]
    b = next(q for q in food if q["type"] == "binary")
    assert b["threshold"] == b["last_observed"]["value"] and b["threshold_basis"]["period"] == "2026-03"
    assert b["resolution"]["expected_date"] == "2027-08-20"  # period end 2027-02-01 + 200 days
    weo = [q for q in qs if q["family"] == "t.weo"]
    assert sorted(q["reference_period"] for q in weo) == ["2026", "2027"]
    ref = [f for f in m1["forecasts"] if f["model"] == "forecast.reference_imf_weo"]
    assert {f["point"] for f in ref} == {"1.4", "1.2"} and all(f["predictive"] == {"family": "point"} for f in ref)
    assert {p["family"] for p in m1["generation"]["pending_families"]} == {"t.wait"}
    # every frozen forecast parses back into the same predictive
    for f in m1["forecasts"]:
        if f["status"] == "issued":
            predictive_from_record(f["point"], f["predictive"])
    # already issued keys are not issued again in the cycle
    m3 = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture",
                     already={q["question_key"] for q in qs})  # fmt: skip
    assert m3["questions"] == []


def test_resolution_first_later_version_void_and_not_prospective() -> None:
    pins, frames = _frames()
    m = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture", already=set(),
                    families=["t.food"])  # fmt: skip
    fam = SPECS.family("t.food")
    issue_series, _ = extract_series(frames["f" * 64], fam)
    t_nov = P.parse_label("M", "2026-11")
    v1 = cells("fao.cp", "23014|6121", "M", {**monthly("FRA", 120, LAST_M), ("FRA", t_nov): "2.5"})
    v2 = cells("fao.cp", "23014|6121", "M", {**monthly("FRA", 120, LAST_M), ("FRA", t_nov): "9.9"})
    later = {"t.food": [({"dataset_key": "atlas.fao.cp", "dataset_id": "v1", "artifact_sha256": "1" * 64, "recorded_at": "a"},
                         extract_series(v1, fam)[0]),
                        ({"dataset_key": "atlas.fao.cp", "dataset_id": "v2", "artifact_sha256": "2" * 64, "recorded_at": "b"},
                         extract_series(v2, fam)[0])]}  # fmt: skip
    res = resolve_batch(m, set(), {"t.food": issue_series}, later, today=date(2027, 12, 1))
    by = {o["question_id"]: o for o in res["outcomes"]}
    qs = {q["question_id"]: q for q in m["questions"]}
    fra_nov = [o for qid, o in by.items() if qs[qid]["geo"] == "FRA" and qs[qid]["reference_period"] == "2026-11"]
    assert fra_nov and all(o["status"] == "resolved" and o["value"] == "2.5" for o in fra_nov)  # first version wins
    assert fra_nov[0]["source"]["dataset_id"] == "v1"
    deu = [o for qid, o in by.items() if qs[qid]["geo"] == "DEU"]
    assert deu == []  # not void yet on 2027-12-01 (void_after = period end + 400 days)
    res2 = resolve_batch(m, set(), {"t.food": issue_series}, later, today=date(2028, 6, 1))
    assert {o["status"] for o in res2["outcomes"] if qs[o["question_id"]]["geo"] == "DEU"} == {"void"}
    # a value already present in the pinned issue version is never scored
    res3 = resolve_batch(m, set(), {"t.food": extract_series(v1, fam)[0]}, later, today=date(2027, 12, 1))
    assert {o["status"] for o in res3["outcomes"] if o["question_id"] in {x["question_id"] for x in fra_nov}} == {
        "not_prospective"
    }
    # scores: every issued forecast of the resolved questions, typed metrics
    sc = [s for s in res["scores"] if s["question_id"] in {x["question_id"] for x in fra_nov}]
    assert sc and all(s["abs_error"] is not None for s in sc)
    iv = [s for s in sc if s["type"] == "interval"]
    assert iv and all("covered" in s and s["interval_score"] is not None for s in iv)
    # handled questions are skipped
    assert resolve_batch(m, set(by), {"t.food": issue_series}, later, today=date(2027, 12, 1))["outcomes"] == []


def test_scores_crps_brier_calibration() -> None:
    xs = [0.0, 1.0, 3.0]
    brute = sum(abs(x - 1.5) for x in xs) / 3 - 0.5 * sum(abs(a - b) for a in xs for b in xs) / 9
    assert S.crps_empirical(xs, 1.5) == pytest.approx(brute)
    assert S.crps(Predictive("point", 1.0), 2.0) is None
    assert S.brier(0.7, 1) == pytest.approx(0.09) and S.binary_log_score(0.7, 0) == pytest.approx(math.log(0.3))
    q = {"question_id": "q", "type": "binary", "family": "f", "threshold": "2"}
    f = {
        "model": "m",
        "point": "2.5",
        "predictive": {"family": "normal", "mu": "2.5", "sigma": "1"},
        "prob_above": "0.69",
    }
    s = S.score_forecast(q, f, "3")
    assert s["outcome_binary"] == 1 and float(s["brier"]) == pytest.approx(0.31**2)
    assert float(s["pit"]) == pytest.approx(0.6914625, abs=1e-6) and s["log_score"] is not None
    cal = S.calibration([s, {**s, "pit": "0.05", "prob_above": "0.05", "outcome_binary": 0}])
    assert sum(cal["pit_histogram"]["counts"]) == 2 and cal["reliability"]["n"] == 2
    rel = S.relative_to([{"question_id": "a", "abs_error": "1"}], [{"question_id": "a", "abs_error": "2"}], "abs_error")
    assert rel["ratio"] == 0.5 and rel["skill"] == 0.5


def test_forecast_baseline_binary_probability_and_interval() -> None:
    pins, frames = _frames()
    m = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture", already=set(),
                    families=["t.food"])  # fmt: skip
    q = next(x for x in m["questions"] if x["type"] == "binary")
    s, _ = extract_series(frames["f" * 64], SPECS.family("t.food"))
    rec = forecast_baseline(q, "forecast.registry_hist_quantile", s[q["geo"]].history())
    assert rec["status"] == "issued" and 0 < float(rec["prob_above"]) < 1
    assert rec["horizon_periods"] == P.parse_label("M", q["reference_period"]) - LAST_M


def test_backtest_family_reports_relative_metrics() -> None:
    _, frames = _frames()
    fam = SPECS.family("t.food")
    s, _ = extract_series(frames["f" * 64], fam)
    out = backtest_family(fam, s, years=3)
    assert out["horizons"] == [3, 5]
    pers = [r for r in out["results"] if r["model"] == "forecast.registry_persistence"]
    assert pers and pers[0]["mae_vs_persistence"]["ratio"] == pytest.approx(1.0)
    assert all(r["n_scored"] > 0 for r in out["results"])


def test_monthly_mean_of_daily_excludes_the_latest_month() -> None:
    base = SPECS.family("t.food")
    fam = base.__class__(**{**base.__dict__, "value_rule": "monthly_mean_of_daily", "indicator": "D"})
    rows = []
    for d, v in ((date(2026, 8, 3), "1.0"), (date(2026, 8, 4), "2.0"), (date(2026, 9, 1), "4.0")):
        rows.append({"source": "fao", "dataset": "cp", "indicator": "D", "concept_id": None, "iso3": "FRA",
                     "provider_geo": "FRA", "entity_qid": None, "period_start": d,
                     "period_end": date.fromordinal(d.toordinal() + 1), "value": v, "value_text": None, "unit": "x",
                     "flags": "{}", "raw_sha256": RAW, "selector": "{}"})  # fmt: skip
    s, _ = extract_series(pl.DataFrame(rows, schema=ATLAS_SCHEMA), fam)
    assert [(P.label_of("M", c.idx), c.value) for c in s["FRA"].observed] == [("2026-08", "1.50000000")]


def test_fiscal_year_cells_never_enter_a_calendar_question() -> None:
    """FIX4B (audit AUDIT3 M7): WEO cells of a fiscal-year series (latest_actual FY…), even when an older parser
    placed them on calendar years, are dropped with a note; calendar cells are kept."""
    from reva.evaluation.registry.questions import is_fiscal_year_cell

    weo = SPECS.family("t.weo")
    fy = {"latest_actual": "FY2025/26"}
    frame = cells("imf.weo", "NGDP_RPCH", "A", {("FRA", 2024): "1.1", ("FRA", 2025): "13.2", ("FRA", 2026): "12.0"},
                  {("FRA", 2025): fy, ("FRA", 2026): fy})  # fmt: skip
    s, notes = extract_series(frame, weo)
    assert [c.idx for c in s["FRA"].observed] == [2024] and not s["FRA"].projections
    assert {"family": "t.weo", "reason": "fiscal-year cells (never a calendar-year question)", "n": 2} in notes
    assert is_fiscal_year_cell({"period_basis": "fiscal_year_labelled_as_calendar"})
    assert not is_fiscal_year_cell({"latest_actual": "2025"})


# --------------------------------------------------------------------------- FIX7 (QA-PRED X10): prospectivity
def test_fix7_no_question_on_an_ended_period_and_horizon_from_issue() -> None:
    pins, frames = _frames()
    late = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)  # same cycle (2026-Q4) as ISSUED, issued late in it
    m = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=late, origin="test_fixture", already=set())
    food = [q for q in m["questions"] if q["family"] == "t.food"]
    assert sorted({q["reference_period"] for q in food}) == ["2027-01"]  # 2026-11 had ended at issue
    assert any(s.get("period") == "2026-11" and "ended at issue" in s["reason"] for s in m["generation"]["skipped"])
    jan = food[0]
    assert jan["cycle_horizon"] == 3 and jan["issue_horizon"] == 1 and jan["timing"]["kind"] == "forecast"
    weo26 = next(q for q in m["questions"] if q["family"] == "t.weo" and q["reference_period"] == "2026")
    assert weo26["timing"]["kind"] == "near_nowcast" and weo26["timing"]["elapsed_fraction_at_issue"] > 0.9


def test_fix7_resolution_checks_issue_date_and_provider_publication_date() -> None:
    pins, frames = _frames()
    m = build_batch(SPECS, pins, frames, cycle=CYCLE, issued_at=ISSUED, origin="test_fixture", already=set(),
                    families=["t.food"])  # fmt: skip
    fam = SPECS.family("t.food")
    issue_series, _ = extract_series(frames["f" * 64], fam)
    t_nov = P.parse_label("M", "2026-11")
    nov = {q["question_id"] for q in m["questions"] if q["geo"] == "FRA" and q["reference_period"] == "2026-11"}
    pin = {"dataset_key": "atlas.fao.cp", "dataset_id": "v1", "artifact_sha256": "1" * 64, "recorded_at": "a"}
    # (c) the provider's release date of the outcome cell is before the seal -> not prospective
    early = cells("fao.cp", "23014|6121", "M", {**monthly("FRA", 120, LAST_M), ("FRA", t_nov): "2.5"},
                  {("FRA", t_nov): {"release_date": "2026-10-02"}})  # fmt: skip
    res = resolve_batch(m, set(), {"t.food": issue_series}, {"t.food": [(pin, extract_series(early, fam)[0])]},
                        today=date(2027, 12, 1))  # fmt: skip
    got = {o["question_id"]: o for o in res["outcomes"] if o["question_id"] in nov}
    assert got and all(o["status"] == "not_prospective" and "flags.release_date" in o["reason"] for o in got.values())
    assert not [s for s in res["scores"] if s["question_id"] in nov]
    # a later release date resolves normally; a non-ISO 'release' label is ignored, never guessed
    late = cells("fao.cp", "23014|6121", "M", {**monthly("FRA", 120, LAST_M), ("FRA", t_nov): "2.5"},
                 {("FRA", t_nov): {"release_date": "2026-12-04", "release": "Dec"}})  # fmt: skip
    res = resolve_batch(m, set(), {"t.food": issue_series}, {"t.food": [(pin, extract_series(late, fam)[0])]},
                        today=date(2027, 12, 1))  # fmt: skip
    assert {o["status"] for o in res["outcomes"] if o["question_id"] in nov} == {"resolved"}
    # (a) a batch sealed after the period ended (older code) -> not prospective, whatever the tables say
    old = {**m, "issued_at": "2026-12-15T00:00:00+00:00"}
    res = resolve_batch(old, set(), {"t.food": issue_series}, {"t.food": [(pin, extract_series(late, fam)[0])]},
                        today=date(2027, 12, 1))  # fmt: skip
    assert {o["status"] for o in res["outcomes"] if o["question_id"] in nov} == {"not_prospective"}
