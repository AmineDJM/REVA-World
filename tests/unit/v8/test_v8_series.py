"""V8 series service (V8MONDE): indicator parsing, periods, transform rules and the map/place/multi queries.

No database: a synthetic atlas table is written to a filesystem store and pinned by an in-memory snapshot (same
pattern as tests/unit/test_atlas_query_periods.py).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.atlas.catalog import Catalog, Concept, Series
from reva.atlas.query import AtlasQuery
from reva.atlas.snapshot import AtlasSnapshot
from reva.atlas.tables import ATLAS_SCHEMA, to_arrow
from reva.kernel.errors import ValidationProblem
from reva.simulation.datasets import parquet_bytes
from reva.v8.series import service as svc
from reva.v8.series.core import (
    default_precision,
    parse_indicator,
    parse_period,
    period_code,
    period_label_fr,
    scale_hint,
    split_series_key,
    transform_rules,
    unit_kind,
)
from reva.v8.series.index import SeriesIndex

RAW = "c" * 64


# --------------------------------------------------------------------------- pure helpers


def test_parse_indicator_measure_and_filters() -> None:
    assert parse_indicator("NY.GDP.PCAP.CD") == ("NY.GDP.PCAP.CD", ())
    assert parse_indicator("UNE|sex=SEX_F|classif1=AGE_Y15-24") == ("UNE", (("sex", "SEX_F"), ("classif1", "AGE_Y15-24")))
    assert parse_indicator("2511|5123") == ("2511|5123", ())  # FAO item|element: both codes are the measure
    assert parse_indicator("a|k=v|tail") == ("a", (("k", "v"), ("_2", "tail")))
    assert split_series_key("wb.wdi_econ.NY.GDP.PCAP.CD") == ("wb", "wdi_econ", "NY.GDP.PCAP.CD")
    assert split_series_key("nodots") is None


def test_period_codes_round_trip() -> None:
    assert period_code("year", "2020-01-01", 2020) == "2020"
    assert period_code("month", "2020-03-01") == "2020-03"
    assert period_code("quarter", "2020-07-01") == "2020-Q3"
    assert period_code("semester", "2020-07-01") == "2020-S2"
    assert period_code("quarter", "2020-02-01") == "2020-02-01"  # off-boundary quarter keeps its start date
    assert parse_period("quarter", "2020-Q3").start == date(2020, 7, 1)  # type: ignore[union-attr]
    assert parse_period("month", "2020-03").start == date(2020, 3, 1)  # type: ignore[union-attr]
    assert parse_period("year", "-500").year == -500  # type: ignore[union-attr]
    assert parse_period("year", "2020-Q1") is None
    assert period_label_fr("month", "2020-03") == "mars 2020"
    assert period_label_fr("quarter", "2020-Q1") == "T1 2020"


def test_default_precision() -> None:
    assert default_precision({"year": 100, "month": 0}) == "year"
    assert default_precision({"year": 0, "month": 300}) == "month"
    assert default_precision({"year": 10, "quarter": 400}) == "quarter"
    assert default_precision({}) is None


def test_unit_kinds_and_transform_rules() -> None:
    assert unit_kind("% of GDP") == "percent"
    assert unit_kind("Current US$") == "extensive"
    assert unit_kind("persons") == "extensive"
    assert unit_kind("index 2015=100") == "intensive"
    assert unit_kind("years") == "intensive"
    r = transform_rules(unit="Current US$", precision="year", numeric=True, min_value=1.0, countries=True)
    assert r["percap"].ok and r["index"].ok and r["yoy"].ok
    # already per capita (code or label): no second division
    r = transform_rules(
        unit="Current US$", precision="year", numeric=True, min_value=1.0, countries=True, context="NY.GDP.PCAP.CD"
    )
    assert not r["percap"].ok and "population" in (r["percap"].reason or "")
    r = transform_rules(unit="% change", precision="year", numeric=True, min_value=-2.0, countries=True)
    assert not r["index"].ok and not r["yoy"].ok and not r["percap"].ok
    r = transform_rules(unit="tonnes", precision="month", numeric=True, min_value=3.0, countries=True)
    assert r["yoy"].ok and not r["percap"].ok  # population is annual only
    r = transform_rules(unit="t", precision="year", numeric=False, min_value=None, countries=True)
    assert not any(x.ok for x in r.values())


def test_scale_hint() -> None:
    assert scale_hint([-3, -1, 0.5, 2, 4, 5, 6, 7]) == "diverging"
    assert scale_hint([1, 2, 3]) == "linear"
    skewed = [200, 300, 500, 800, 1000, 2000, 3000, 5000, 8000, 20000, 60000, 150000]
    assert scale_hint(skewed) == "log"
    uniform = [float(x) for x in range(10, 100, 3)]
    assert scale_hint(uniform) == "linear"


# --------------------------------------------------------------------------- synthetic snapshot


def _row(ind: str, geo: str, iso3: str | None, start: date, end: date, value: str | None, **flags: object) -> dict:
    return {
        "source": "src",
        "dataset": "ds",
        "indicator": ind,
        "concept_id": None,
        "iso3": iso3,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": start,
        "period_end": end,
        "value": value,
        "value_text": None,
        "unit": "Current US$" if ind.startswith("GDP") else "u",
        "flags": json.dumps(flags, sort_keys=True),
        "raw_sha256": RAW,
        "selector": json.dumps({"geo": geo, "start": start.isoformat()}),
    }


def _y(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


@pytest.fixture()
def env(tmp_path: Path) -> tuple[AtlasQuery, SeriesIndex]:
    rows = [
        _row("GDP", "FRA", "FRA", *_y(2020), "100"),
        _row("GDP", "FRA", "FRA", *_y(2021), "110"),
        _row("GDP", "FRA", "FRA", *_y(2022), "121"),
        _row("GDP", "FRA", "FRA", *_y(2023), "130", estimate_kind="projection"),
        _row("GDP", "DEU", "DEU", *_y(2020), "200"),
        _row("GDP", "DEU", "DEU", *_y(2021), "220"),
        _row("GDP", "DEU", "DEU", *_y(2022), None, missing="not_reported"),
        _row("GDP", "ITA", "ITA", *_y(2020), "50"),
        _row("GDP", "WLD", None, *_y(2021), "9000"),
        _row("CPI|sex=T", "FRA", "FRA", date(2023, 1, 1), date(2023, 2, 1), "1.5"),
        _row("CPI|sex=T", "FRA", "FRA", date(2023, 2, 1), date(2023, 3, 1), "1.7"),
        _row("CPI|sex=T", "DEU", "DEU", date(2023, 2, 1), date(2023, 3, 1), "2.1"),
        _row("CPI|sex=F", "FRA", "FRA", date(2023, 1, 1), date(2023, 2, 1), "1.4"),
        _row("POP", "FR10", "FRA", *_y(2021), "12000000"),
        _row("POP", "FRA", "FRA", *_y(2021), "67000000"),
    ]
    df = pl.DataFrame(rows).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    df = df.sort(["indicator", "provider_geo", "period_start"])
    store = FilesystemArtifactStore(tmp_path / "objects")
    obj = store.put_bytes(parquet_bytes(to_arrow(df)), "normalised", "application/vnd.apache.parquet")
    table = {
        "dataset_key": "atlas.src.ds",
        "dataset_id": "1",
        "artifact_sha256": obj.sha256,
        "row_count": df.height,
        "recorded_at": "2026-10-16T00:00:00+00:00",
    }
    snap = AtlasSnapshot(obj.sha256[::-1], "test_fixture", datetime(2026, 10, 16, tzinfo=UTC), (table,))
    cat = Catalog(
        concepts={
            "eco.gdp": Concept(
                "eco.gdp",
                {"fr": "Produit intérieur brut", "en": "Gross domestic product"},
                "d",
                "Current US$",
                "economy",
                None,
                (Series("src", "ds", "GDP"),),
                theme="economy",
            )
        }
    )
    q = AtlasQuery(snap, store, tmp_path / "cache")
    svc._META.clear()
    svc._PERIODS.clear()
    return q, SeriesIndex(q, cat)


def test_index_search_and_resolution(env: tuple[AtlasQuery, SeriesIndex]) -> None:
    q, idx = env
    res = idx.search("produit interieur")  # accents folded
    assert [h.concept.id for h in res["concepts"]] == ["eco.gdp"]
    assert idx.primary_series(res["concepts"][0].concept) == ("src", "ds", "GDP")
    assert [m.measure for m in idx.search("cpi")["measures"]] == ["CPI"]
    m = idx.measure("src.ds", "CPI")
    assert m is not None and m.facets() == [{"key": "sex", "values": ["T", "F"], "truncated": False}]
    assert m.default().indicator == "CPI|sex=T"  # "total" value first
    sid, *_rest = svc.resolve(idx, table="src.ds", measure="CPI", filters={"sex": "F"})
    assert q.series_of[sid][2] == "CPI|sex=F"
    with pytest.raises(ValidationProblem):
        svc.resolve(idx, table="src.ds", measure="CPI", filters={"sex": "X"})
    with pytest.raises(ValidationProblem):
        svc.resolve(idx, series="src.ds.NOPE")


def test_map_slice_values_and_typed_missing(env: tuple[AtlasQuery, SeriesIndex]) -> None:
    q, idx = env
    sid, *_ = svc.resolve(idx, series="src.ds.GDP")
    d = svc.map_slice(q, idx, sid, precision=None, period=None)
    # the default period is the latest with an observation (2022), never a projection-only year
    assert d["precision"] == "year" and d["period"] == "2022"
    assert [p["code"] for p in d["periods"]] == ["2020", "2021", "2022", "2023"]
    assert d["periods"][-1] == {"code": "2023", "label": "2023", "obs": 0, "proj": 1}
    vals = {v["iso3"]: v["v"] for v in d["values"]}
    assert vals == {"FRA": "121"}  # exact decimal strings
    miss = {m["geo"]: m["reason"] for m in d["missing"]}
    assert miss["DEU"] == "provider:not_reported"  # the provider's own missing cell
    assert miss["ITA"] == "not_this_period"  # the series reports ITA in another year
    assert miss["ESP"] == "not_in_series"  # never reported: absent, not zero
    assert "FRA" not in miss
    assert d["meta"]["concept"]["id"] == "eco.gdp" and d["meta"]["levels"] == {"adm0": 3, "adm1": 0}
    agg = svc.map_slice(q, idx, sid, precision="year", period="2021")["aggregates"]
    assert [(a["geo"], a["v"]) for a in agg] == [("WLD", "9000")]  # provider aggregates are never countries


def test_projections_hidden_then_flagged(env: tuple[AtlasQuery, SeriesIndex]) -> None:
    q, idx = env
    sid, *_ = svc.resolve(idx, series="src.ds.GDP")
    hidden = svc.map_slice(q, idx, sid, precision="year", period="2023")
    assert hidden["values"] == [] and {m["geo"]: m["reason"] for m in hidden["missing"]}["FRA"] == "projection_hidden"
    shown = svc.map_slice(q, idx, sid, precision="year", period="2023", projections="include")
    (v,) = shown["values"]
    assert v["proj"] is True and v["v"] == "130" and shown["counts"]["projection"] == 1


def test_monthly_slice_and_subnational_level(env: tuple[AtlasQuery, SeriesIndex]) -> None:
    q, idx = env
    sid, *_ = svc.resolve(idx, table="src.ds", measure="CPI", filters={})
    d = svc.map_slice(q, idx, sid, precision=None, period=None)
    assert d["precision"] == "month" and d["period"] == "2023-02"
    assert [p["label"] for p in d["periods"]] == ["janv. 2023", "févr. 2023"]
    assert {v["iso3"]: v["v"] for v in d["values"]} == {"FRA": "1.7", "DEU": "2.1"}
    pop, *_ = svc.resolve(idx, series="src.ds.POP")
    meta = svc.series_meta(q, idx, pop)
    assert meta["levels"] == {"adm0": 1, "adm1": 1}
    adm1 = svc.map_slice(q, idx, pop, precision="year", period="2021", level="adm1")
    assert [(v["geo"], v["v"]) for v in adm1["values"]] == [("FR10", "12000000")]
    adm0 = svc.map_slice(q, idx, pop, precision="year", period="2021")
    assert [(v["geo"], v["v"]) for v in adm0["values"]] == [("FRA", "67000000")]  # the country row, not the region


def test_place_and_multi_series(env: tuple[AtlasQuery, SeriesIndex]) -> None:
    q, idx = env
    sid, *_ = svc.resolve(idx, series="src.ds.GDP")
    p = svc.place_series(q, idx, sid, "FRA")
    assert [(x["p"], x["v"], x["proj"]) for x in p["points"]] == [
        ("2020", "100", False),
        ("2021", "110", False),
        ("2022", "121", False),
        ("2023", "130", True),
    ]
    deu = svc.place_series(q, idx, sid, "DEU")["points"]
    assert deu[-1]["v"] is None and deu[-1]["miss"] == "not_reported"
    cpi, *_ = svc.resolve(idx, table="src.ds", measure="CPI", filters={})
    m = svc.multi_series(q, idx, [sid, cpi], ["FRA", "DEU", "XXX1"])
    assert m["geos"] == ["FRA", "DEU"]
    gdp, cp = m["series"]
    assert [x["v"] for x in gdp["places"]["FRA"]] == ["100", "110", "121", "130"]
    assert cp["precision"] == "month" and [x["p"] for x in cp["places"]["FRA"]] == ["2023-01", "2023-02"]
    assert gdp["transforms"]["percap"]["ok"] and gdp["transforms"]["index"]["ok"]
    assert not cp["transforms"]["percap"]["ok"]  # monthly: no annual population to divide by
    assert m["population"] is None  # only on request
