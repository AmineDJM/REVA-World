"""PRED-HEALTH (V6): health surveillance connectors (WHO FluNet, CDC forecast hubs, ECDC ERVISS), quantile
forecasters and WIS, the health registry's pure core (periods, positivity, vintages, questions, back-tests)."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_cdc_resp_hubs as hubs
from reva.adapters.connectors import atlas_ecdc_erviss as erviss
from reva.adapters.connectors import atlas_who_flunet as flunet
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.evaluation.health_registry import core as C
from reva.kernel.errors import SourceResponseError
from reva.models.forecasting import health_quantile as HQ

FX = Path(__file__).parents[1] / "fixtures" / "source"
RAW = "0" * 64


def _fx(conn: str) -> dict[str, bytes]:
    man = json.loads((FX / conn / "manifest.json").read_text())
    for e in man:
        assert e["licence"] and e["fixture_note"]
    return {e["url"]: (FX / conn / e["file"]).read_bytes() for e in man}


def _run(conn: str, obj: Any) -> pl.DataFrame:
    by = _fx(conn)
    queue, bodies, raws = list(obj.items()), {}, {}
    while queue:
        it = queue.pop(0)
        body = by[it.url]
        bodies[it.key], raws[it.key] = body, RAW
        obj.check(it, body)
        queue.extend(obj.follow_ups(it, body))
    return obj.atlas_frame(bodies, raws)[0]


@pytest.fixture(scope="module")
def frames() -> dict[str, pl.DataFrame]:
    out = {"atlas.who.flunet": _run("atlas.who_flunet", flunet.WhoFluNetConnector(flunet.FIXTURE_COUNTRIES))}
    for hub in (hubs.FLUSIGHT, hubs.COVIDHUB):
        for ds, obj in hubs._datasets(hub):
            out[f"atlas.cdc.{ds}"] = _run(f"atlas.cdc_{hub.key}", obj)
    out["atlas.ecdc.erviss_ili"] = _run("atlas.ecdc_erviss", erviss.IliDataset())
    out["atlas.ecdc.erviss_sentinel"] = _run("atlas.ecdc_erviss", erviss.SentinelDataset())
    return out


# --------------------------------------------------------------------------- connectors


def test_flunet_rows_are_verbatim_with_typed_missing(frames: dict[str, pl.DataFrame]) -> None:
    f = frames["atlas.who.flunet"]
    validate_atlas_frame(f, source="who", dataset="flunet", raw_sha256s=[RAW], geo_scheme="iso3")
    assert set(f["provider_geo"].unique()) == {"FRA", "AUS"}
    assert (f["period_end"] - f["period_start"]).dt.total_days().unique().to_list() == [7]
    assert f["period_start"].dt.weekday().unique().to_list() == [1]  # ISO weeks start on Monday
    nulls = f.filter(pl.col("value").is_null())
    assert nulls.height > 0 and all(json.loads(x)["missing"] == "not_reported" for x in nulls["flags"].to_list()[:50])
    assert set(f["indicator"].str.extract(r"origin=(\w+)").unique().to_list()) <= set(flunet.ORIGINS)


def test_flunet_rejects_a_week_start_that_is_not_the_iso_week() -> None:
    doc = {"value": [{c: None for c in flunet.SELECT}]}
    doc["value"][0].update(
        COUNTRY_CODE="FRA", ORIGIN_SOURCE="SENTINEL", ISO_WEEKSTARTDATE="2026-09-22", ISO_YEAR=2026, ISO_WEEK=39
    )
    with pytest.raises(SourceResponseError, match="not ISO"):
        flunet.parse_country(json.dumps(doc).encode(), "FRA")


def test_hub_listing_refuses_truncation_and_dtds() -> None:
    ok = (
        b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>false</IsTruncated><Contents><Key>a/b.parquet</Key><Size>12</Size></Contents></ListBucketResult>"
    )
    assert hubs.parse_listing(ok) == [("a/b.parquet", 12)]
    with pytest.raises(SourceResponseError, match="truncated"):
        hubs.parse_listing(ok.replace(b"<IsTruncated>false", b"<IsTruncated>true"))
    with pytest.raises(SourceResponseError, match="DTD"):
        hubs.parse_listing(b'<!DOCTYPE x [<!ENTITY a "b">]>' + ok)


def test_hub_tables_target_vintages_ensemble(frames: dict[str, pl.DataFrame]) -> None:
    t = frames["atlas.cdc.flusight_target"]
    validate_atlas_frame(t, source="cdc", dataset="flusight_target", raw_sha256s=[RAW], geo_scheme="cdc_hub")
    assert t["period_start"].dt.weekday().unique().to_list() == [7]  # epiweeks start on Sunday
    v = frames["atlas.cdc.flusight_vintages"]
    per = v.group_by("indicator").agg(pl.len().alias("n"), pl.col("period_start").min().alias("first"))
    first = per.sort("indicator").row(0, named=True)
    later = per.sort("indicator").row(-1, named=True)
    assert first["n"] > later["n"]  # earliest vintage keeps its full history, later ones a 52-week window
    assert later["first"] > date(2024, 11, 1)
    e = frames["atlas.cdc.flusight_ensemble"]
    flags = [json.loads(x) for x in e["flags"].unique().to_list()]
    assert {f["estimate_kind"] for f in flags} == {"projection"} and {f["horizon"] for f in flags} == {0, 1, 2, 3}
    assert {f["model_id"] for f in flags} == {"FluSight-ensemble"}
    c = frames["atlas.cdc.covidhub_vintages"]
    assert c["indicator"].str.starts_with("nhsn.wk_inc_covid_hosp|vintage=").all()


def test_hub_ensemble_parser_checks_reference_dates() -> None:
    body = _fx("atlas.cdc_flusight")[
        "https://cdcepi-flusight-forecast-hub.s3.amazonaws.com/model-output/FluSight-ensemble/2025-12-06-FluSight-ensemble.parquet"
    ]
    rows = hubs.parse_ensemble(body, hubs.FLUSIGHT, date(2025, 12, 6))
    assert rows and all(r["horizon"] in (0, 1, 2, 3) for r in rows)
    with pytest.raises(SourceResponseError, match="reference_date"):
        hubs.parse_ensemble(body, hubs.FLUSIGHT, date(2025, 12, 13))


def test_erviss_keeps_only_declared_rows_and_no_iso3(frames: dict[str, pl.DataFrame]) -> None:
    f = frames["atlas.ecdc.erviss_sentinel"]
    validate_atlas_frame(f, source="ecdc", dataset="erviss_sentinel", raw_sha256s=[RAW])
    assert set(f["indicator"].unique()) <= {
        f"influenza.{k}|survtype=primary care sentinel|age=total" for k in erviss.SENTINEL_INDICATORS
    }
    assert f["iso3"].null_count() == f.height  # names are never mapped to codes
    assert all(json.loads(x)["licence_review"] for x in f["flags"].unique().to_list())
    assert erviss.iso_week_start("2026-W41") == date(2026, 10, 5)
    with pytest.raises(SourceResponseError):
        erviss.iso_week_start("2026-W54")


# --------------------------------------------------------------------------- numerics


def test_wis_matches_the_interval_definition() -> None:
    levels, q = [0.05, 0.25, 0.5, 0.75, 0.95], [0.0, 1.0, 2.0, 3.0, 4.0]
    for y in (2.0, 3.5, -1.0, 7.0):
        k = 2
        direct = 0.5 * abs(y - 2.0)
        for alpha, lo, hi in ((0.1, 0.0, 4.0), (0.5, 1.0, 3.0)):
            iscore = (hi - lo) + 2 / alpha * max(lo - y, 0) + 2 / alpha * max(y - hi, 0)
            direct += alpha / 2 * iscore
        assert HQ.wis(levels, q, y) == pytest.approx(direct / (k + 0.5))


def test_baselines_and_candidates() -> None:
    h = [(i, 100 + 40 * math.sin(2 * math.pi * i / 52) + (i % 7)) for i in range(400)]
    p = HQ.persistence(h, 3, freq="W")
    assert isinstance(p, HQ.QuantileForecast) and p.median == h[-1][1] and len(p.values) == 23
    a1, a2 = HQ.log_ar(h, 4), HQ.log_ar(h, 4)
    assert a1 == a2  # deterministic bootstrap
    c = HQ.climatology(h, 410, lambda i: i % 52 + 1, lambda i: i // 52)
    assert isinstance(c, HQ.QuantileForecast)
    assert isinstance(HQ.climatology(h[-60:], 410, lambda i: i % 52 + 1, lambda i: i // 52), HQ.Abstain)
    b = HQ.blend({"p": p, "c": c})
    assert isinstance(b, HQ.QuantileForecast) and b.median == pytest.approx((p.median + c.median) / 2)
    bounded = HQ.persistence([(i, 99.0 + (i % 3)) for i in range(60)], 1, freq="W", upper=100.0)
    assert isinstance(bounded, HQ.QuantileForecast) and max(bounded.values) <= 100.0
    yearly = [(2000 + i, 1000.0 * 1.02**i) for i in range(24)]
    d = HQ.log_drift(yearly, 2)
    assert isinstance(d, HQ.QuantileForecast) and d.median == pytest.approx(yearly[-1][1] * 1.02**2, rel=1e-6)


def test_dm_test_sign_and_power() -> None:
    worse = HQ.dm_test([1.0 + 0.1 * math.sin(i) for i in range(40)], 3)
    assert worse["status"] == "ok" and worse["mean_diff"] > 0 and worse["p_value"] < 0.01
    assert HQ.dm_test([0.1, -0.1, 0.0], 3)["status"] == "too_few_origins"


# --------------------------------------------------------------------------- core


def test_week_indices_and_rounds() -> None:
    for ws, d in (("monday", date(2026, 10, 12)), ("sunday", date(2026, 10, 11))):
        i = C.week_idx(d, ws)
        assert C.week_start_of(i, ws) == d and C.week_start_of(i + 1, ws) == d + timedelta(days=7)
    fam = C.cached_specs().family("nhsn.flu_hosp")
    rnd, idx = C.round_of(fam, date(2026, 10, 14))  # a Wednesday: the hub round's reference date is that Saturday
    assert rnd == "2026-10-17" and C.label_of(fam, idx) == "EW-2026-10-17"
    assert C.round_of(fam, date(2026, 10, 17))[0] == "2026-10-17"
    assert C.round_of(fam, date(2026, 10, 18))[0] == "2026-10-24"
    iso = C.cached_specs().family("flunet.flu_positivity")
    assert C.round_of(iso, date(2026, 10, 17))[0] == "2026-W42"


def test_flunet_positivity_rule() -> None:
    D = Decimal
    v, d = C.positivity(
        {
            "SENTINEL": {"spec_processed_nb": D(40), "inf_all": None, "inf_negative": D(40)},  # all negative -> 0
            "NONSENTINEL": {"spec_processed_nb": D(60), "inf_all": D(15), "inf_negative": D(45)},
            "NOTDEFINED": {"spec_processed_nb": D(50), "inf_all": None, "inf_negative": D(20)},  # unusable
        },
        30,
    )
    assert v == "15.000000" and d["inf_all_zero_from_negatives"] == ["SENTINEL"] and "NOTDEFINED" not in d["origins"]
    assert C.positivity({"SENTINEL": {"spec_processed_nb": D(10), "inf_all": D(1)}}, 30)[0] is None


def test_history_as_of_uses_only_vintages_published_by_the_cutoff() -> None:
    v = {
        date(2025, 11, 29): {"US": {10: 1.0, 11: 2.0}},
        date(2025, 12, 6): {"US": {11: 2.5, 12: 3.0}},
        date(2025, 12, 13): {"US": {12: 3.5, 13: 4.0}},
    }
    assert C.history_as_of(v, date(2025, 12, 6)) == {"US": [(10, 1.0), (11, 2.5), (12, 3.0)]}


def test_as_of_backtest_against_the_hub_ensemble(frames: dict[str, pl.DataFrame]) -> None:
    fam = C.cached_specs().family("nhsn.flu_hosp")
    series, _labels, _ = C.extract_series(frames["atlas.cdc.flusight_target"], fam)
    vint = C.extract_vintages(frames["atlas.cdc.flusight_vintages"], fam)
    ens = C.extract_ensemble(frames["atlas.cdc.flusight_ensemble"], fam)
    tasks, skipped = C.as_of_tasks(fam, series, vint, ens)
    assert not skipped and {t.origin for t in tasks} == {"2025-12-06", "2025-12-13"}
    t0 = next(t for t in tasks if t.origin == "2025-12-06" and t.series == "US")
    # the round of 2025-12-06 sees the vintage whose last epiweek is 2025-11-29, nothing later
    assert C.label_of(fam, t0.history[-1][0]) == "EW-2025-11-29" and t0.reference is not None
    res = C.run_backtest(fam, tasks, alpha=0.05)
    assert "forecast.reference_flusight_ensemble" in res["models"]
    assert res["pairs"]["health.persistence|forecast.reference_flusight_ensemble"]["dm"]["status"] == "too_few_origins"
    assert res["winners"] == []  # nothing is a winner without a significant test


def test_winner_rule_needs_significance() -> None:
    fam = C.cached_specs().family("erviss.ili_rate")
    hist = [(i, 50 + 30 * math.sin(2 * math.pi * i / 52)) for i in range(5000, 5300)]
    tasks = [
        C.Task(f"o{o}", "X", 1, o + 1, [x for x in hist if x[0] <= o], dict(hist)[o + 1]) for o in range(5200, 5290)
    ]
    res = C.run_backtest(fam, tasks, alpha=0.05)
    for w in res["winners"]:
        for b in fam.baselines:
            p = res["pairs"][f"{w}|{b}"]
            assert p["relative_wis"] < 1 and p["dm"]["p_value"] < 0.05 / len(fam.baselines)


def test_questions_target_unobserved_periods_and_declare_no_disease_link(frames: dict[str, pl.DataFrame]) -> None:
    specs = C.cached_specs()
    for key, ds in (("flunet.flu_positivity", "atlas.who.flunet"), ("nhsn.covid_hosp", "atlas.cdc.covidhub_target")):
        fam = specs.family(key)
        series, labels, _ = C.extract_series(frames[ds], fam)
        qs, _ = C.generate_questions(fam, series, labels, today=date(2026, 10, 17), source_pin={"dataset_key": ds})
        assert qs
        for q in qs:
            assert q["steps_from_last_observed"] >= 1 and q["disease_link"] is None and q["disease_link_reason"]
            assert q["question_id"] == C.question_id(q) and len(q["levels"]) == 23
    for fam in specs.families:  # every family says why it has no disease link
        assert fam.disease_link is None and fam.disease_link_reason


def test_ghe_family_reads_the_v4health_table_shape() -> None:
    fam = C.cached_specs().family("ghe.deaths")
    rows = []
    for y in range(2000, 2024):
        for cause, base in (("0", 500000.0), ("10", 60000.0)):
            rows.append(
                {
                    "source": "who", "dataset": "ghe", "indicator": f"ghe.deaths|sex=TOTAL|age=TOTAL|cause={cause}",
                    "concept_id": None, "iso3": None, "provider_geo": "FRA", "entity_qid": None,
                    "period_start": date(y, 1, 1), "period_end": date(y + 1, 1, 1), "value": f"{base * 1.01 ** (y - 2000):.2f}",
                    "value_text": None, "unit": "deaths", "flags": "{}", "raw_sha256": RAW, "selector": '{"i":1}',
                }
            )  # fmt: skip
    series, _, _ = C.extract_series(pl.DataFrame(rows, schema=ATLAS_SCHEMA), fam)
    assert set(series) == {"FRA|cause=0", "FRA|cause=10"}
    qs, _ = C.generate_questions(fam, series, {}, today=date(2026, 10, 17), source_pin={})
    assert sorted({q["reference_period"] for q in qs}) == ["2024", "2025", "2026"]
    res = C.run_backtest(fam, C.final_vintage_tasks(fam, series), alpha=0.05)
    assert res["models"]["health.log_drift"]["mean_wis"] < res["models"]["health.persistence"]["mean_wis"]
