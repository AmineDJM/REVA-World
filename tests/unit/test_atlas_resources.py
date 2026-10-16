"""Offline tests of the A-RESOURCES atlas connectors: USGS minerals (MCS + Minerals Yearbook vol. III) and EIA
International Energy Data.

Fixtures: ``tests/fixtures/source/atlas.usgs_minerals/`` (whole ScienceBase item JSONs and FGDC metadata; verbatim
record excerpts of the three live CSVs of 2026-10-15) and ``tests/fixtures/source/atlas.eia_intl/`` (whole bulk
manifest and footnotes page; ``INTL.zip`` re-zipped with verbatim lines of the live ``INTL.txt``). Each manifest
entry records the live sha256/size and what the excerpt holds.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_eia, atlas_usgs
from reva.adapters.connectors.atlas_eia import EiaParseError, parse_bulk, parse_footnotes, parse_manifest, table_of
from reva.adapters.connectors.atlas_usgs import (
    RELEASES,
    RELEASES_BY_DATASET,
    UsgsParseError,
    bounded_indicator,
    disambiguate,
    licence_ok,
    parse_item,
    parse_metadata,
    parse_release,
)
from reva.atlas.ingest import AtlasRun
from reva.atlas.sources import MAX_ATLAS_RESPONSE_BYTES, discover
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source"


def _get(conn: str, url: str) -> bytes:
    run = AtlasRun.offline(atlas_usgs.atlas_source() if "usgs" in conn else atlas_eia.atlas_source(), FixtureHttp(conn))
    raw = asyncio.run(run.get(url, key="t"))
    body: bytes = raw.body
    return body


# ----------------------------------------------------------------------------- USGS helpers
_USGS: dict[str, tuple[pl.DataFrame, Any, list[dict[str, Any]], dict[str, Any]]] = {}


def _usgs(ds: str) -> tuple[pl.DataFrame, Any, list[dict[str, Any]], dict[str, Any]]:
    if ds not in _USGS:
        rel = RELEASES_BY_DATASET[ds]
        item = parse_item(_get("atlas.usgs_minerals", rel.item_url), rel)
        meta = parse_metadata(_get("atlas.usgs_minerals", item["files"][rel.meta_file]["url"]))
        assert licence_ok(item, meta)
        body = _get("atlas.usgs_minerals", item["files"][rel.data_file]["url"])
        sha = hashlib.sha256(body).hexdigest()
        f, rep = parse_release(rel, body, sha, meta)
        v, unmapped = validate_atlas_frame(f, source="usgs", dataset=ds, raw_sha256s=[sha], geo_scheme="usgs_name")
        _USGS[ds] = (v, rep, unmapped, meta)
    return _USGS[ds]


def _cell(f: pl.DataFrame, **kw: Any) -> dict[str, Any]:
    q = f
    for k, v in kw.items():
        q = q.filter(pl.col(k) == v)
    assert q.height == 1, (kw, q.height)
    return q.row(0, named=True)


LI_PROD = "Lithium|World Mine Production and Reserves|Production|Mine production|unit=metric tons"
LI_RES = "Lithium|World Mine Production and Reserves|Reserves|Reserves|unit=metric tons"


# ----------------------------------------------------------------------------- specs / registry
def test_specs_are_discovered_and_bounded() -> None:
    sources, errors = discover(["reva.adapters.connectors.atlas_usgs", "reva.adapters.connectors.atlas_eia"])
    assert errors == [] and {"atlas.usgs_minerals", "atlas.eia_intl"} <= set(sources)
    u, e = atlas_usgs.atlas_source(), atlas_eia.atlas_source()
    assert u.allowed_hosts == ("www.sciencebase.gov",) and e.allowed_hosts == ("www.eia.gov",)
    assert set(u.datasets) == {"mcs2026", "myb2024", "myb2019"} and u.planned_requests == 9
    assert set(e.datasets) == {"intl_petroleum", "intl_gas", "intl_coal", "intl_electricity", "intl_energy"}
    assert e.planned_requests == 3
    assert max(u.max_response_bytes, e.max_response_bytes) <= MAX_ATLAS_RESPONSE_BYTES


def test_registry_files_and_budget_request() -> None:
    reg = ROOT / "registry"
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (reg / "sources").glob("*.yaml")}
    assert {"usgs.sciencebase", "eia.opendata_bulk"} <= keys
    req = yaml.safe_load((reg / "atlas" / "budget_requests" / "a_resources.yaml").read_text())
    by = {r["source_key"]: r for r in req["requests"]}
    assert by["usgs.sciencebase"]["daily_requests"] >= atlas_usgs.atlas_source().planned_requests
    assert by["eia.opendata_bulk"]["daily_requests"] >= atlas_eia.atlas_source().planned_requests


def test_fixture_manifests_match_their_bytes() -> None:
    for conn in ("atlas.usgs_minerals", "atlas.eia_intl"):
        for e in json.loads((FIX / conn / "manifest.json").read_text()):
            body = (FIX / conn / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            assert e["request_fingerprint"] == _fixture_fingerprint(e["url"])
            assert len(body) < 400_000


# ----------------------------------------------------------------------------- USGS
@pytest.mark.parametrize("ds", [r.dataset for r in RELEASES])
def test_every_usgs_release_parses_into_a_valid_atlas_table(ds: str) -> None:
    f, rep, _unmapped, _meta = _usgs(ds)
    assert f.height == rep.cells > 0
    assert f["iso3"].drop_nulls().n_unique() >= 5
    assert f["indicator"].str.len_chars().max() <= 200  # type: ignore[operator]
    for flags in f["flags"].unique().to_list():
        json.loads(flags)
    sel = json.loads(f["selector"][0])
    assert sel["file"] == RELEASES_BY_DATASET[ds].data_file and sel["record"] >= 1


def test_item_file_list_and_licence_are_checked() -> None:
    rel = RELEASES_BY_DATASET["mcs2026"]
    body = _get("atlas.usgs_minerals", rel.item_url)
    item = parse_item(body, rel)
    assert item["files"][rel.data_file]["url"].startswith("https://www.sciencebase.gov/catalog/file/get/")
    doc = json.loads(body)
    doc["files"] = [x for x in doc["files"] if x["name"] != rel.data_file]
    with pytest.raises(UsgsParseError, match="has no file"):
        parse_item(json.dumps(doc).encode(), rel)
    with pytest.raises(UsgsParseError, match="item id"):
        parse_item(json.dumps({**json.loads(body), "id": "x"}).encode(), rel)
    assert not licence_ok({"rights": "All rights reserved"}, {"use_constraints": "Contact the author"})
    assert licence_ok({"rights": "This work is marked with Creative Commons Zero v1.0"}, {})


def test_mcs_value_codes_come_from_the_archived_metadata() -> None:
    _f, _rep, _u, meta = _usgs("mcs2026")
    codes = meta["value_codes"]
    assert codes["W"] == "Withheld" and codes["—"] == "zero" and codes["NA"] == "not available"
    assert codes["E"] == "Net exporter" and codes["s"] == "small" and codes["XX"].startswith("World totals cannot")
    assert _usgs("myb2024")[3]["value_codes"]["-999"] == "Value is not known or not available."


def test_mcs_world_production_and_reserves_by_country() -> None:
    f, _rep, unmapped, _ = _usgs("mcs2026")
    chl = _cell(f, indicator=LI_PROD, iso3="CHL", period_start=date(2025, 1, 1))
    assert chl["value"] == "56000" and chl["unit"] == "metric tons"  # "56,000": thousands separator removed only
    assert json.loads(chl["flags"])["estimate_kind"] == "estimated"
    assert _cell(f, indicator=LI_PROD, iso3="CHL", period_start=date(2024, 1, 1))["value"] == "48900"
    assert _cell(f, indicator=LI_RES, iso3="CHL", period_start=date(2025, 1, 1))["value"] == "9200000"
    world = _cell(f, indicator=LI_RES.replace("|Reserves|unit", "|Reserves: rounded|unit"), provider_geo="World total")
    assert world["iso3"] is None and world["value"] == "37000000"
    um = {u["provider_geo"] for u in unmapped}
    assert {"World total", "Other countries"} <= um
    assert country_iso3("Burma", "usgs_name") == "MMR" and country_iso3("Congo (Kinshasa)", "usgs_name") == "COD"
    assert country_iso3("Côte d’Ivoire", "usgs_name") == "CIV"


def test_withheld_is_never_a_zero_and_the_usgs_dash_is_zero() -> None:
    f, rep, _u, _ = _usgs("mcs2026")
    us = _cell(f, indicator=LI_PROD, iso3="USA", period_start=date(2025, 1, 1))
    fl = json.loads(us["flags"])
    assert us["value"] is None and us["value_text"] is None
    assert fl["missing"] == "confidential" and fl["provider_code"] == "W" and fl["provider_meaning"] == "Withheld"
    assert rep.missing_cells["confidential"] > 0 and rep.missing_cells["not_available"] > 0
    other = _cell(f, indicator=LI_PROD, provider_geo="Other countries", period_start=date(2025, 1, 1))
    assert other["value"] == "0" and json.loads(other["flags"])["provider_code"] == "—"
    missing = f.filter(pl.col("flags").str.contains('"missing"'))
    assert missing["value"].is_null().all() and missing["value_text"].is_null().all()
    assert f.filter(pl.col("value").is_null() & pl.col("value_text").is_null())["flags"].str.contains('"missing"').all()


def test_qualitative_and_coded_entries_go_to_value_text() -> None:
    f, rep, _u, _ = _usgs("mcs2026")
    t = f.filter(pl.col("value_text").is_not_null())
    assert rep.text_cells == t.height > 0 and t["value"].is_null().all()
    e = t.filter(pl.col("value_text") == "E").row(0, named=True)
    assert json.loads(e["flags"])["provider_meaning"] == "Net exporter"
    assert {"s", "Large"} & set(t["value_text"].to_list())


def test_identical_label_blocks_are_told_apart_and_flagged() -> None:
    f, rep, _u, _ = _usgs("mcs2026")  # Mica (Natural): scrap-and-flake and sheet salient blocks
    mica = f.filter(pl.col("indicator").str.starts_with("Mica (Natural)|Salient Statistics"))
    dups = mica.filter(pl.col("indicator").str.contains(r"\|dup=[12]$"))
    assert rep.ambiguous_duplicates == dups.height > 0
    assert dups["flags"].str.contains("ambiguous_duplicate").all()
    m24, rep24, _u, _ = _usgs("myb2024")
    gold = "Gold|Gold|Mine||||Au content|unit=Kilograms|note="
    assert _cell(m24, indicator=gold + "Large scale producers.", iso3="GHA", period_start=date(2024, 1, 1))[
        "value"
    ] == ("90695")
    asm = _cell(m24, indicator=gold + "Artisanal and small-scale producers.", iso3="GHA", period_start=date(2020, 1, 1))
    assert asm["value"] == "37000" and json.loads(asm["flags"])["estimate_kind"] == "estimated"
    assert rep24.ambiguous_duplicates > 0  # North Korea graphite: two rows, nothing distinguishes them


def test_disambiguation_and_bounded_indicators() -> None:
    inds = ["a", "a", "a", "b"]
    hit = disambiguate(inds, ["X", "X", "X", "X"], ["2020", "2020", "2021", "2020"])
    assert inds == ["a|dup=1", "a|dup=2", "a|dup=1", "b"] and hit == {0, 1, 2}
    long = "x" * 250
    b = bounded_indicator(long)
    assert len(b) == 199 and b.startswith("x" * 186 + "~h=") and bounded_indicator("short") == "short"


def test_myb_tables_keep_not_available_flags_estimates_and_revisions() -> None:
    m24, rep24, unm24, _ = _usgs("myb2024")
    assert rep24.missing_cells["not_available"] > 0
    na = m24.filter(pl.col("flags").str.contains('"provider_code":"-999"'))
    assert na.height == rep24.missing_cells["not_available"] and na["value"].is_null().all()
    assert unm24 == []
    m19, rep19, unm19, _ = _usgs("myb2019")
    cu = "Copper|Mine, Cu content, Concentrates|Metals|unit=Metric tons"
    c = _cell(m19, indicator=cu, iso3="CHL", period_start=date(2015, 1, 1))
    assert c["value"] == "3993700" and c["period_end"] == date(2016, 1, 1)
    cem = _cell(m19, indicator="Cement|Hydraulic|Industrial minerals|unit=Thousand metric tons", iso3="CHL",
                period_start=date(2018, 1, 1))  # fmt: skip
    fl = json.loads(cem["flags"])
    assert fl["revised"] is True and fl["estimate_kind"] == "estimated" and fl["footnote"] == "Revised. Estimated."
    assert rep19.revised > 0 and unm19 == []
    assert country_iso3("Guadaloupe", "usgs_name") == "GLP"  # USGS spelling in the 2019 table


def test_usgs_layout_surprises_are_refused() -> None:
    rel = RELEASES_BY_DATASET["mcs2026"]
    with pytest.raises(UsgsParseError, match="missing column"):
        parse_release(rel, b"a,b\n1,2\n", "0" * 64, {"value_codes": {}})
    head = "MCS chapter,Section,Commodity,Country,Statistics,Statistics_detail,Unit,Year,Value,Notes\n"
    row = "X,World Production,X,Chile,Production,P,metric tons,20x5,1,\n"
    with pytest.raises(UsgsParseError, match="unexpected Year"):
        parse_release(rel, (head + row).encode(), "0" * 64, {"value_codes": {}})
    dash = "X,World Production,X,Chile,Production,P,metric tons,2025,—,\n"
    with pytest.raises(UsgsParseError, match="zero"):  # '—' only means zero when the metadata says so
        parse_release(rel, (head + dash).encode("cp1252"), "0" * 64, {"value_codes": {}})
    myb = RELEASES_BY_DATASET["myb2019"]
    h19 = "Country,Commodity,Commodity Product,Commodity Type,Units,2019 Production,2019 Footnotes\n"
    with pytest.raises(UsgsParseError, match="unexpected production value"):
        parse_release(myb, (h19 + "Chile,Copper,Mine,Metals,Metric tons,-5,\n").encode(), "0" * 64, {})
    ok, rep = parse_release(myb, (h19 + 'Chile,Copper,Mine,Metals,Metric tons,"1,500",\n').encode(), "0" * 64, {})
    assert ok["value"].to_list() == ["1500"] and ok["iso3"].to_list() == ["CHL"] and rep.cells == 1


# ----------------------------------------------------------------------------- EIA
_EIA: dict[str, Any] = {}


def _eia() -> tuple[dict[str, pl.DataFrame], Any, dict[str, str]]:
    if not _EIA:
        notes = parse_footnotes(_get("atlas.eia_intl", atlas_eia.FOOTNOTES_URL))
        body = _get("atlas.eia_intl", atlas_eia.ZIP_URL)
        sha = hashlib.sha256(body).hexdigest()
        frames, rep = parse_bulk(body, sha, notes)
        for ds, f in frames.items():
            v, unm = validate_atlas_frame(f, source="eia", dataset=ds, raw_sha256s=[sha], geo_scheme="eia")
            frames[ds] = v
            _EIA[f"unmapped:{ds}"] = unm
        _EIA.update(frames=frames, rep=rep, notes=notes)
    return _EIA["frames"], _EIA["rep"], _EIA["notes"]


def test_eia_manifest_and_notation_are_read_from_archived_documents() -> None:
    man = parse_manifest(_get("atlas.eia_intl", atlas_eia.MANIFEST_URL))
    assert man["accessURL"] == atlas_eia.ZIP_URL and man["last_updated"].startswith("2026-")
    with pytest.raises(EiaParseError, match="accessURL"):
        parse_manifest(json.dumps({"dataset": {"INTL": {"accessURL": "https://x/INTL.zip"}}}).encode())
    _f, _r, notes = _eia()
    assert notes["--"] == "Not applicable" and notes["NA"] == "Not available"
    assert notes["W"] == "Data withheld to avoid disclosure"
    assert notes["ie"] == "Data are included elsewhere for this time period"


def test_eia_tables_parse_and_route_by_product() -> None:
    frames, rep, _ = _eia()
    assert set(frames) == {t.dataset for t in atlas_eia.TABLES}
    assert table_of("57", "1") == "intl_petroleum" and table_of("26", "1") == "intl_gas"
    assert table_of("7", "6") == "intl_coal" and table_of("116", "7") == "intl_electricity"
    assert table_of("5", "8") == "intl_energy" and table_of("9999", "1") == "intl_energy"
    for ds, f in frames.items():
        assert f["indicator"].str.contains(r"^INTL\.[0-9]+-[0-9]+-[A-Z0-9]+\.A$").all(), ds
        assert not f["unit"].is_in(["terajoules", "million metric tons of oil equivalent"]).any()
    assert rep.excluded_unit == {"MTOE": 18, "TJ": 18} and rep.excluded_product == {"4701": 10}
    assert rep.non_annual == 2 and rep.unknown_codes == {}


def test_eia_values_are_verbatim_and_units_as_published() -> None:
    frames, _rep, _ = _eia()
    p = frames["intl_petroleum"]
    w = _cell(p, indicator="INTL.57-1-TBPD.A", provider_geo="WORL", period_start=date(2024, 1, 1))
    assert w["value"] == "81949.4481623602" and w["unit"] == "thousand barrels per day" and w["iso3"] is None
    sau = _cell(p, indicator="INTL.57-1-TBPD.A", iso3="SAU", period_start=date(2024, 1, 1))
    assert sau["value"] == "9232.154456284154"
    assert json.loads(sau["flags"])["series_last_updated"].startswith("20")
    oecd = _cell(p, indicator="INTL.57-1-TBPD.A", provider_geo="OECD", period_start=date(2024, 1, 1))
    assert "USA" in json.loads(oecd["flags"])["eia_members"].split("+")
    coal = frames["intl_coal"]
    assert coal.filter(pl.col("indicator") == "INTL.7-6-MST.A")["unit"].unique().to_list() == ["million short tons"]
    sel = json.loads(w["selector"])
    assert sel["file"] == "INTL.txt" and sel["series_id"] == "INTL.57-1-WORL-TBPD.A" and sel["period"] == "2024"


def test_eia_codes_never_become_zero() -> None:
    frames, rep, _ = _eia()
    p = frames["intl_petroleum"]
    usoh = p.filter((pl.col("provider_geo") == "USOH") & (pl.col("indicator") == "INTL.5-5-MBBL.A"))
    assert usoh.height > 0 and usoh["value"].is_null().all()
    fl = json.loads(usoh["flags"][0])
    assert fl["missing"] == "confidential" and fl["provider_code"] == "w"
    mtq = frames["intl_energy"].filter((pl.col("iso3") == "MTQ") & pl.col("value").is_null())
    assert mtq.height > 0 and json.loads(mtq["flags"][0])["provider_code"] == "ie"
    dropped = sum(sum(t.dropped.values()) for t in rep.tables.values())
    assert dropped > 0  # '--' and 'NA' produce no cell at all
    for f in frames.values():
        nul = f.filter(pl.col("value").is_null())
        assert nul["flags"].str.contains('"missing"').all()


def test_eia_geographies_map_through_iso3_plus_kosovo() -> None:
    frames, _rep, _ = _eia()
    assert country_iso3("XKS", "eia") == "XKX" and country_iso3("SUN", "eia") is None
    assert country_iso3("WORL", "eia") is None and country_iso3("FRA", "eia") == "FRA"
    e = frames["intl_energy"]
    assert e.filter(pl.col("provider_geo") == "XKS")["iso3"].unique().to_list() == ["XKX"]
    um = {u["provider_geo"] for u in _EIA["unmapped:intl_energy"]}
    assert {"WORL", "OECD", "SUN"} <= um


def _zip(lines: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("INTL.txt", "".join(json.dumps(x) + "\n" for x in lines))
    return buf.getvalue()


def _series(sid: str, data: list[list[Any]], **kw: Any) -> dict[str, Any]:
    return {"series_id": sid, "name": "x", "units": "thousand barrels per day", "f": "A", "copyright": "None",
            "geography": sid.split("-")[2], "geoset_id": sid[:5] + "-".join(sid[5:].split("-")[:2]) + "-"
            + sid.split("-")[3], "data": data, **kw}  # fmt: skip


def test_eia_layout_surprises_are_refused_and_unknown_codes_are_flagged() -> None:
    with pytest.raises(EiaParseError, match="not a zip"):
        parse_bulk(b"nope", "0" * 64, {})
    s = _series("INTL.57-1-FRA-TBPD.A", [["2020", 1.5], ["2021", "--"], ["2022", "(s)"], ["2023", None]])
    frames, rep = parse_bulk(_zip([s]), "0" * 64, {"(s)": "Value is too small"}, {"intl_petroleum"})
    f = frames["intl_petroleum"]
    assert f["value"].to_list() == ["1.5", None]
    assert json.loads(f["flags"][1])["missing"] == "provider_missing_code" and rep.unknown_codes == {"(s)": 1}
    with pytest.raises(EiaParseError, match="repeats period"):
        parse_bulk(_zip([_series("INTL.57-1-FRA-TBPD.A", [["2020", 1], ["2020", 2]])]), "0" * 64, {})
    with pytest.raises(EiaParseError, match="period"):
        parse_bulk(_zip([_series("INTL.57-1-FRA-TBPD.A", [["202001", 1]])]), "0" * 64, {})
    cr = _series("INTL.57-1-FRA-TBPD.A", [["2020", 2]], copyright="(c) someone")
    frames, rep = parse_bulk(_zip([cr]), "0" * 64, {}, {"intl_petroleum"})
    assert json.loads(frames["intl_petroleum"]["flags"][0])["copyright"] == "(c) someone"
    assert rep.copyright_other == {"(c) someone": 1}
