"""V4ECON atlas connectors: offline parsing of the recorded fixtures (IMF monthly/quarterly, Eurostat macro and NUTS,
OECD KEI and regional TL2/TL3, World Bank ICP 2021, FAO Food Price Index) and the statistical-region KG builder."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_eurostat_macro as es
from reva.adapters.connectors import atlas_fao_ffpi as ffpi
from reva.adapters.connectors import atlas_imf as imf
from reva.adapters.connectors import atlas_imf_hf as ih
from reva.adapters.connectors import atlas_oecd_macro as om
from reva.adapters.connectors import atlas_wb_icp as icp
from reva.adapters.connectors import econ_regions_kg as rkg
from reva.atlas.sources import atlas_source_errors, atlas_sources
from reva.atlas.tables import ATLAS_SCHEMA
from reva.evidence.runner import FixtureHttp

REPO = Path(__file__).resolve().parents[2]
MODULES = {m.CONNECTOR_NAME: m for m in (ih, es, om, icp, ffpi)}
SHA = "a" * 64


def _get(name: str, url: str) -> bytes:
    async def go() -> bytes:
        return bytes((await FixtureHttp(name).get(url)).body)

    return asyncio.run(go())


def _flags(df: pl.DataFrame) -> list[dict[str, Any]]:
    return [json.loads(f) for f in df["flags"].to_list()]


def test_sources_discovered_with_valid_specs() -> None:
    srcs = atlas_sources()
    for name in MODULES:
        spec = srcs[name].spec
        assert spec.code_digest.startswith("sha256:")
    assert srcs[ih.CONNECTOR_NAME].spec.source_key == "imf.sdmx"
    assert srcs[es.CONNECTOR_NAME].spec.source_key == "eurostat.dissemination"
    assert srcs[om.CONNECTOR_NAME].spec.source_key == "oecd.sdmx"
    assert srcs[icp.CONNECTOR_NAME].spec.budget_key == "worldbank.atlas"
    mine = {"atlas_imf_hf", "atlas_eurostat_macro", "atlas_oecd_macro", "atlas_wb_icp", "atlas_fao_ffpi"}
    assert not [e for e in atlas_source_errors() if e["module"].rsplit(".", 1)[-1] in mine]


def test_modules_build_no_http_client() -> None:
    for m in (
        "atlas_imf_hf",
        "atlas_eurostat_macro",
        "atlas_oecd_macro",
        "atlas_wb_icp",
        "atlas_fao_ffpi",
        "econ_regions_kg",
    ):
        src = (REPO / f"src/reva/adapters/connectors/{m}.py").read_text()
        for bad in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen("):
            assert bad not in src, (m, bad)


# --------------------------------------------------------------------------- IMF
@pytest.mark.parametrize("fl", ih.FLOWS, ids=lambda f: f.dataset)
def test_imf_flows_parse_with_period_precision_and_sa_flag(fl: ih.HfFlow) -> None:
    body = _get(ih.CONNECTOR_NAME, fl.url("+".join(ih.FIXTURE_GEOS), ih.FIXTURE_START))
    df, rep = ih.parse_flow(fl, body, SHA, "x.xml")
    assert df.schema == ATLAS_SCHEMA and df.height == rep.cells > 0
    assert df["value"].null_count() == 0
    days = [(e - s).days for s, e in zip(df["period_start"], df["period_end"], strict=True)]
    if fl.freqs == ("M",):
        assert min(days) >= 28 and max(days) <= 31
    if fl.freqs == ("Q",):
        assert min(days) >= 90 and max(days) <= 92
    for f in _flags(df):
        assert f["frequency"] in fl.freqs
        assert "seasonal_adjustment" in f
        if f["seasonal_adjustment"] != "not_stated_by_provider":
            assert f["seasonal_adjustment_label"] == fl.sa_codes[f["seasonal_adjustment"]]


def test_imf_qnea_keeps_every_adjustment_and_never_rescales() -> None:
    fl = ih.FLOWS_BY_DATASET["qnea_q"]
    df, _rep = ih.parse_flow(fl, _get(ih.CONNECTOR_NAME, fl.url("+".join(ih.FIXTURE_GEOS), ih.FIXTURE_START)), SHA, "x")
    sa = {f["seasonal_adjustment"] for f in _flags(df)}
    assert "not_stated_by_provider" not in sa and sa <= set(ih.S_ADJUSTMENT_LABELS)
    fra = df.filter((pl.col("provider_geo") == "FRA") & pl.col("indicator").str.ends_with("type_of_transformation=XDC"))
    assert fra.height > 0 and all(float(v) > 1e9 for v in fra["value"])  # full units (euros), display SCALE not applied


def test_imf_period_parser_and_fail_closed_code() -> None:
    assert ih.imf_period("M", "2025-M12") == (date(2025, 12, 1), date(2026, 1, 1))
    assert ih.imf_period("Q", "2025-Q4") == (date(2025, 10, 1), date(2026, 1, 1))
    with pytest.raises(ih.ImfHfError):
        ih.imf_period("M", "2025-13")
    fl = ih.FLOWS_BY_DATASET["cpi_m"]
    body = _get(ih.CONNECTOR_NAME, fl.url("+".join(ih.FIXTURE_GEOS), ih.FIXTURE_START))
    tampered = body.replace(
        b'COICOP_1999="_T" TYPE_OF_TRANSFORMATION="IX" FREQUENCY',
        b'COICOP_1999="_T" TYPE_OF_TRANSFORMATION="WGT" FREQUENCY',
    )
    assert tampered != body
    with pytest.raises(ih.ImfHfError, match="quoted codelist"):
        ih.parse_flow(fl, tampered, SHA, "x")
    assert imf.GEO_SCHEME == ih.GEO_SCHEME


# --------------------------------------------------------------------------- Eurostat
@pytest.mark.parametrize("q", es.QUERIES, ids=lambda q: q.dataset)
def test_eurostat_queries_parse(q: es.EsQuery) -> None:
    df, rep, _geo = es.parse_jsonstat(q, _get(es.CONNECTOR_NAME, q.url(True)), SHA)
    assert df.schema == ATLAS_SCHEMA and df.height == rep.cells > 0
    flags = _flags(df)
    if q.freq in ("M", "Q"):
        assert all("seasonal_adjustment" in f for f in flags)
    if q.regional:
        regional = df.filter(pl.col("provider_geo").str.len_chars() > 2)
        assert regional["iso3"].null_count() == regional.height  # a region is never a country cell
        assert all(f["nuts_level"] in "0123" for f in flags if "nuts_level" in f)
        nuts0 = df.filter(pl.col("provider_geo") == "FR")
        assert set(nuts0["iso3"].to_list()) <= {"FRA"}


def test_eurostat_seasonal_adjustment_verbatim() -> None:
    q = es.QUERIES_BY_DATASET["une_m"]
    df, _rep, _ = es.parse_jsonstat(q, _get(es.CONNECTOR_NAME, q.url(True)), SHA)
    labels = {f["seasonal_adjustment"]: f["seasonal_adjustment_label"] for f in _flags(df)}
    assert labels.get("NSA", "").startswith("Unadjusted data")
    h = es.QUERIES_BY_DATASET["hicp_m"]
    dh, _, _ = es.parse_jsonstat(h, _get(es.CONNECTOR_NAME, h.url(True)), SHA)
    assert {f["seasonal_adjustment"] for f in _flags(dh)} == {"not_stated_by_provider"}


def test_nuts_regions_hierarchy_from_codes() -> None:
    q = es.QUERIES_BY_DATASET["nuts_gdp"]
    df, _rep, geo = es.parse_jsonstat(q, _get(es.CONNECTOR_NAME, q.url(True)), SHA)
    present = set(df["provider_geo"].to_list())
    regs = rkg.nuts_regions(
        {g: lab for g, lab in geo.items() if g in present},
        table="eurostat.nuts_gdp",
        described_by="t",
        raw_sha256=SHA,
        dataset_code=q.code,
    )
    assert "FR" not in regs and "EU27_2020" not in regs  # countries and aggregates are not regions
    assert regs["FR101"].parent == "FR10" and regs["FR10"].parent == "FR1" and regs["FR1"].parent is None
    assert regs["EL303"].country_iso3 == "GRC" and regs["FR101"].level == "NUTS 3"
    assert rkg.nuts_country("UKI3") == "GBR"


# --------------------------------------------------------------------------- OECD
def _oecd(dataset: str) -> tuple[pl.DataFrame, om.OReport, dict[str, tuple[str, str]]]:
    fl = om.FLOWS_BY_DATASET[dataset]
    return om.parse_csvfile(fl, _get(om.CONNECTOR_NAME, fl.url(True)), SHA, "x.csv")


def test_oecd_kei_adjustment_and_iso3() -> None:
    df, rep, seen = _oecd("kei_mq")
    assert not seen and df.height == rep.cells > 0
    assert (
        df.filter(pl.col("provider_geo") == "EA20")["iso3"].null_count()
        == df.filter(pl.col("provider_geo") == "EA20").height
    )
    adj = {f["seasonal_adjustment"] for f in _flags(df)}
    assert adj <= {"Y", "_Z"}
    une = df.filter(pl.col("indicator").str.starts_with("UNEMP"))
    assert une.height > 0 and set(une["unit"]) == {"Percentage of labour force"}


@pytest.mark.parametrize("dataset", ["reg_gdp", "reg_une", "reg_pop"])
def test_oecd_regional_rows(dataset: str) -> None:
    df, _rep, seen = _oecd(dataset)
    for f, geo, iso3 in zip(_flags(df), df["provider_geo"], df["iso3"], strict=True):
        assert f["territorial_level"] in ("TL2", "TL3", "CTRY")
        if f["territorial_level"] == "CTRY":
            assert iso3 == geo
        else:
            assert iso3 is None and f["country_iso3"]
    assert seen and all(v[0] in ("TL2", "TL3") for v in seen.values())


def test_oecd_xdc_gdp_keeps_unit_multiplier() -> None:
    df, _, _ = _oecd("reg_gdp")
    xdc = df.filter(pl.col("indicator").str.ends_with("unit_measure=XDC"))
    if xdc.height:
        assert all(u.startswith("National currency") for u in xdc["unit"])
        assert all("currency" in f for f in _flags(xdc))


def test_oecd_region_hierarchy_from_codelist() -> None:
    cl = om.parse_cl_regional(_get(om.CONNECTOR_NAME, om.cl_regional_url(True)), min_codes=1)
    assert cl["FR101"].name_en and cl["FRL04"].parent == "FRL"
    seen: dict[str, tuple[str, str]] = {}
    for d in ("reg_gdp", "reg_une", "reg_pop"):
        seen.update(_oecd(d)[2])
    regs = om.oecd_regions(seen, cl, table="oecd.reg_pop", described_by="t", raw_sha256=SHA, codelist_sha256=SHA)
    info = om.resolve_parents(regs, cl)
    assert sum(info.values()) == len(regs)
    if "FRL04" in regs and "FRL" in regs:
        assert regs["FRL04"].parent == "FRL"


def test_region_kg_build_uses_identifiers_only() -> None:
    regs = {
        "FR10": rkg.Region("nuts", "FR10", "Ile-de-France", "NUTS 2", "FRA", parent="FR1", parent_basis="b"),
        "FR1": rkg.Region("nuts", "FR1", "Ile de France", "NUTS 1", "FRA", tables={"eurostat.nuts_gdp"}),
        "US06": rkg.Region("oecdreg", "US06", "California", "TL2", "USA", tables={"oecd.reg_pop"}),
    }
    for r in regs.values():
        r.raw_sha256 = SHA
    ents, sts, info = rkg.build(
        regs,
        {"FRA": "Q142"},
        source_key="x.y",
        revision="r",
        retrieved_at=datetime(2026, 10, 17, tzinfo=UTC),
        licence="l",
        table_titles={},
    )
    kinds = {e.qid: e.kind for e in ents}
    assert kinds["nuts:FR10"] == "nuts_region" and kinds["oecdreg:US06"] == "oecd_region"
    assert kinds["atlas:eurostat.nuts_gdp"] == "data_series"
    edges = {(s.subject_qid, s.property_pid, s.object_qid) for s in sts if s.object_qid}
    assert ("nuts:FR10", "nuts:part_of", "nuts:FR1") in edges
    assert ("nuts:FR10", "nuts:in_country", "Q142") in edges
    assert ("oecdreg:US06", "econ:measured_by", "atlas:oecd.reg_pop") in edges
    assert info["country_qid_missing"] == 1  # USA has no QID in this map: no edge, counted
    assert not any(
        s.object_qid and s.object_qid.startswith("nuts:") and s.subject_qid.startswith("oecdreg:") for s in sts
    )


def test_relations_dropin_maps_every_entity_predicate() -> None:
    import yaml

    doc = yaml.safe_load((REPO / "registry/kg/relations_v4.d/v4econ.yaml").read_text())
    mapped = {m["property"]: m["family"] for m in doc["mappings"]}
    for pid, (kind, *_rest) in rkg.PROPERTIES.items():
        if kind == "item":
            assert pid in mapped, pid
    assert mapped["nuts:part_of"] == "part_of" and mapped["oecdreg:in_country"] == "located_in"
    assert mapped["econ:measured_by"] == "measured_by"


# --------------------------------------------------------------------------- ICP, FFPI
def test_icp_parse() -> None:
    df, rep = icp.parse(_get(icp.CONNECTOR_NAME, icp.url(True)), SHA)
    assert df.height == rep["cells"] > 0 and rep["null_values"] >= 0
    assert set(df["unit"]) <= set(icp.CLASSIFICATIONS.values())
    assert (
        df.filter(pl.col("provider_geo") == "WLD")["iso3"].null_count()
        == df.filter(pl.col("provider_geo") == "WLD").height
    )
    gdp = df.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "1000000|classification=PX.WL"))
    assert gdp.height >= 1


def test_ffpi_parse_and_licence_flag() -> None:
    facts = ffpi.page_facts(_get(ffpi.CONNECTOR_NAME, ffpi.PAGE_URL))
    assert facts["definition"].startswith(ffpi.DEFINITION_START)
    df, _rep = ffpi.parse_csv(_get(ffpi.CONNECTOR_NAME, ffpi.CSV_URL), SHA, facts)
    assert set(df["indicator"]) == set(ffpi.COLUMNS) and set(df["provider_geo"]) == {"world"}
    assert df["iso3"].null_count() == df.height
    assert all(f["licence_restriction"] == "non_commercial" for f in _flags(df))
    first = df.filter(pl.col("indicator") == "Food Price Index").sort("period_start").row(0, named=True)
    assert first["period_start"] == date(1990, 1, 1) and first["value"] == "64.4"
