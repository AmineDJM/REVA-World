"""UN atlas layer (agent A-UN): offline parsing of recorded excerpts of the real provider files.

Fixtures (``tests/fixtures/source/atlas.un_*``, ``atlas.unhcr``) are excerpts of the bytes
retrieved live on 2026-10-15; each manifest entry says what was kept (``excerpt``) and the sha256
of the full file it was cut from.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import zipfile
from datetime import date

import polars as pl
import pytest

from reva.adapters.connectors import atlas_un as U
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.kernel.ids import sha256_bytes


def _fetch(connector: str, items):
    http = FixtureHttp(connector)

    async def go():
        return {it.key: (await U.fetch(it, http)).model_copy(update={"raw_sha256": None}) for it in items}

    return asyncio.run(go())


def _validated(plan: U.TablePlan) -> pl.DataFrame:
    frame, _ = validate_atlas_frame(
        plan.frame, source=plan.source, dataset=plan.dataset, raw_sha256s=plan.raw_sha256s, geo_scheme=plan.geo_scheme
    )
    return frame


def _flags(row: dict) -> dict:
    return json.loads(row["flags"])


# --------------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("text", "out"),
    [
        ("12.5", ("12.5", False)),
        ("-0.25", ("-0.25", False)),
        ("3e-4", ("3e-4", False)),
        ("7,021.73", ("7021.73", True)),
        ("5.", ("5", True)),
        (".5", ("0.5", True)),
        ("+4", ("4", True)),
        ("007", ("7", True)),
        (" 12 ", ("12", True)),
        (">95", (None, False)),
        ("<0.1", (None, False)),
        ("NaN", (None, False)),
        ("..", (None, False)),
        ("", (None, False)),
        ("1,00", (None, False)),
    ],
)
def test_strict_decimal(text, out):
    assert U.strict_decimal(text) == out


def test_gunzip_bounded_multi_member_and_limits():
    body = gzip.compress(b"abc", mtime=0) + gzip.compress(b"def", mtime=0)
    assert U.gunzip_bounded(body, 100) == b"abcdef"
    with pytest.raises(SourceResponseError) as e:
        U.gunzip_bounded(gzip.compress(b"x" * 10_000, mtime=0), 1000)
    assert e.value.details["kind"] == "decompression_limit"
    with pytest.raises(SourceResponseError) as e:
        U.gunzip_bounded(gzip.compress(b"x" * 1000, mtime=0)[:-12], 10_000)
    assert e.value.details["kind"] == "decoding_error"
    with pytest.raises(SourceResponseError):
        U.gunzip_bounded(gzip.compress(b"abc", mtime=0) + b"garbage", 100)


def test_geo_aliases_are_evidence_backed_and_minimal():
    assert country_iso3("412", U.GEO_SCHEME) == "XKX"  # WPP Kosovo (ISO3_code XKX in the WPP row)
    assert country_iso3("830", U.GEO_SCHEME) == "CHI"  # IMS Channel Islands
    assert country_iso3("250", U.GEO_SCHEME) == "FRA"
    assert country_iso3("772", U.GEO_SCHEME) == "TKL"  # territory without a World Bank code
    assert country_iso3("158", U.GEO_SCHEME) is None  # Taiwan: not a crosswalk entity, kept unmapped
    assert country_iso3("900", U.GEO_SCHEME) is None  # World aggregate


def test_html_body_is_refused():
    with pytest.raises(SourceResponseError):
        U.read_wpp_csv(b"<!DOCTYPE html><html>blocked</html>", file="x.csv.gz")


# --------------------------------------------------------------------------- WPP
@pytest.fixture(scope="module")
def wpp_raws():
    return _fetch(U.WPP_CONNECTOR.name, U.WPP_CONNECTOR.plan())


@pytest.fixture(scope="module")
def wpp_demo(wpp_raws):
    raw = wpp_raws[U.WPP_DI.key]
    plan = U.build_wpp_demo(raw.body, sha256_bytes(raw.body))
    return plan, _validated(plan)


def test_wpp_demo_estimates_and_projections_are_separate_series(wpp_demo):
    plan, f = wpp_demo
    fra = f.filter(pl.col("iso3") == "FRA")
    est = fra.filter(pl.col("indicator") == "TFR")
    proj = fra.filter(pl.col("indicator") == "TFR|proj=medium")
    assert est["period_start"].min() == date(1950, 1, 1) and est["period_start"].max() == date(2023, 1, 1)
    assert proj["period_start"].min() == date(2024, 1, 1) and proj["period_start"].max() == date(2100, 1, 1)
    assert all(_flags(r)["projection"] is False and _flags(r)["estimate"] is True for r in est.to_dicts())
    assert all(_flags(r)["projection"] is True and _flags(r)["variant"] == "Medium" for r in proj.to_dicts())
    # concept hints: estimates and projections never share a concept
    assert set(est["concept_id"]) == {"un.wpp.total_fertility_rate"}
    assert set(proj["concept_id"]) == {"un.wpp.total_fertility_rate_proj_medium"}
    assert est["unit"].unique().to_list() == ["live births per woman"]
    assert plan.report["unused_columns"] == []


def test_wpp_demo_values_trace_back_to_the_raw_cell(wpp_raws, wpp_demo):
    _, f = wpp_demo
    text = gzip.decompress(wpp_raws[U.WPP_DI.key].body).decode("utf-8-sig")
    records = list(csv.DictReader(io.StringIO(text)))
    for row in f.filter(pl.col("iso3").is_in(["FRA", "TKL"])).sample(50, seed=1).to_dicts():
        sel = json.loads(row["selector"])
        rec = records[sel["record"]]
        assert rec[sel["column"]] == row["value"]
        assert rec["LocID"] == row["provider_geo"] and int(rec["Time"]) == row["period_start"].year


def test_wpp_geographies_kept_and_mapped(wpp_demo):
    _, f = wpp_demo
    geo = dict(f.select("provider_geo", "iso3").unique().iter_rows())
    assert geo["250"] == "FRA" and geo["412"] == "XKX" and geo["772"] == "TKL"
    assert geo["158"] is None and geo["900"] is None and geo["1834"] is None  # kept, unmapped
    tw = f.filter((pl.col("provider_geo") == "158") & (pl.col("indicator") == "TPopulation1July")).to_dicts()[0]
    assert _flags(tw)["geo_label"] == "China, Taiwan Province of China"
    world = f.filter(pl.col("provider_geo") == "900").to_dicts()[0]
    assert _flags(world)["loc_type"] == "World"


def test_wpp_variants_are_all_projections_without_medium(wpp_raws):
    raw = wpp_raws[U.WPP_VARIANTS.key]
    plan = U.build_wpp_variants(raw.body, sha256_bytes(raw.body))
    f = _validated(plan)
    assert f["period_start"].min() >= date(2024, 1, 1)
    assert not f["indicator"].str.contains("variant=Medium$").any()
    assert plan.report["medium_rows_skipped_duplicate_of_demo"] > 0
    high = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "PopTotal|variant=High"))
    assert high.height == 3 and set(high["concept_id"]) == {"un.wpp.population_proj_high"}
    pi = f.filter(pl.col("indicator") == "PopTotal|variant=Lower 95 PI").to_dicts()[0]
    assert _flags(pi)["probabilistic_interval"] is True and _flags(pi)["projection"] is True


def test_wpp_age5_split_in_estimate_and_projection_tables(wpp_raws):
    raw = wpp_raws[U.WPP_AGE5.key]
    est, proj = list(U.build_wpp_age5(raw.body, sha256_bytes(raw.body)))
    fe, fp = _validated(est), _validated(proj)
    assert est.dataset == "wpp2024_age5" and proj.dataset == "wpp2024_age5_proj"
    assert fe["period_start"].max() == date(2023, 1, 1) and fp["period_start"].min() == date(2024, 1, 1)
    assert fp["indicator"].str.ends_with("|proj=medium").all()
    assert not fe["indicator"].str.contains("proj=").any()
    row = fe.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "PopTotal|age=100+")).to_dicts()[0]
    assert row["concept_id"] == "un.wpp.population_age_100" and _flags(row)["sex"] == "both sexes"
    # 21 age groups x 3 sexes for each location-year
    n = fe.filter((pl.col("iso3") == "FRA") & (pl.col("period_start") == date(1950, 1, 1))).height
    assert n == 63


def test_wpp_schema_drift_is_typed():
    body = gzip.compress(
        b"SortOrder,LocID,Notes,ISO3_code,ISO2_code,SDMX_code,LocTypeID,LocTypeName,ParentID,"
        b"Location,VarID,Variant,Time,TFR\n,250,,FRA,FR,250,4,Country/Area,1,France,2,Medium,2020,1.8\n"
    )
    with pytest.raises(SourceResponseError) as e:
        U.build_wpp_demo(body, "0" * 64)
    assert e.value.details["kind"] == "schema_drift"


# --------------------------------------------------------------------------- IMS
@pytest.fixture(scope="module")
def ims():
    raw = _fetch(U.IMS_CONNECTOR.name, U.IMS_CONNECTOR.plan())["ims:table1"]
    plan = U.build_ims(raw.body, sha256_bytes(raw.body))
    return raw, plan, _validated(plan)


def test_ims_bilateral_cells(ims):
    _, plan, f = ims
    assert plan.report["years"] == [1990, 1995, 2000, 2005, 2010, 2015, 2020, 2024]
    alg = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "migrant_stock|origin=12|sex=T"))
    assert alg.height == 8
    row = alg.filter(pl.col("period_start") == date(2024, 1, 1)).to_dicts()[0]
    fl = _flags(row)
    assert fl["counterpart_iso3"] == "DZA" and fl["counterpart_role"] == "origin"
    assert fl["time_reference"] == "as of 1 July" and row["unit"] == "persons"
    assert "data_type" in fl
    # sexes: both = males + females for this cell as published (no derivation, just a sanity check)
    by_sex = {
        s: f.filter(
            (pl.col("iso3") == "FRA")
            & (pl.col("indicator") == f"migrant_stock|origin=12|sex={s}")
            & (pl.col("period_start") == date(2024, 1, 1))
        )["value"][0]
        for s in "TMF"
    }
    assert int(by_sex["T"]) == int(by_sex["M"]) + int(by_sex["F"])


def test_ims_world_totals_and_unmapped_kept(ims):
    _, plan, f = ims
    tot = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "migrant_stock|origin=900|sex=T"))
    assert set(tot["concept_id"]) == {"un.ims.migrant_stock"}
    chi = f.filter(pl.col("provider_geo") == "830")
    assert set(chi["iso3"]) == {"CHI"}
    tw = f.filter(pl.col("indicator").str.contains("origin=158"))
    assert tw.height > 0 and _flags(tw.to_dicts()[0])["counterpart_iso3"] is None
    assert {"code": "158", "label": "China, Taiwan Province of China"} in plan.report["unmapped_origins"]
    assert f.filter(pl.col("provider_geo") == "900")["iso3"].is_null().all()


def test_ims_selector_points_to_the_cell(ims):
    raw, _, f = ims
    xl = U.Xlsx(raw.body)
    cells = {f"{c}{r}": v for r, row in xl.rows("Table 1") for c, v in row.items()}
    for row in f.sample(30, seed=2).to_dicts():
        assert cells[json.loads(row["selector"])["cell"]] == row["value"]


def test_xlsx_refuses_dtd_and_non_zip():
    with pytest.raises(SourceResponseError):
        U.Xlsx(b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><workbook/>')
        z.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
    with pytest.raises(SourceResponseError) as e:
        U.Xlsx(buf.getvalue())
    assert e.value.details["kind"] == "unsafe_xml"


def test_ims_year_block_layout_is_checked():
    header = {"A": "Index", "H": "1990", "I": "2024", "J": "1990", "K": "2024", "L": "1990", "M": "2024"}
    cols = U.ims_year_columns(header, [(8, "T"), (10, "M"), (12, "F")])
    assert cols == {
        "H": (1990, "T"),
        "I": (2024, "T"),
        "J": (1990, "M"),
        "K": (2024, "M"),
        "L": (1990, "F"),
        "M": (2024, "F"),
    }
    with pytest.raises(SourceResponseError):
        U.ims_year_columns(header, [(8, "T"), (12, "F"), (10, "M")][:2])


# --------------------------------------------------------------------------- SDG
@pytest.fixture(scope="module")
def sdg_raws():
    items = [U.sdg_series_list_item(), U.sdg_data_item(U.SDG_BY_CODE["SH_STA_MORT"], 1)]
    raws = _fetch(U.SDG_CONNECTOR.name, items)
    follow = U.sdg_follow_ups(items[1], raws[items[1].key])
    assert [i.key for i in follow] == ["sdg:SH_STA_MORT:p2"]
    raws.update(_fetch(U.SDG_CONNECTOR.name, follow))
    return raws


def test_sdg_pages_assembled_with_bounds_and_concepts(sdg_raws):
    pages = {k: r for k, r in sdg_raws.items() if "series" in r.item.params}
    plan = U.build_sdg(pages, sdg_raws["sdg:series_list"])
    f = _validated(plan)
    assert plan.report["series"]["SH_STA_MORT"]["records"] == f.height
    assert "SH_STA_MORT" not in plan.report["missing_series"]
    afg = f.filter((pl.col("iso3") == "AFG") & (pl.col("period_start") == date(2020, 1, 1))).to_dicts()[0]
    fl = _flags(afg)
    assert afg["indicator"].startswith("SH_STA_MORT|") and "Sex=FEMALE" in afg["indicator"]
    assert afg["unit"] == "PER_100000_LIVE_BIRTHS"
    assert "lower_bound" in fl and "upper_bound" in fl and fl["release"]
    # female-only series: the single-valued Sex dimension still marks the headline cell
    assert afg["concept_id"] == "un.sdg.maternal_mortality_ratio"
    # Taiwan / regions are kept unmapped when the database reports them
    assert f.filter(pl.col("provider_geo") == "1")["iso3"].is_null().all()


def test_sdg_incomplete_series_is_left_out_and_named(sdg_raws):
    pages = {k: r for k, r in sdg_raws.items() if k == "sdg:SH_STA_MORT:p1"}
    with pytest.raises(SourceResponseError):  # nothing complete -> no table
        U.build_sdg(pages, sdg_raws["sdg:series_list"])


def test_sdg_text_and_duplicate_cells_are_kept_labelled():
    def rec(v, src):
        return {
            "series": "SH_H2O_SAFE",
            "geoAreaCode": "250",
            "geoAreaName": "France",
            "timePeriodStart": 2020.0,
            "value": v,
            "valueType": "Float",
            "source": src,
            "attributes": {"Units": "PERCENT", "Nature": "C"},
            "dimensions": {"Location": "ALLAREA", "Reporting Type": "G"},
        }

    doc = {
        "size": 10000,
        "totalElements": 3,
        "totalPages": 1,
        "pageNumber": 1,
        "data": [rec(">99", "JMP"), rec("98.5", "JMP"), rec("", "JMP")],
    }
    item = U.sdg_data_item(U.SDG_BY_CODE["SH_H2O_SAFE"], 1)
    raw = U.RawAcquisition(
        item=item,
        body=json.dumps(doc).encode(),
        media_type="application/json",
        retrieved_at="2026-10-15T00:00:00Z",
        response_metadata={},
        request_fingerprint="x",
        origin="test_fixture",
    )
    plan = U.build_sdg({item.key: raw}, None)
    f = _validated(plan).sort("indicator")
    assert f["value_text"].to_list() == [">99", None]
    assert f["value"].to_list() == [None, "98.5"]
    assert f["indicator"][1].endswith("|dup=2") and _flags(f.to_dicts()[1])["duplicate_cell_in_source"] == 2
    assert plan.report["empty_values_omitted"] == 1


# --------------------------------------------------------------------------- UNHCR
@pytest.fixture(scope="module")
def unhcr_raws():
    raws = _fetch(U.UNHCR_CONNECTOR.name, [U.unhcr_years_item()])
    follow = U.unhcr_follow_ups(U.unhcr_years_item(), raws["unhcr:years"], max_year=2025)
    assert [i.key for i in follow] == [
        "unhcr:bilateral:2024:p1",
        "unhcr:bilateral:2025:p1",
        "unhcr:by_asylum:2024-2025:p1",
        "unhcr:by_origin:2024-2025:p1",
    ]
    raws.update(_fetch(U.UNHCR_CONNECTOR.name, follow))
    return raws


def test_unhcr_string_zero_is_never_a_zero(unhcr_raws):
    plans = {p.dataset: p for p in U.build_unhcr(unhcr_raws, max_year=2025)}
    assert set(plans) == {"pop_bilateral", "pop_by_asylum", "pop_by_origin"}
    bil = _validated(plans["pop_bilateral"])
    assert not (bil["value"] == "0").any()
    assert plans["pop_bilateral"].report["string_zero_omitted"] > 0
    syr_deu = bil.filter(
        (pl.col("iso3") == "DEU")
        & (pl.col("indicator") == "refugees|origin=SYR")
        & (pl.col("period_start") == date(2024, 1, 1))
    )
    assert syr_deu["value"].to_list() == ["725102"]
    fl = _flags(syr_deu.to_dicts()[0])
    assert fl["counterpart_iso3"] == "SYR" and fl["time_reference"].startswith("end of year")
    # stateless (XXA) and unknown (UNK) origins stay codes, unmapped
    assert bil.filter(pl.col("indicator").str.contains("origin=XXA")).height > 0
    org = _validated(plans["pop_by_origin"])
    assert org.filter(pl.col("provider_geo").is_in(["XXA", "UNK", "TIB"]))["iso3"].is_null().all()
    hosted = _validated(plans["pop_by_asylum"]).filter((pl.col("iso3") == "TUR") & (pl.col("indicator") == "refugees"))
    assert set(hosted["concept_id"]) == {"un.unhcr.refugees_hosted"}


def test_unhcr_incomplete_span_is_refused(unhcr_raws):
    raws = {k: r for k, r in unhcr_raws.items() if k != "unhcr:bilateral:2025:p1"}
    doc = json.loads(raws["unhcr:bilateral:2024:p1"].body)
    doc["maxPages"] = 2  # page 2 never fetched
    raws["unhcr:bilateral:2024:p1"] = raws["unhcr:bilateral:2024:p1"].model_copy(
        update={"body": json.dumps(doc).encode()}
    )
    plans = {p.dataset: p for p in U.build_unhcr(raws, max_year=2025)}
    assert plans["pop_bilateral"].frame.height == 0
    assert plans["pop_bilateral"].report["refused"] == "incomplete"


# --------------------------------------------------------------------------- SYB
def test_syb_area_cp1252_and_respelled_numbers():
    raw = _fetch(U.SYB_CONNECTOR.name, U.SYB_CONNECTOR.plan())["syb:t02"]
    plan = U.build_syb(raw.body, sha256_bytes(raw.body))
    f = _validated(plan)
    assert plan.report["encoding"] == "cp1252"
    area = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "Surface area (thousand km2)")).to_dicts()[0]
    assert area["value"] == "552" and area["unit"] == "thousand km2" and area["concept_id"] == "un.unsd.surface_area"
    world = f.filter((pl.col("provider_geo") == "1") & (pl.col("indicator") == "Surface area (thousand km2)"))
    fl = _flags(world.to_dicts()[0])
    assert world["value"][0] == "130094" and fl["provider_text"] == "130,094"
    civ = f.filter(pl.col("iso3") == "CIV").to_dicts()[0]
    assert _flags(civ)["geo_label"] == "Côte d’Ivoire"


# --------------------------------------------------------------------------- specs / catalog
def test_connector_specs_validate():
    for c in U.CONNECTORS.values():
        spec = c.describe()
        assert spec.template == "atlas_bulk" and spec.parser_version == U.PARSER_VERSION
        assert spec.source_key in {"un.wpp", "un.desa_ims", "un.sdg", "unhcr.population", "unsd.syb"}
        assert all(i.url.startswith("https://" + c.hosts[0] + "/") for i in c.plan())


def test_catalog_un_loads_and_names_produced_series():
    from reva.atlas.catalog import load_catalog

    cat = load_catalog(strict=True)
    un = [c for c in cat.concepts.values() if c.domain == "un"]
    assert len(un) >= 80
    datasets = {
        ("un_wpp", "wpp2024_demo"),
        ("un_wpp", "wpp2024_variants"),
        ("un_wpp", "wpp2024_age5"),
        ("un_desa", "ims2024"),
        ("un_sdg", "sdg_headline"),
        ("unhcr", "pop_by_asylum"),
        ("unhcr", "pop_by_origin"),
        ("unsd", "syb68_t02"),
    }
    for c in un:
        for s in c.source_order:
            if s.source.startswith("un") and s.source in {d[0] for d in datasets}:
                assert (s.source, s.dataset) in datasets, s.key
            if s.dataset == "wpp2024_demo":
                base = s.indicator.split("|")[0]
                assert base in U.WPP_DI_COLUMNS, s.key
                assert ("|proj=medium" in s.indicator) == c.id.endswith("_proj_medium"), c.id
        assert c.provider_declared
