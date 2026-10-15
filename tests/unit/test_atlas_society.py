"""A-SOCIETY atlas sources (religion, fractionalisation, death penalty, WHO homicide, TI CPI, OECD SIGI,
ParlGov): offline parsing against captured fixtures.

Fixtures (tests/fixtures/source/atlas.{rcs_dem,wrp,hief,sscp_frac,cdpd,who_violence,ti_cpi,oecd_sigi,parlgov})
are real responses captured on 2026-10-15; large files are cut to documented subsets (each manifest entry says
which rows and carries ``derived_from_sha256``), and integrity records (Dataverse MD5, OSF sha256) are the real
records with the subset's checksum substituted.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_society_common as common
from reva.adapters.connectors import atlas_society_frac as frac
from reva.adapters.connectors import atlas_society_gov as gov
from reva.adapters.connectors import atlas_society_justice as jus
from reva.adapters.connectors import atlas_society_religion as rel
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import atlas_sources
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
NAMES = (
    "atlas.rcs_dem", "atlas.wrp", "atlas.hief", "atlas.sscp_frac", "atlas.cdpd", "atlas.who_violence",
    "atlas.ti_cpi", "atlas.oecd_sigi", "atlas.parlgov",
)  # fmt: skip
MODULES = (common, frac, gov, jus, rel)


async def _bodies(name: str, items: Any) -> dict[str, bytes]:
    http = FixtureHttp(name)
    return {it.key: (await http.get(it.url, max_bytes=it.max_bytes)).body for it in items}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _validate(frame: pl.DataFrame, geo_scheme: str | None = None) -> None:
    validate_atlas_frame(
        frame,
        source=frame["source"][0],
        dataset=frame["dataset"][0],
        raw_sha256s=frame["raw_sha256"].unique().to_list(),
        geo_scheme=geo_scheme,
    )


def _cell(frame: pl.DataFrame, indicator: str, geo: str, year: int) -> dict[str, Any]:
    rows = frame.filter(
        (pl.col("indicator") == indicator)
        & (pl.col("provider_geo") == geo)
        & (pl.col("period_start").dt.year() == year)
    ).to_dicts()
    assert len(rows) == 1, (indicator, geo, year, len(rows))
    return rows[0]


# ----------------------------------------------------------------------------- platform contract
def test_sources_are_discovered_with_registered_source_keys() -> None:
    found = atlas_sources()
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for n in NAMES:
        assert n in found, n
        spec = found[n].spec
        assert spec.source_key in keys and spec.budget_key in keys
        assert spec.planned_requests >= 2 and spec.code_digest.startswith("sha256:")
        assert (REPO / "tests/fixtures/source" / n / "manifest.json").exists()


def test_modules_never_build_an_http_client() -> None:
    forbidden = ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen(", "requests.")
    for m in MODULES:
        src = Path(m.__file__).read_text()
        assert not any(f in src for f in forbidden), m.__name__


def test_fixture_manifests_document_subsets() -> None:
    for n in NAMES:
        for e in json.loads((REPO / "tests/fixtures/source" / n / "manifest.json").read_text()):
            body = (REPO / "tests/fixtures/source" / n / e["file"]).read_bytes()
            assert _sha(body) == e["sha256"]
            assert e["licence"] and e["note"]
            if e["derived_from_sha256"]:
                assert e["derived_from_sha256"] != e["sha256"]


# ----------------------------------------------------------------------------- RCS-Dem / WRP
async def test_rcs_dem_parse_and_integrity() -> None:
    b = await _bodies(rel.RCS_CONNECTOR, rel.rcs_items())
    meta = rel.check_osf(
        b["rcs.node"], b["rcs.files"], {rel.RCS_XLSX_NAME: b["rcs.xlsx"], rel.RCS_USAGE_NAME: b["rcs.usage"]}
    )
    assert meta["node_license"] is None and meta["title"] == rel.RCS_TITLE
    frame, rep = rel.parse_rcs(b["rcs.xlsx"], raw_sha256=_sha(b["rcs.xlsx"]), min_rows=1)
    _validate(frame)
    assert rep["unit_years"] == 8
    usa = _cell(frame, "CHRPC", "2", 2015)
    assert usa["iso3"] == "USA" and usa["unit"] == "%" and float(usa["value"]) > 50
    assert json.loads(usa["flags"])["rcs_abbrev"] == "USA"
    assert usa["concept_id"] == "society.religion.rcs.christians_pct"
    assert _cell(frame, "CHRPC", "347", 2015)["iso3"] == "XKX"  # RCS gives no ISO3 for Kosovo: listed alias
    assert _cell(frame, "MUSPC", "260", 1980)["iso3"] is None  # West Germany stays unmapped
    assert _cell(frame, "CHRPC", "315", 1980)["iso3"] is None  # Czechoslovakia (RCS ISO3 CSK) unmapped
    assert _cell(frame, "CHRPC", "365", 1950)["iso3"] == "RUS"  # RCS unit continuity, flagged
    maj = _cell(frame, "MAJID", "750", 2015)
    assert maj["value"] == "5000" and maj["value_text"] == "Hindus" and maj["unit"] == "code"
    # zeros are provider values (kept); blanks omitted
    assert frame.filter(pl.col("value") == "0").height > 0
    assert set(frame["indicator"]) <= {p + "PC" for p in rel.RCS_PC} | set(rel.RCS_EXTRA) | set(rel.RCS_CODE_VARS)


async def test_rcs_dem_refuses_checksum_or_licence_drift() -> None:
    b = await _bodies(rel.RCS_CONNECTOR, rel.rcs_items())
    with pytest.raises(SourceResponseError, match="sha256"):
        rel.check_osf(b["rcs.node"], b["rcs.files"], {rel.RCS_XLSX_NAME: b["rcs.xlsx"] + b"x"})
    node = json.loads(b["rcs.node"])
    node["data"]["attributes"]["node_license"] = {"id": "x"}
    with pytest.raises(SourceResponseError, match="licence"):
        rel.check_osf(json.dumps(node).encode(), b["rcs.files"], {})


async def test_wrp_parse_codes_and_shares() -> None:
    from reva.adapters.connectors.atlas_cow import CodeResolver, load_crosswalk

    b = await _bodies(rel.WRP_CONNECTOR, rel.wrp_items())
    resolver = CodeResolver(load_crosswalk(b["countrycode"]))
    frame, rep = rel.parse_wrp(b["wrp.national"], raw_sha256=_sha(b["wrp.national"]), resolver=resolver, min_rows=1)
    _validate(frame)
    assert rep["rows"] == 8
    share = _cell(frame, "chrstgenpct", "2", 2010)
    assert share["iso3"] == "USA" and share["unit"] == "proportion (0-1)" and 0 < float(share["value"]) < 1
    f = json.loads(share["flags"])
    assert f["datatype_label"] is None or isinstance(f["datatype_label"], str)
    assert "reliabilevel" in f and f["terms"].startswith("cite")
    assert _cell(frame, "pop", "2", 2010)["unit"] == "persons"
    assert _cell(frame, "islmgenpct", "260", 1980)["iso3"] is None  # West Germany
    assert _cell(frame, "islmgenpct", "365", 1980)["iso3"] is None  # USSR
    assert _cell(frame, "islmgenpct", "365", 1995)["iso3"] == "RUS"
    assert _cell(frame, "judconspct", "2", 2010)["unit"] == "proportion (0-1)"  # file spells it judconspct
    assert "totpct" not in set(frame["indicator"])


# ----------------------------------------------------------------------------- fractionalisation
async def test_hief_windows_and_aliases() -> None:
    b = await _bodies(frac.HIEF_CONNECTOR, frac.hief_items())
    meta = common.check_dataverse_record(
        b["hief.record"],
        doi=frac.HIEF_DOI,
        files={frac.HIEF_FILE_ID: (frac.HIEF_FILE, b["hief.data"])},
        licences=("CC0 1.0",),
    )
    assert meta["licence"] == "CC0 1.0"
    frame, _rep = frac.parse_hief(b["hief.data"], raw_sha256=_sha(b["hief.data"]), min_rows=1)
    _validate(frame)
    assert _cell(frame, "EFindex", "German Federal Republic", 1990)["iso3"] is None
    assert _cell(frame, "EFindex", "German Federal Republic", 1991)["iso3"] == "DEU"
    assert _cell(frame, "EFindex", "German Democratic Republic", 1990)["iso3"] is None
    assert _cell(frame, "EFindex", "Serbia", 2006)["iso3"] is None
    assert _cell(frame, "EFindex", "Serbia", 2007)["iso3"] == "SRB"
    assert _cell(frame, "EFindex", "Iran", 2013)["iso3"] == "IRN"
    assert _cell(frame, "EFindex", "Taiwan", 2013)["iso3"] == "TWN"
    assert _cell(frame, "EFindex", "USSR", 1990)["iso3"] is None
    fr = _cell(frame, "EFindex", "United Kingdom", 2013)
    assert (
        fr["iso3"] == "GBR"
        and fr["unit"] == "index (0-1)"
        and fr["concept_id"] == "society.fractionalization.ethnic_hief"
    )


async def test_dataverse_record_refuses_licence_and_md5_drift() -> None:
    b = await _bodies(frac.HIEF_CONNECTOR, frac.hief_items())
    files = {frac.HIEF_FILE_ID: (frac.HIEF_FILE, b["hief.data"])}
    with pytest.raises(SourceResponseError, match="licence"):
        common.check_dataverse_record(b["hief.record"], doi=frac.HIEF_DOI, files=files, licences=("CC BY 4.0",))
    with pytest.raises(SourceResponseError, match="MD5"):
        common.check_dataverse_record(
            b["hief.record"],
            doi=frac.HIEF_DOI,
            files={frac.HIEF_FILE_ID: (frac.HIEF_FILE, b"x")},
            licences=("CC0 1.0",),
        )
    with pytest.raises(SourceResponseError, match="not in the latest"):
        common.check_dataverse_record(b["hief.record"], doi=frac.HIEF_DOI, files={1: ("x", b"")}, licences=("CC0 1.0",))


async def test_sscp_origins_periods_and_geo() -> None:
    from reva.atlas.tables import country_iso3

    b = await _bodies(frac.SSCP_CONNECTOR, frac.sscp_items())
    common.check_dataverse_record(
        b["sscp.record"],
        doi=frac.SSCP_DOI,
        files={
            frac.SSCP_FILE_ID: (frac.SSCP_FILE, b["sscp.data"]),
            frac.SSCP_README_ID: (frac.SSCP_README, b["sscp.readme"]),
        },
        licences=("CC BY 4.0",),
    )
    frac.check_sscp_readme(b["sscp.readme"])
    with pytest.raises(SourceResponseError):
        frac.check_sscp_readme(b"no statements")
    frame, rep = frac.parse_sscp(b["sscp.data"], raw_sha256=_sha(b["sscp.data"]), min_rows=1)
    frame = frame.with_columns(
        pl.col("provider_geo")
        .map_elements(lambda g: country_iso3(g, frac.SSCP_SCHEME), return_dtype=pl.String)
        .alias("iso3")
    )
    _validate(frame, frac.SSCP_SCHEME)
    afg = _cell(frame, "ethnic_frac", "4", 2003)
    assert json.loads(afg["flags"])["value_origin"] == "alesina_2003" and afg["iso3"] == "AFG"
    asm = _cell(frame, "ethnic_frac", "16", 2026)  # American Samoa: CIA-based SSCP value, release year
    assert "SSCP v1.0 release year" in json.loads(asm["flags"])["period_basis"]
    kos = _cell(frame, "religion_frac", "689", 2015)  # RCS-Dem 2015 cross-section
    assert kos["iso3"] == "XKX"
    assert _cell(frame, "religion_frac", "830", 2015)["iso3"] == "CHI"
    assert _cell(frame, "ethnic_frac", "158", 2003)["iso3"] == "TWN"  # ISO numeric 158 (crosswalk iso_numeric)
    assert "530" in rep["unmapped"] and "158" not in rep["unmapped"]
    assert frame.filter(pl.col("provider_geo") == "275").height == 1  # West Bank and Gaza: religion only


# ----------------------------------------------------------------------------- justice
async def test_cdpd_status_codes_and_names() -> None:
    from reva.atlas.tables import country_iso3

    b = await _bodies(jus.CDPD_CONNECTOR, jus.cdpd_items())
    common.check_dataverse_record(
        b["cdpd.record"],
        doi=jus.CDPD_DOI,
        files={jus.CDPD_FILE_ID: (jus.CDPD_FILE, b["cdpd.data"])},
        licences=("CC0 1.0",),
    )
    frame, rep = jus.parse_cdpd(b["cdpd.data"], raw_sha256=_sha(b["cdpd.data"]), min_rows=1)
    _validate(frame, jus.CDPD_SCHEME)
    assert rep["rows"] == 10
    f80, f81 = _cell(frame, "Deathpenalty", "FRANCE", 1980), _cell(frame, "Deathpenalty", "FRANCE", 1981)
    assert (f80["value"], f81["value"], f81["value_text"]) == ("4", "0", "Abolished")
    assert json.loads(f81["flags"])["flag_change"] == "1"
    assert f81["iso3"] == "FRA" and f81["concept_id"] == "society.death_penalty_status"
    assert country_iso3("KYRGYZTAN", jus.CDPD_SCHEME) == "KGZ"
    assert country_iso3("USSR", jus.CDPD_SCHEME) is None
    assert country_iso3("GERMANY WEST", jus.CDPD_SCHEME) is None
    assert country_iso3("TAIWAN", jus.CDPD_SCHEME) == "TWN"


async def test_who_homicide_estimates() -> None:
    items = jus.who_items(jus.WHOV_FIXTURE_COUNTRIES)
    b = await _bodies(jus.WHOV_CONNECTOR, items)
    frame, rep = jus.who_frame(b, {k: _sha(v) for k, v in b.items()})
    _validate(frame, "iso3")
    assert set(frame["dataset"]) == {"gho_violence"} and set(frame["source"]) == {"who"}
    assert set(frame["provider_geo"]) == set(jus.WHOV_FIXTURE_COUNTRIES)
    fra = frame.filter(pl.col("indicator") == "VIOLENCE_HOMICIDERATE|sex=SEX_BTSX", pl.col("provider_geo") == "FRA")
    assert fra.height > 10 and set(fra["unit"]) == {"per 100 000 population"}
    assert set(fra["concept_id"]) == {"society.homicide_rate_who"}
    f = json.loads(fra["flags"][0])
    assert (
        f["producer"] == "WHO Global Health Estimates (GHE)"
        and "low" in f
        and f["indicator_name"].startswith("Estimates")
    )
    assert rep["rows"]["VIOLENCE_HOMICIDENUM"] > 0


# ----------------------------------------------------------------------------- governance
async def test_cpi_strict_ooxml_verbatim_values() -> None:
    from reva.atlas.tables import country_iso3

    b = await _bodies(gov.CPI_CONNECTOR, gov.cpi_items())
    gov.check_cpi_page(b["cpi.page"])
    with pytest.raises(SourceResponseError, match="licence"):
        gov.check_cpi_page(b"<html>all rights reserved</html>")
    import io
    import zipfile

    assert b"purl.oclc.org/ooxml" in zipfile.ZipFile(io.BytesIO(b["cpi.xlsx"])).read("xl/workbook.xml")  # strict OOXML
    frame, rep = gov.parse_cpi(b["cpi.xlsx"], raw_sha256=_sha(b["cpi.xlsx"]), min_rows=1)
    _validate(frame, gov.CPI_SCHEME)
    assert rep["years"] == [2012, 2025] and rep["latest_sheet_rows_checked"] == 5
    dnk = _cell(frame, "cpi_score", "DNK", 2025)
    assert dnk["value"] == "89" and dnk["concept_id"] == "society.cpi_score"
    se = _cell(frame, "standard_error", "DNK", 2025)["value"]
    assert re.fullmatch(r"\d+\.\d+", se)  # binary-float text kept as stored, never rounded by REVA
    assert country_iso3("KSV", gov.CPI_SCHEME) == "XKX" and country_iso3("TWN", gov.CPI_SCHEME) == "TWN"
    assert rep["unmapped"] == {}
    assert not any("Bertelsmann" in i or "Economist" in i for i in frame["indicator"].unique())


async def test_cpi_sheets_must_agree() -> None:
    b = await _bodies(gov.CPI_CONNECTOR, gov.cpi_items())
    import io
    import zipfile

    zin = zipfile.ZipFile(io.BytesIO(b["cpi.xlsx"]))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":  # sheet CPI2025: change Denmark's 89 to 88
                data = data.replace(b"<v>89</v>", b"<v>88</v>", 1)
            zout.writestr(info, data)
    with pytest.raises(SourceResponseError, match="disagree"):
        gov.parse_cpi(out.getvalue(), raw_sha256="0" * 64, min_rows=1)


async def test_sigi_scores_and_legal_variables() -> None:
    from reva.atlas.tables import country_iso3

    b = await _bodies(gov.SIGI_CONNECTOR, gov.sigi_items())
    frame, rep = gov.parse_sigi(b, {k: _sha(v) for k, v in b.items()}, min_rows={})
    _validate(frame, gov.SIGI_SCHEME)
    assert rep["gid_non_law_skipped"] > 0  # survey variables (third-party data) are not ingested
    assert not frame.filter(pl.col("indicator").str.ends_with("_PCT_1")).height
    fra = _cell(frame, "SIGI", "FRA", 2023)
    assert fra["unit"] == "0-100 scale" and fra["concept_id"] == "society.gender.sigi.score"
    assert _cell(frame, "DF_CM_LAW", "NGA", 2023)["concept_id"] == "society.gender.sigi.law.child_marriage"
    assert country_iso3("XKV", gov.SIGI_SCHEME) == "XKX" and country_iso3("W", gov.SIGI_SCHEME) is None
    assert frame.filter(pl.col("value").is_null() & pl.col("value_text").is_null()).height == 0
    assert rep.get("sigi_empty_obs", 0) + rep.get("gid_empty_obs", 0) > 0  # empty observations omitted, counted


async def test_parlgov_elections() -> None:
    b = await _bodies(gov.PG_CONNECTOR, gov.parlgov_items())
    common.check_dataverse_record(
        b["parlgov.record"],
        doi=gov.PG_DOI,
        files={gov.PG_FILE_ID: (gov.PG_FILE, b["parlgov.election"])},
        licences=("CC0 1.0",),
    )
    frame, rep = gov.parse_parlgov(b["parlgov.election"], raw_sha256=_sha(b["parlgov.election"]), min_rows=1)
    _validate(frame, "iso3")
    assert rep["elections"] == 3 and rep["countries"] == 3
    tot = frame.filter(pl.col("indicator") == "seats_total|election=parliament", pl.col("provider_geo") == "DEU")
    assert tot.height == 1 and tot["period_start"][0].isoformat() == "2021-09-26"
    assert (tot["period_end"][0] - tot["period_start"][0]).days == 1
    vs = frame.filter(pl.col("indicator").str.starts_with("vote_share|election=ep|"), pl.col("provider_geo") == "FRA")
    assert vs.height > 3 and set(vs["unit"]) == {"% of votes"}
    assert "party_name" in json.loads(vs["flags"][0])


# ----------------------------------------------------------------------------- catalog
def test_society_catalog_is_valid_and_points_at_our_tables() -> None:
    cat = load_catalog()
    doc = yaml.safe_load((REPO / "registry/atlas/catalog/society.yaml").read_text())
    ours = {
        "rcs.dem2",
        "wrp.national",
        "hief.hief",
        "sscp.frac_v1",
        "cdpd.cdpd_v1",
        "who.gho_violence",
        "ti.cpi",
        "oecd.sigi2023",
    }
    assert doc["domain"] == "society"
    for c in doc["concepts"]:
        assert c["provider_declared"] is True and c["id"].startswith("society.")
        assert c["id"] in cat.concepts
        for s in c["source_order"]:
            src, ds, ind = s.split(".", 2)
            assert f"{src}.{ds}" in ours, s
            assert ind


def test_catalog_units_match_connector_units() -> None:
    doc = yaml.safe_load((REPO / "registry/atlas/catalog/society.yaml").read_text())
    for c in doc["concepts"]:
        for s in c["source_order"]:
            src, ds, ind = s.split(".", 2)
            if f"{src}.{ds}" == "rcs.dem2":
                want = "%" if ind.endswith("PC") else ("code" if ind in rel.RCS_CODE_VARS else rel.RCS_EXTRA[ind])
                assert c["unit"] == want, c["id"]
            if f"{src}.{ds}" == "wrp.national":
                assert c["unit"] == rel.SHARE_UNIT and ind in rel.WRP_PCTS
            if f"{src}.{ds}" == "who.gho_violence":
                code = ind.split("|")[0]
                assert c["unit"] == {i.code: i.unit for i in jus.who_indicators()}[code]
            if f"{src}.{ds}" == "ti.cpi":
                assert c["unit"] == {v[0]: v[1] for v in gov.CPI_COLS.values()}[ind]
