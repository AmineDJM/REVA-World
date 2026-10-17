"""Finance families of the forecast registry (V6 PRED-FIN): specs, month-end / spread / meeting questions,
finance forecasters, sealing with input pins, meeting resolution and probability-only scores."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from reva.atlas.tables import ATLAS_SCHEMA
from reva.evaluation.registry import finance as FN
from reva.evaluation.registry import periods as P
from reva.evaluation.registry import scores as S
from reva.evaluation.registry.questions import (
    binary_outcome,
    extract_series,
    generate_meeting_questions,
    resolution_value,
)
from reva.evaluation.registry.service import build_batch
from reva.evaluation.registry.specs import SpecSet, load_specs, parse_specs
from reva.kernel.errors import ValidationProblem
from reva.models.forecasting.finance_models import DISCLAIMER

RAW = "b" * 64


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def daily_frame(source: str, dataset: str, series: dict[tuple[str, str], list[tuple[date, str]]]) -> pl.DataFrame:
    rows = []
    for (indicator, geo), vals in series.items():
        for d, v in vals:
            rows.append(
                {
                    "source": source,
                    "dataset": dataset,
                    "indicator": indicator,
                    "concept_id": None,
                    "iso3": None,
                    "provider_geo": geo,
                    "entity_qid": None,
                    "period_start": d,
                    "period_end": d + timedelta(days=1),
                    "value": v,
                    "value_text": None,
                    "unit": "x",
                    "flags": json.dumps({"estimate_kind": "observation"}),
                    "raw_sha256": RAW,
                    "selector": json.dumps({"day": d.isoformat(), "indicator": indicator}),
                }
            )
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


def calendar_frame(days: list[date], indicator: str = "POLICY_DECISION|body=ECB_GC", geo: str = "U2") -> pl.DataFrame:
    rows = [
        {
            "source": "cbcal",
            "dataset": "meetings",
            "indicator": indicator,
            "concept_id": None,
            "iso3": None,
            "provider_geo": geo,
            "entity_qid": None,
            "period_start": d,
            "period_end": d + timedelta(days=1),
            "value": None,
            "value_text": f"meeting {d.isoformat()} (Day 2), followed by press conference",
            "unit": "scheduled decision day",
            "flags": json.dumps({"estimate_kind": "announced_schedule"}),
            "raw_sha256": RAW,
            "selector": json.dumps({"entry": i}),
        }
        for i, d in enumerate(days)
    ]
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


FX = "EUR_REF|currency=USD"
DFR = "FM.D.U2.EUR.4F.KR.DFR.LEV"


def fx_values(days: list[date]) -> list[tuple[date, str]]:
    return [(d, format(1.10 + 0.05 * math.sin(i / 37.0) + 0.002 * math.cos(i * 1.7), ".4f")) for i, d in enumerate(days)]


def specs_subset(keys: tuple[str, ...]) -> SpecSet:
    full = load_specs()
    return SpecSet(full.version, full.interval_level, tuple(f for f in full.families if f.key in keys), full.sha256)


# --------------------------------------------------------------------------- specs


def test_finance_families_parse_and_validate() -> None:
    s = load_specs()
    fin = {f.key: f for f in s.families if f.finance}
    for k in ("fin.fx_eurusd_month_end", "fin.ust_2s10s_month_end", "fin.fomc_hike", "fin.ecb_dfr_cut",
              "fin.boe_bank_rate_hike", "eia.brent_spot_monthly_mean"):  # fmt: skip
        assert k in fin
    assert fin["fin.ecb_dfr_cut"].finance["direction"] == "down"  # type: ignore[index]
    assert fin["fin.ecb_dfr_cut"].finance["calendar"]["indicator"] == "POLICY_DECISION|body=ECB_GC"  # type: ignore[index]
    assert not fin["fin.fomc_hike"].runs_generic_baselines and fin["fin.fx_eurusd_month_end"].runs_generic_baselines
    assert fin["fin.ust_2s10s_month_end"].indicator_minus == "UST_PAR|tenor=2 Yr"
    # candidates are never issued unless a back-test admitted them: none is listed among the forecasters today
    for f in fin.values():
        assert not set(f.finance["forecasters"]) & {"fin.garch11", "fin.ns_dl", "fin.ppp_reversion",  # type: ignore[index]
                                                    "fin.taylor_logit"}  # fmt: skip

    base = {
        "key": "x",
        "dataset_key": "atlas.ecb.key_rates",
        "indicator": DFR,
        "frequency": "M",
        "geos": [{"provider_geo": "U2", "key": "ECB", "label_fr": "a", "label_en": "a"}],
        "publication_lag_days": 3,
        "void_after_days": 120,
    }
    meeting = {**base, "value_rule": "meeting_change", "threshold": "0",
               "questions": [{"type": "binary", "horizons": [1], "threshold_rule": "above_fixed"}]}  # fmt: skip
    bad = [
        {**meeting},  # meeting without finance block
        {**meeting, "finance": {"kind": "meeting", "forecasters": ["fin.meeting_base_rate"], "direction": "up",
                                "inputs": {"history": {"dataset_key": "atlas.ecb.key_rates", "indicator": DFR,
                                                       "provider_geo": "U2"}}}},  # no calendar  # fmt: skip
        {**base, "value_rule": "month_end_spread_of_daily", "questions": [{"type": "quantity", "horizons": [1]}]},
        {**base, "value_rule": "month_end_of_daily", "questions": [{"type": "quantity", "horizons": [1]}],
         "finance": {"kind": "fx", "scale": "log", "forecasters": ["fin.oracle"]}},  # fmt: skip
        {**meeting, "threshold": None},
    ]
    for fam in bad:
        with pytest.raises(ValidationProblem):
            parse_specs({"schema": "reva-forecast-question-specs/1", "version": "t", "families": [fam]})


# --------------------------------------------------------------------------- month-end and spread series


def test_month_end_and_spread_series() -> None:
    days = weekdays(date(2026, 1, 1), date(2026, 3, 10))
    frame = daily_frame("ecb", "fx_ref", {(FX, "U2"): fx_values(days)})
    fam = load_specs().family("fin.fx_eurusd_month_end")
    s = extract_series(frame, fam)[0]["EUR-USD"]
    assert [P.label_of("M", c.idx) for c in s.observed] == ["2026-01", "2026-02"]  # March incomplete: excluded
    jan_last = max(d for d in days if d.month == 1)
    assert s.observed[0].value == dict(fx_values(days))[jan_last] and s.observed[0].refs[0]["day"] == jan_last.isoformat()

    ten = [(d, "4.25") for d in days]
    two = [(d, "3.90") for d in days if d != date(2026, 2, 27)]  # 2 Yr missing on the last weekday of February
    frame = daily_frame("ust", "par_yield", {("UST_PAR|tenor=10 Yr", "US"): ten, ("UST_PAR|tenor=2 Yr", "US"): two})
    spread = extract_series(frame, load_specs().family("fin.ust_2s10s_month_end"))[0]["UST-2S10S"]
    feb = spread.observed[1]
    assert feb.value == "0.35" and feb.refs[0]["day"] == "2026-02-26"  # last day both are published
    assert len(feb.refs[0]["cells"]) == 2


# --------------------------------------------------------------------------- meetings


def dfr_values(start: date, end: date, changes: dict[date, str], first: str = "2.00") -> list[tuple[date, str]]:
    out, level, d = [], first, start
    while d <= end:
        level = changes.get(d, level)
        out.append((d, level))
        d += timedelta(days=1)
    return out


def test_meeting_questions_resolution_and_outcome() -> None:
    hike = load_specs().family("fin.ecb_dfr_hike")
    cut = load_specs().family("fin.ecb_dfr_cut")
    issue_frame = daily_frame("ecb", "key_rates", {(DFR, "U2"): dfr_values(date(2020, 1, 1), date(2026, 10, 17), {})})
    cal = FN.meeting_days(calendar_frame([date(2026, 9, 10), date(2026, 10, 29), date(2026, 12, 17)]), hike.finance["calendar"])  # type: ignore[index]  # fmt: skip
    pin = {"dataset_key": "atlas.ecb.key_rates", "dataset_id": "d", "artifact_sha256": "c" * 64, "recorded_at": "t"}
    qs, skipped = generate_meeting_questions(hike, extract_series(issue_frame, hike)[0], cal, cycle="2026-Q4",
                                             today=date(2026, 10, 17), source_pin=pin, calendar_pin=pin)  # fmt: skip
    assert [q["event"]["decision_day"] for q in qs] == ["2026-10-29", "2026-12-17"] and not skipped
    q = qs[0]
    assert q["resolution"]["level_day"] == "2026-11-05" and q["reference_period"] == "2026-11"
    assert q["threshold"] == "0" and q["event"]["basis_value"] == "2.00" and q["disclaimer"]["en"] == DISCLAIMER
    assert "strictly above" in q["text_en"] and "2026-10-29" in q["text_en"]
    # not resolvable from the issue version (no day on or after the level day)
    assert resolution_value(q, extract_series(issue_frame, hike)[0]) is None
    later = daily_frame("ecb", "key_rates", {(DFR, "U2"): dfr_values(date(2020, 1, 1), date(2026, 11, 20),
                                                                         {date(2026, 11, 4): "2.25"})})  # fmt: skip
    cell = resolution_value(q, extract_series(later, hike)[0])
    assert cell is not None and cell.value == "0.25" and binary_outcome(q, float(cell.value)) == 1
    qc = generate_meeting_questions(cut, extract_series(issue_frame, cut)[0], cal, cycle="2026-Q4",
                                    today=date(2026, 10, 17), source_pin=pin, calendar_pin=pin)[0][0]  # fmt: skip
    assert "strictly below" in qc["text_en"]
    cc = resolution_value(qc, extract_series(later, cut)[0])
    assert cc is not None and cc.value == "-0.25" and binary_outcome(qc, float(cc.value)) == 0
    # a calendar with a single upcoming meeting: the second question is skipped with its reason
    _, sk = generate_meeting_questions(hike, extract_series(issue_frame, hike)[0], cal[:2], cycle="2026-Q4",
                                       today=date(2026, 10, 17), source_pin=pin, calendar_pin=pin)  # fmt: skip
    assert sk and sk[0]["reason"] == "meeting not in the published calendar"


def test_probability_only_record_scores() -> None:
    q = {"question_id": "fq_x", "type": "binary", "family": "fin.ecb_dfr_hike", "threshold": "0"}
    f = {"model": "fin.meeting_base_rate", "point": None, "predictive": None, "prob_above": "0.2"}
    sc = S.score_forecast(q, f, "0.25")
    assert sc["abs_error"] is None and sc["crps"] is None and sc["outcome_binary"] == 1
    assert float(sc["brier"]) == pytest.approx(0.64)


# --------------------------------------------------------------------------- sealing with inputs


def test_build_batch_finance_families_pins_inputs_and_calendar() -> None:
    specs = specs_subset(("fin.fx_eurusd_month_end", "fin.ecb_dfr_hike"))
    days = weekdays(date(2023, 1, 2), date(2026, 10, 16))
    fx = daily_frame("ecb", "fx_ref", {(FX, "U2"): fx_values(days)})
    rates = daily_frame("bis", "cbpol_d", {("CBPOL", "XM"): [(d, "2.00") for d in days],
                                           ("CBPOL", "US"): [(d, "4.25") for d in days]})  # fmt: skip
    dfr = daily_frame("ecb", "key_rates", {(DFR, "U2"): dfr_values(date(1999, 1, 1), date(2026, 10, 17),
        {date(2005, 12, 6): "1.25", date(2008, 10, 15): "2.75", date(2011, 4, 13): "0.75", date(2014, 6, 11): "-0.10",
         date(2019, 9, 18): "-0.50", date(2022, 7, 27): "0.00", date(2023, 9, 20): "4.00", date(2024, 6, 12): "3.75",
         date(2025, 6, 11): "2.00"}, first="2.00")})  # fmt: skip
    cal = calendar_frame([date(2026, 10, 29), date(2026, 12, 17)])
    frames_by_key = {"atlas.ecb.fx_ref": fx, "atlas.bis.cbpol_d": rates, "atlas.ecb.key_rates": dfr,
                     "atlas.cbcal.meetings": cal}  # fmt: skip
    pins: dict[str, Any] = {}
    frames: dict[str, pl.DataFrame] = {}
    for i, (k, fr) in enumerate(frames_by_key.items()):
        sha = f"{i:x}" * 64
        pins[k] = {"dataset_key": k, "dataset_id": f"id{i}", "artifact_sha256": sha, "recorded_at": "2026-10-17T00:00"}
        frames[sha] = fr
    pins["atlas.eurostat.hicp_m"] = pins["atlas.imf.cpi_m"] = None  # inputs not registered: their models abstain
    m = build_batch(specs, pins, frames, cycle="2026-Q4", issued_at=datetime(2026, 10, 17, 12, tzinfo=UTC),
                    origin="test_fixture", already=set())  # fmt: skip
    fams = {q["family"] for q in m["questions"]}
    assert fams == {"fin.fx_eurusd_month_end", "fin.ecb_dfr_hike"}
    assert all(q.get("disclaimer") for q in m["questions"])
    assert set(m["data"]["tables"]) == {"atlas.ecb.fx_ref", "atlas.bis.cbpol_d", "atlas.ecb.key_rates",
                                        "atlas.cbcal.meetings"}  # fmt: skip
    by = {}
    for f in m["forecasts"]:
        by.setdefault((f["question_id"], f["model"]), f)
    fxq = next(q for q in m["questions"] if q["family"] == "fin.fx_eurusd_month_end" and q["type"] == "quantity"
               and q["cycle_horizon"] == 12)  # fmt: skip
    rw = by[(fxq["question_id"], "fin.rw_histvol")]
    fwd = by[(fxq["question_id"], "fin.fx_forward_cip")]
    assert rw["status"] == "issued" and fwd["status"] == "issued"
    assert float(fwd["point"]) > float(rw["point"])  # USD rate above the euro rate: forward above spot
    assert (fxq["question_id"], "forecast.registry_persistence") in by  # generic baselines still run
    mq = [q for q in m["questions"] if q["family"] == "fin.ecb_dfr_hike"]
    assert len(mq) == 2 and mq[0]["training"]["last_day"] == "2026-10-17"
    br = by[(mq[0]["question_id"], "fin.meeting_base_rate")]
    assert br["status"] == "issued" and br["point"] is None and 0 < float(br["prob_above"]) < 1
    assert (mq[0]["question_id"], "forecast.registry_persistence") not in by  # no generic baseline on meetings
    assert {f["model"] for f in m["forecasts"] if f["question_id"] == mq[0]["question_id"]} == {
        "fin.meeting_base_rate", "fin.meeting_momentum"}  # fmt: skip
