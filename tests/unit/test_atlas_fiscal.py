"""A-FISCAL atlas connectors (OECD fiscal / skills SDMX, BIS statistics): offline parsing of the recorded fixtures."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_bis as bis
from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_oecd_fiscal as of
from reva.adapters.connectors import atlas_pisa as pz
from reva.atlas.catalog import CATALOG_DIR, load_catalog
from reva.atlas.sources import atlas_source_errors, atlas_sources
from reva.atlas.tables import ATLAS_SCHEMA
from reva.evidence.runner import FixtureHttp

MODULES = {of.CONNECTOR_NAME: of, pz.CONNECTOR_NAME: pz, bis.CONNECTOR_NAME: bis}
REPO = Path(__file__).resolve().parents[2]


def _files(mod: Any, flow: fc.Flow) -> list[tuple[str, bytes, str]]:
    http = FixtureHttp(mod.CONNECTOR_NAME)
    reqs = bis.bis_requests(flow, True) if mod is bis else of.oecd_requests(flow, True)

    async def go() -> list[tuple[str, bytes, str]]:
        return [(rq.label, (await http.get(rq.url)).body, "a" * 64) for rq in reqs]

    return asyncio.run(go())


def _parse(mod: Any, dataset: str) -> tuple[pl.DataFrame, fc.FlowReport]:
    flow = next(f for f in mod.FLOWS if f.dataset == dataset)
    build = bis.bis_build if mod is bis else of.oecd_build
    frame, rep, note = build(flow, _files(mod, flow))
    assert note and len(note) <= 4000
    return frame, rep


def test_sources_are_discovered_with_valid_specs() -> None:
    srcs = atlas_sources()
    for name, mod in MODULES.items():
        spec = srcs[name].spec
        assert spec.datasets == tuple(f.dataset for f in mod.FLOWS)
        assert spec.planned_requests == sum(len(f.keys) for f in mod.FLOWS)
        assert spec.code_digest.startswith("sha256:")
    assert srcs[of.CONNECTOR_NAME].spec.source_key == srcs[pz.CONNECTOR_NAME].spec.source_key == "oecd.sdmx"
    assert srcs[bis.CONNECTOR_NAME].spec.source_key == "bis.stats"
    mine = {"atlas_fiscal_common", "atlas_oecd_fiscal", "atlas_pisa", "atlas_bis"}
    assert not [e for e in atlas_source_errors() if e["module"].rsplit(".", 1)[-1] in mine]


def test_modules_build_no_http_client() -> None:
    for m in ("atlas_fiscal_common", "atlas_oecd_fiscal", "atlas_pisa", "atlas_bis"):
        src = (REPO / f"src/reva/adapters/connectors/{m}.py").read_text()
        for bad in ("SafeHttpClient(", "make_live_client(", "httpx.", "urlopen(", "requests."):
            assert bad not in src, (m, bad)


@pytest.mark.parametrize(
    "freq, text, start, end",
    [
        ("A", "2020", date(2020, 1, 1), date(2021, 1, 1)),
        ("Q", "2023-Q4", date(2023, 10, 1), date(2024, 1, 1)),
        ("Q", "2023-Q2", date(2023, 4, 1), date(2023, 7, 1)),
        ("M", "2024-12", date(2024, 12, 1), date(2025, 1, 1)),
        ("M", "1946-02", date(1946, 2, 1), date(1946, 3, 1)),
        ("S", "2020-S2", date(2020, 7, 1), date(2021, 1, 1)),
    ],
)
def test_sdmx_periods(freq: str, text: str, start: date, end: date) -> None:
    assert fc.sdmx_period(freq, text) == (start, end)


@pytest.mark.parametrize("freq, text", [("A", "2020-Q1"), ("Q", "2020-Q5"), ("M", "2020-13"), ("D", "2020-01-01")])
def test_bad_periods_are_refused(freq: str, text: str) -> None:
    with pytest.raises(fc.FiscalParseError):
        fc.sdmx_period(freq, text)


def test_strict_decimal_keeps_provider_text() -> None:
    assert fc.strict_decimal("62986.695409") == "62986.695409"
    assert fc.strict_decimal("1E-5") == "1E-5"
    assert fc.strict_decimal(".5") == "0.5"
    for bad in ("1,000", "inf", "x"):
        with pytest.raises(fc.FiscalParseError):
            fc.strict_decimal(bad)


def test_both_label_styles_are_read() -> None:
    oecd = fc.read_labelled_csv(_files(of, of.FLOWS_BY_DATASET["cts_cit"])[0][1], "cit")
    assert oecd.style == "oecd" and oecd.rows[0]["REF_AREA"][1]  # label column
    flow = bis.FLOWS_BY_DATASET["dsr"]
    b = fc.read_labelled_csv(_files(bis, flow)[0][1], "dsr", coded=frozenset(flow.dims) | {"UNIT_MEASURE"})
    assert b.style == "bis"
    row = b.rows[0]
    assert row["FREQ"] == ("Q", "Quarterly") and row["UNIT_MEASURE"] == ("367", "Per cent")
    with pytest.raises(fc.FiscalParseError, match="HTML"):
        fc.read_labelled_csv(b"<!doctype html><html></html>", "x")
    with pytest.raises(fc.FiscalParseError):
        fc.read_labelled_csv(b"a,b\n1,2\n", "x")


@pytest.mark.parametrize("mod", [of, pz, bis])
def test_every_fixture_flow_parses_to_a_valid_frame(mod: Any) -> None:
    for flow in mod.FLOWS:
        frame, rep = _parse(mod, flow.dataset)
        assert frame.schema == ATLAS_SCHEMA and frame.height == rep.cells > 0
        assert frame["source"].unique().to_list() == [mod.SOURCE if mod is not pz else "oecd"]
        assert frame["value"].null_count() == 0  # value-less rows are not cells
        for f in frame["flags"].to_list()[:50]:
            assert isinstance(json.loads(f), dict)
        assert int(frame["unit"].str.len_chars().max()) <= 100  # type: ignore[arg-type]


def test_revenue_statistics_cells() -> None:
    frame, rep = _parse(of, "rs_global")
    vat = frame.filter(
        (pl.col("indicator") == "TAX_REV|sector=S13|standard_revenue=T_5111|unit_measure=PT_B1GQ")
        & (pl.col("provider_geo") == "FRA")
    )
    assert vat.height > 0 and vat["unit"].unique().to_list() == ["Percentage of GDP"]
    assert set(vat["iso3"].to_list()) == {"FRA"}
    # regional average kept unmapped with its provider label
    assert rep.unmapped("iso3") == {"A9": "Latin America and the Caribbean"}
    assert frame.filter(pl.col("provider_geo") == "A9")["iso3"].null_count() > 0
    # national currency: unit multiplier kept, never applied; ISO 4217 code in flags
    xdc = frame.filter(pl.col("indicator").str.ends_with("unit_measure=XDC") & (pl.col("provider_geo") == "KEN"))
    fl = json.loads(xdc["flags"][0])
    assert fl["currency"] == "KES" and fl["unit_mult"] in ("6", "9") and "x10^" in xdc["unit"][0]
    # rows without a value are counted, never filled
    assert sum(rep.no_value.values()) == rep.rows - rep.cells


def test_flagged_status_and_missing_rows_in_cofog() -> None:
    frame, rep = _parse(of, "gov_cofog")
    prov = frame.filter(pl.col("flags").str.contains('"obs_status":"P"'))
    assert prov.height == rep.statuses["P"] > 0
    assert json.loads(prov["flags"][0])["obs_status_label"] == "Provisional value"


def test_pisa_scores() -> None:
    frame, _ = _parse(pz, "hsl_skills")
    math = frame.filter(
        (pl.col("indicator") == "6_2|unit_measure=PO|age=_T|sex=_T|education_lev=_T") & (pl.col("iso3") == "FRA")
    )
    assert math.height >= 1 and math["unit"].unique().to_list() == ["Points"]
    assert all(d.year in (2018, 2022) for d in math["period_start"].to_list())


def test_bis_tables_keep_frequency_and_series_attributes() -> None:
    eer, rep = _parse(bis, "eer")
    assert rep.no_value["(series attribute row)"] > 0
    # EER observation rows leave UNIT_MEASURE empty: taken from the series attribute row
    assert eer["unit"].unique().to_list() == ["Index, 2020 = 100"]
    assert int((eer["period_end"] - eer["period_start"]).dt.total_days().max()) <= 31  # type: ignore[arg-type]  # monthly
    assert json.loads(eer["flags"][0])["collection"] == "A"
    pol, rep = _parse(bis, "cbpol")
    assert pol["indicator"].unique().to_list() == ["CBPOL"] and rep.unmapped("iso2") == {"XM": "Euro area"}
    assert "compilation" in json.loads(pol.filter(pl.col("provider_geo") == "XM")["flags"][0])
    us = pol.filter(pl.col("provider_geo") == "US")
    assert set(us["iso3"].to_list()) == {"USA"}
    tc, _ = _parse(bis, "tc")
    assert tc["indicator"].str.starts_with("TC|tc_borrowers=").all()
    q = tc["period_start"].dt.month().unique().sort().to_list()
    assert set(q) <= {1, 4, 7, 10}


def test_changed_dimension_order_or_foreign_rows_are_refused() -> None:
    flow = of.FLOWS_BY_DATASET["cts_cit"]
    files = _files(of, flow)
    moved = fc.Flow(**{**flow.__dict__, "dims": tuple(reversed(flow.dims))})
    with pytest.raises(fc.FiscalParseError, match="dimension order"):
        fc.parse_flow(moved, files, source="oecd", geo_scheme="iso3")
    other = fc.Flow(**{**flow.__dict__, "flow": "DSD_OTHER@DF_X"})
    with pytest.raises(fc.FiscalParseError, match="another structure"):
        fc.parse_flow(other, files, source="oecd", geo_scheme="iso3")
    with pytest.raises(fc.FiscalParseError, match="duplicate"):
        fc.parse_flow(flow, files + files, source="oecd", geo_scheme="iso3")


def test_catalog_series_point_at_declared_tables() -> None:
    cat = load_catalog(CATALOG_DIR, strict=True)
    tables = {(m.SOURCE if m is not pz else "oecd", f.dataset) for m in MODULES.values() for f in m.FLOWS}
    mine = [c for c in cat.concepts.values() if c.domain in ("fiscal", "finance", "education_outcomes")]
    assert len(mine) >= 100
    for c in mine:
        for s in c.source_order:
            assert (s.source, s.dataset) in tables, (c.id, s.key)
    assert {c.id for c in mine} >= {"fiscal.tax_total_gdp", "finance.policy_rate", "education_outcomes.pisa_math_mean"}
