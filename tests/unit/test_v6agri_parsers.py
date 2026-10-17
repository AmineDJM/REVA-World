"""V6AGRI food-systems connectors: parsers on captured excerpts (no network, no database).

Fixtures (``tests/fixtures/source/atlas.{usda_psd,usda_wasde,fao_tm,ifpri_er}``, licence + live sha256 in each
manifest): PSD zips rebuilt with verbatim lines of the live files (10 countries, 3 commodities each, market years
>= 2018); WASDE CSVs reduced to the World Rice / U.S. quarterly / reliability lines of the May 2026 (V2) and
October 2026 reports; the FAOSTAT TM Asia archive rebuilt with the first 1,500 real lines of the wide member and the
real code lists; the IFPRI tool page and both workbooks verbatim.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_fao_tm as tm
from reva.adapters.connectors import atlas_ifpri_er as ie
from reva.adapters.connectors import atlas_usda_psd as psd
from reva.adapters.connectors import atlas_usda_wasde as wa
from reva.adapters.connectors.agri_common import load_predicates, slug
from reva.adapters.connectors.vintage_common import AVAILABILITY_RULES, FAMILIES, Rows, known_variable
from reva.atlas.sources import discover
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evaluation.vintages import VINTAGE_DATASET_KEYS, cells_from_atlas_frame

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "source"
SHA, SHA2 = "a" * 64, "b" * 64


def _entry(source: str, url_part: str) -> tuple[bytes, dict[str, Any]]:
    man = json.loads((FIX / source / "manifest.json").read_text())
    hits = [e for e in man if url_part in e["url"]]
    assert len(hits) == 1, url_part
    return (FIX / source / hits[0]["file"]).read_bytes(), hits[0]


def test_sources_are_discovered() -> None:
    found, problems = discover([psd.__name__, wa.__name__, tm.__name__, ie.__name__])
    assert not problems
    assert {psd.CONNECTOR_NAME, wa.NAME, tm.CONNECTOR_NAME, ie.CONNECTOR_NAME} <= set(found)


def test_slug_is_a_respelling_only() -> None:
    assert slug("World Rice Supply and Use  (Milled Basis)") == "world_rice_supply_and_use_milled_basis"
    with pytest.raises(ValueError):
        slug(" () ")


# --------------------------------------------------------------------------- PSD
def test_psd_geo_scheme_needs_code_and_name_agreement() -> None:
    assert country_iso3("IN", "usda_psd") == "IND"
    assert country_iso3("VM", "usda_psd") == "VNM"
    # Burma: GeoNames FIPS BM = Myanmar, but PSD names it "Burma": no alias from a name alone
    assert country_iso3("BM", "usda_psd") is None
    # GU is Guinea in PSD (an old USDA code), not a GeoNames FIPS code: unmapped
    assert country_iso3("GU", "usda_psd") is None
    assert country_iso3("E4", "usda_psd") is None


def test_psd_grains_parse_projection_rule_and_zero_flag() -> None:
    body, entry = _entry("atlas.usda_psd", "grains")
    frame, rep = psd.parse_psd(
        psd.FILES_BY_DATASET["grains"], body, SHA, last_modified=entry["headers"]["last-modified"]
    )
    v, unmapped = validate_atlas_frame(
        frame, source="usda_psd", dataset="grains", raw_sha256s=[SHA], geo_scheme="usda_psd"
    )
    assert {u["provider_geo"] for u in unmapped} == {"BM", "E4"}
    assert rep.latest_market_year["0422110"] == 2026
    rice = v.filter((pl.col("iso3") == "IND") & (pl.col("indicator") == "0422110|176")).sort("period_start")
    kinds = {r["period_start"].year: json.loads(r["flags"])["estimate_kind"] for r in rice.iter_rows(named=True)}
    assert kinds[2026] == "projection" and kinds[2025] == "estimated" and kinds[2024] == "published_history"
    f = json.loads(rice["flags"][-1])
    assert f["market_year"] == "2026/27" and f["attribute"] == "Ending Stocks" and f["record_updated"]
    assert rice["unit"][0] == "1000 MT"
    zeros = v.filter(pl.col("value").cast(pl.Float64) == 0.0)
    assert zeros.height == rep.zero_values > 0
    assert all("zero_semantics" in json.loads(x) for x in zeros["flags"].to_list())


def test_psd_refuses_layout_change() -> None:
    body, _ = _entry("atlas.usda_psd", "grains")
    zf = zipfile.ZipFile(io.BytesIO(body))
    text = zf.read(psd.FILES[0].member).decode().replace("Market_Year", "MarketYear", 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(psd.FILES[0].member, text)
    with pytest.raises(psd.PsdError):
        psd.parse_psd(psd.FILES[0], buf.getvalue(), SHA, last_modified=None)


def test_psd_oilseeds_bounded_to_listed_complexes() -> None:
    body, _ = _entry("atlas.usda_psd", "oilseeds")
    frame, rep = psd.parse_psd(psd.FILES_BY_DATASET["oilseeds"], body, SHA, last_modified=None)
    assert set(rep.commodities) <= psd.OILSEED_COMMODITIES
    assert frame.height == rep.cells > 0


# --------------------------------------------------------------------------- WASDE
def test_wasde_family_rules_and_dataset_key_registered() -> None:
    assert "usda_wasde" in FAMILIES
    assert {"wasde_csv_release_date", "wasde_csv_file_last_modified"} <= set(AVAILABILITY_RULES)
    assert known_variable("usda_wasde:world_rice_supply_and_use_milled_basis.rice.ending_stocks")
    assert "atlas.usda_wasde.wasde" in VINTAGE_DATASET_KEYS


def test_wasde_availability_is_conservative() -> None:
    rel = date(2026, 10, 9)
    a, basis, _ = wa.availability(rel, "Fri, 09 Oct 2026 16:10:12 GMT")
    assert basis == "wasde_csv_release_date" and a == datetime(2026, 10, 10, tzinfo=UTC)
    a, basis, _ = wa.availability(date(2021, 1, 12), "Fri, 19 Mar 2021 13:21:18 GMT")
    assert basis == "wasde_csv_file_last_modified" and a == datetime(2021, 3, 20, tzinfo=UTC)
    assert wa.availability(rel, None) == (None, None, None)
    assert wa.parse_release_date("01/12/2021") == date(2021, 1, 12)


def _wasde(name: str, month: tuple[int, int]) -> tuple[pl.DataFrame, wa.FileReport]:
    body, entry = _entry("atlas.usda_wasde", name)
    rows = Rows()
    rep = wa.parse_csv(
        body,
        month=month,
        file=entry["file"],
        raw_sha256=SHA,
        last_modified=entry["headers"]["last-modified"],
        rows=rows,
    )
    return rows.frame(), rep


def test_wasde_parse_projection_and_periods() -> None:
    frame, rep = _wasde("2026-10.csv", (2026, 10))
    v, _unmapped = validate_atlas_frame(frame, source="usda_wasde", dataset="wasde", raw_sha256s=[SHA])
    assert v["iso3"].null_count() == v.height  # region names are never mapped to countries
    assert rep.reliability_rows > 0 and rep.basis == "wasde_csv_release_date" and rep.release_date == "2026-10-09"
    ind = v.filter(
        (pl.col("provider_geo") == "India")
        & pl.col("indicator").str.starts_with("usda_wasde:world_rice_supply_and_use_milled_basis.rice.ending_stocks|")
    ).sort("period_start")
    got = {(r["period_start"].year, json.loads(r["flags"])["estimate_kind"]) for r in ind.iter_rows(named=True)}
    assert got == {(2024, "published_history"), (2025, "published_history"), (2026, "projection")}
    assert ind.filter(pl.col("period_start") == date(2025, 1, 1))["value"][0] == "59"
    q = v.filter(pl.col("indicator").str.contains(r"\.quarterly\|v="))
    assert q.height > 0 and set(q["period_end"].dt.month().unique().to_list()) <= {1, 4, 7, 10}
    cells = cells_from_atlas_frame(v, dataset_key="atlas.usda_wasde.wasde", table_recorded_at=None)
    assert set(cells["family"].unique().to_list()) == {"usda_wasde"}
    assert set(cells["vintage"].unique().to_list()) == {"2026-10"}


def test_wasde_refuses_wrong_report_month() -> None:
    body, entry = _entry("atlas.usda_wasde", "2026-10.csv")
    with pytest.raises(wa.WasdeError):
        wa.parse_csv(body, month=(2026, 9), file=entry["file"], raw_sha256=SHA, last_modified=None, rows=Rows())


def test_wasde_months() -> None:
    assert wa.months((2025, 11), (2026, 2)) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


# --------------------------------------------------------------------------- FAOSTAT TM
class _Run:
    """Minimal run for the byte-range part of atlas_fao_tm (no archive / register)."""

    def __init__(self) -> None:
        from reva.atlas.ingest import ArchiveRangeFixtureHttp, AtlasRun

        self.run = AtlasRun.offline(tm.atlas_source(), ArchiveRangeFixtureHttp(tm.CONNECTOR_NAME))


def _tm_frame() -> tuple[pl.DataFrame, tm.TmReport]:
    from reva.adapters.connectors.atlas_wid import content_range, inflate_member, parse_central_directory

    run = _Run().run

    async def go() -> tuple[pl.DataFrame, tm.TmReport]:
        tail = await run.get_range(tm.ARCHIVE_URL, key="t", suffix=tm.TAIL_BYTES)
        start, _e, size = content_range(tail)
        members, _ = parse_central_directory(tail.body, start)
        by = {m.name: m for m in members}
        small = {
            k: inflate_member(tail.body[by[n].offset - start :], by[n].offset, by[n])
            for k, n in tm.CODE_MEMBERS.items()
        }
        wide = by[tm.WIDE_MEMBER]
        part = await run.get_range(
            tm.ARCHIVE_URL,
            key="w",
            start=wide.offset,
            end=min(wide.offset + 30 + wide.name_len + wide.extra_len + 1024 + wide.csize - 1, size - 1),
        )
        served, _e2, _s2 = content_range(part)
        rep = tm.TmReport(inflated_bytes=wide.usize)
        frame = tm.parse_wide(tm.iter_lines(part.body, served, wide), tm.code_lists(small), SHA, rep)
        return frame, rep

    return asyncio.run(go())


def test_tm_staples_bilateral_cells() -> None:
    frame, rep = _tm_frame()
    v, _ = validate_atlas_frame(frame, source="fao_tm", dataset="tm_staples_asia", raw_sha256s=[SHA])
    assert rep.lines == 1500 and rep.rows_in_scope > 0 and rep.years[0] == 1986 and rep.years[-1] == 2024
    items = {i.split("|")[0] for i in v["indicator"].unique().to_list()}
    assert items <= tm.ITEMS
    arm = v.filter(pl.col("iso3") == "AFG")
    assert arm.height > 0
    f = json.loads(arm["flags"][0])
    assert {"flag", "flag_meaning", "partner_m49", "item_cpc"} <= set(f)
    assert all("|partner=" in i for i in arm["indicator"].unique().to_list())


def test_tm_crc_is_checked() -> None:
    from reva.adapters.connectors.atlas_wid import Member

    data = (FIX / "atlas.fao_tm" / "Trade_DetailedTradeMatrix_E_Asia_excerpt.zip").read_bytes()
    zf = zipfile.ZipFile(io.BytesIO(data))
    info = zf.getinfo(tm.WIDE_MEMBER)
    m = Member(
        info.filename,
        8,
        (info.CRC + 1) & 0xFFFFFFFF,
        info.compress_size,
        info.file_size,
        info.header_offset,
        len(info.filename),
        0,
    )
    with pytest.raises(tm.TmError):
        list(tm.iter_lines(data, 0, m))


# --------------------------------------------------------------------------- IFPRI
def _ifpri() -> tuple[list[ie.Measure], dict[str, Any], dict[str, str]]:
    ms: list[ie.Measure] = []
    defs: dict[str, Any] = {}
    shas = {"covid19": SHA, "ukrrus": SHA2}
    for t in ie.TRACKERS:
        body, _ = _entry("atlas.ifpri_er", "Covid19_0" if t.tag == "covid19" else "UKRRUS")
        got, rep = ie.parse_workbook(body, t)
        ms += got
        defs[t.tag] = rep.definitions
    return ms, defs, shas


def test_ifpri_page_licence_statements() -> None:
    body, _ = _entry("atlas.ifpri_er", "node/25")
    facts = ie.page_facts(body)
    assert facts["odc_by"].endswith("Open Data Commons Attribution License")
    assert "Creative Commons Attribution 4.0" in facts["cc_by"]


def test_ifpri_workbooks_measures_and_kg() -> None:
    ms, defs, shas = _ifpri()
    assert len(ms) == 82
    assert defs["covid19"]["StartingDate"].startswith("Starting date of the policy")
    assert "EndDate" not in defs["covid19"]  # misaligned definition row: never quoted for EndDate
    trackers = {t.tag: t for t in ie.TRACKERS}
    ents, sts, stats = ie.kg_records(ms, trackers, shas, {"VNM": "Q881"})
    assert stats["entities"] == 82 and {e.kind for e in ents} == {"export_restriction"}
    vn = [s for s in sts if s.subject == "ifpri_er:covid19:61"]
    by = {s.property: s for s in vn}
    assert by["ifpri_er:imposed_by"].object_qid == "Q881"
    assert by["P580"].value == {"type": "time", "time": "2020-04-10", "precision": 11}
    assert (by["P582"].value or {}).get("time") == "2020-04-30"
    assert by["ifpri_er:covers_hs_heading"].object_qid == "hs2017:1006"
    assert "not stated by IFPRI" in by["ifpri_er:covers_hs_heading"].qualifiers["hs_version_basis"]
    padded = [s for s in sts if s.property == "ifpri_er:covers_hs_heading" and "hs_code_repair" in s.qualifiers]
    assert padded and all(len((s.object_qid or "").split(":")[1]) == 4 for s in padded)
    assert all(s.property in set(ie.KG_PIDS) | set(ie.WIKIDATA_PIDS) for s in sts)
    assert {p.pid for p in load_predicates(ie.KG_PIDS)} == set(ie.KG_PIDS)


def test_ifpri_atlas_rows_and_licence_check() -> None:
    ms, defs, shas = _ifpri()
    frame = ie.atlas_rows(ms, {t.tag: t for t in ie.TRACKERS}, shas, defs)
    v, unmapped = validate_atlas_frame(
        frame, source="ifpri_er", dataset="measures", raw_sha256s=list(shas.values()), geo_scheme="iso3"
    )
    assert not unmapped
    r = v.filter(pl.col("indicator") == "Share_in_World_Kcal|measure=covid19:61")
    assert r.height == 1 and r["period_start"][0] == date(2020, 4, 10) and r["period_end"][0] == date(2020, 5, 1)
    assert json.loads(r["flags"][0])["definition"].startswith("Share of restricted products")
    # a workbook whose licence cell changed is refused
    body, _ = _entry("atlas.ifpri_er", "UKRRUS")
    zin = zipfile.ZipFile(io.BytesIO(body))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n == "xl/sharedStrings.xml":
                data = data.replace(b"License: ODC-By", b"License: CC BY-NC")
            zout.writestr(n, data)
    with pytest.raises(ie.IfpriError):
        ie.parse_workbook(buf.getvalue(), ie.TRACKERS[1])
    assert ie.excel_date("43931") == date(2020, 4, 10)
