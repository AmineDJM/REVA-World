"""V6TRADEHF atlas connectors: offline parsing of the recorded fixtures (IMF IMTS monthly, Eurostat Comext monthly)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_eurostat_comext as cx
from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_imf as imf
from reva.adapters.connectors import atlas_imf_imts_m as im
from reva.atlas.sources import atlas_source_errors, atlas_sources
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.evidence.runner import FixtureHttp

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 64


def _get(name: str, url: str) -> bytes:
    async def go() -> bytes:
        return bytes((await FixtureHttp(name).get(url)).body)

    return asyncio.run(go())


def _flags(df: pl.DataFrame) -> list[dict[str, Any]]:
    return [json.loads(f) for f in df["flags"].to_list()]


def test_sources_discovered() -> None:
    errs = atlas_source_errors()
    for m in (im, cx):
        assert m.CONNECTOR_NAME in atlas_sources(), errs.get(m.CONNECTOR_NAME)
        spec = m.atlas_source()
        assert spec.datasets == m.DATASETS


def test_fixture_manifests_carry_licence() -> None:
    for m in (im, cx):
        man = json.loads((REPO / "tests/fixtures/source" / m.CONNECTOR_NAME / "manifest.json").read_text())
        assert man and all(e["licence"] for e in man)


# ------------------------------------------------------------------------------------------------ IMF IMTS
def _imts(world: bool) -> tuple[pl.DataFrame, im.ImtsReport, list[str]]:
    rep = im.ImtsReport("w" if world else "b")
    cols: dict[str, list[Any]] = {c: [] for c in fc._COLS}
    if world:
        url = im.world_url(im.FIXTURE_WORLD_START, im.FIXTURE_REPORTERS)
    else:
        url = im.bilateral_url(list(im.FIXTURE_REPORTERS), im.FIXTURE_START)
    seen = im.parse_response(_get(im.CONNECTOR_NAME, url), SHA, "x", rep, cols, world_only=world)
    return im.frame(cols, "imts_world_m" if world else "imts_m", rep), rep, seen


def test_imts_world_monthly() -> None:
    df, rep, seen = _imts(True)
    assert seen == sorted(im.FIXTURE_REPORTERS)
    assert dict(df.schema) == dict(ATLAS_SCHEMA)
    assert set(df["indicator"]) <= {"XG_FOB_USD|partner=G001", "MG_CIF_USD|partner=G001"}
    # one calendar month per cell, never a year
    assert all((e - s).days in (28, 29, 30, 31) for s, e in zip(df["period_start"], df["period_end"], strict=True))
    f = _flags(df)
    assert {x["frequency"] for x in f} == {"M"}
    assert {x["seasonal_adjustment"] for x in f} == {"not_stated_by_provider"}
    assert all(x["partner_iso3"] is None and x["partner_unmapped"] for x in f)  # World is an aggregate
    assert all(x["series_name"].startswith(("Exports of goods", "Imports of goods")) for x in f)
    assert rep.periods and rep.periods[0] == "2025-01-01"


def test_imts_bilateral_monthly() -> None:
    df, rep, _ = _imts(False)
    assert rep.partners > {"G001"} and len(rep.partners) > 50
    fra = df.filter((pl.col("provider_geo") == "FRA") & (pl.col("indicator") == "XG_FOB_USD|partner=DEU"))
    assert fra.height >= 1
    fl = _flags(fra)[0]
    assert fl["partner_iso3"] == "DEU" and fl["flow_valuation"] == "FOB"
    assert "display_scale" in fl  # IMF SCALE kept, never applied
    assert all(json.loads(s)["series"].startswith("FRA.XG_FOB_USD.DEU.M") for s in fra["selector"])
    # values are the provider's decimal strings
    assert all(fc.strict_decimal(v) == v for v in df["value"].head(50))
    assert min(df["period_start"]) == date(2026, 5, 1)


def test_imts_rejects_unrequested_partner_in_world_request() -> None:
    body = _get(im.CONNECTOR_NAME, im.bilateral_url(list(im.FIXTURE_REPORTERS), im.FIXTURE_START))
    with pytest.raises(im.ImtsMError):
        im.parse_response(body, SHA, "x", im.ImtsReport("w"), {c: [] for c in fc._COLS}, world_only=True)


def test_imts_chunks_and_options() -> None:
    assert im.chunks([f"R{i:03d}" for i in range(65)], 30) == [
        [f"R{i:03d}" for i in range(0, 30)],
        [f"R{i:03d}" for i in range(30, 60)],
        [f"R{i:03d}" for i in range(60, 65)],
    ]
    assert im.check_month("2015-01", "start") == "2015-01"
    with pytest.raises(im.ImtsMError):
        im.check_month("2015", "start")


# ------------------------------------------------------------------------------------------------- Comext
def _comext(dataset: str) -> tuple[pl.DataFrame, cx.ComextReport]:
    labels = imf.parse_codelist(_get(cx.CONNECTOR_NAME, cx.codelist_url()))
    rep = cx.ComextReport(dataset)
    cols: dict[str, list[Any]] = {c: [] for c in fc._COLS}
    for r in cx.FIXTURE_REPORTERS:
        for d, _k, url in cx.requests_for(r, cx.FIXTURE_START, cx.FIXTURE_START):
            if d == dataset:
                cx.parse_tsv(_get(cx.CONNECTOR_NAME, url), SHA, "x", rep, cols, labels, expect={"reporter": (r,)})
    return cx.frame(cols, dataset, rep), rep


def test_comext_hs2() -> None:
    df, rep = _comext("comext_hs2_m")
    assert dict(df.schema) == dict(ATLAS_SCHEMA)
    assert rep.reporters == set(cx.FIXTURE_REPORTERS)
    assert rep.partners == set(cx.PARTNER_AGGREGATES)
    assert "TOTAL" in rep.products and "27" in rep.products
    assert set(df["unit"]) == {"euro", "100 kg"}
    oil = df.filter(pl.col("indicator") == "VALUE_IN_EUROS|flow=1|partner=EXT_EU27_2020|product=27")
    assert oil.height >= 1
    fl = _flags(oil)[0]
    assert fl["hs_chapter_kg"] == "hs2022:27" and fl["flow_label"] == "IMPORT"
    assert fl["partner_label"] == "Extra-EU27 (from 2020)" and fl["partner_iso3"] is None
    tot = _flags(df.filter(pl.col("indicator").str.ends_with("product=TOTAL")))
    assert tot and all("hs_chapter_kg" not in x for x in tot)
    assert all("hs_chapter_kg" not in x for x in _flags(df.filter(pl.col("indicator").str.ends_with("product=99"))))
    assert rep.not_available  # ':' cells counted, never zero-filled


def test_comext_partner() -> None:
    df, rep = _comext("comext_partner_m")
    assert len(rep.partners) > 100
    us = df.filter(
        (pl.col("provider_geo") == "GR") & (pl.col("indicator") == "VALUE_IN_EUROS|flow=2|partner=US|product=TOTAL")
    )
    assert us.height >= 1 and _flags(us)[0]["partner_iso3"] == "USA"
    assert country_iso3("GR", cx.GEO_SCHEME) == "GRC"  # Comext uses ISO alpha-2, not Eurostat 'EL'
    assert country_iso3("XK", cx.GEO_SCHEME) == "XKX"
    assert country_iso3("WORLD", cx.GEO_SCHEME) is None


def test_comext_tsv_tokens_and_guards() -> None:
    assert cx._token("123 p") == ("123", "p")
    assert cx._token(": c") == (None, "c")
    assert cx._token("12.50") == ("12.50", "")
    with pytest.raises(cx.ComextError):
        cx._token("abc")
    soap = b'<?xml version="1.0"?><env:Envelope><queued><id>x</id><status>SUBMITTED</status></queued></env:Envelope>'
    with pytest.raises(cx.ComextError):
        cx.parse_tsv(soap, SHA, "x", cx.ComextReport("d"), {c: [] for c in fc._COLS}, {}, expect={})


def test_comext_request_plan() -> None:
    reqs = cx.requests_for("DE", "2022-01", "2024-01")
    assert len(reqs) == len(cx.CHAPTER_GROUPS) + 1 == 5
    chapters = [c for g in cx.CHAPTER_GROUPS for c in g if c != "TOTAL"]
    assert sorted(chapters) == sorted(cx.CHAPTERS) and "77" not in chapters and "98" not in chapters
    assert all(len(g) <= 26 for g in cx.CHAPTER_GROUPS)
    assert reqs[-1][2].endswith("/M.DE..TOTAL.1+2.VALUE_IN_EUROS?startPeriod=2024-01&format=TSV")


def test_hs_dimension_declared() -> None:
    doc = yaml.safe_load((REPO / "registry/kg/v4link_atlas_dimensions.yaml").read_text())
    d = next(x for x in doc["dimensions"] if x["atlas"] == "eurostat.comext_hs2_m")
    assert d["dimension"] == "product" and d["kg_prefix"] == "hs2022:"
