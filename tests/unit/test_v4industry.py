"""V4INDUSTRY connectors (GLEIF LEI API, Wikidata P1278 bridge, OECD TiVA): offline parser tests on real captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import atlas_gleif as g
from reva.adapters.connectors import atlas_tiva as t
from reva.adapters.connectors.atlas_industry_common import atlas_frame, load_predicates
from reva.atlas.tables import validate_atlas_frame
from reva.kernel.errors import SourceResponseError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
SHA = "a" * 64
T0 = datetime(2026, 10, 17, 12, tzinfo=UTC)
TOYOTA, APPLE, SIEMENS_PK, GM = (
    "5493006W3QUS5LMH6R84",
    "HWUPKR0MPOU8FGXBT394",
    "529900IR3Y1XBIR1GX29",
    "54930070NSV60J38I987",
)
SEEDS = [TOYOTA, APPLE, SIEMENS_PK, GM]


def _fixture(connector: str, url: str) -> bytes:
    from reva.evidence.runner import _fixture_fingerprint

    entries = json.loads((FIX / connector / "manifest.json").read_text())
    fp = _fixture_fingerprint(url)
    (e,) = [e for e in entries if e["request_fingerprint"] == fp]
    return (FIX / connector / e["file"]).read_bytes()


def gl(url: str) -> bytes:
    return _fixture("atlas.gleif", url)


# ----------------------------------------------------------------------------- identifiers / predicates
def test_lei_checksum():
    assert all(g.lei_valid(x) for x in SEEDS)
    assert not g.lei_valid("5493006W3QUS5LMH6R85")  # check digits altered
    assert not g.lei_valid("95980020140005000000")  # a real Wikidata P1278 value that is not a valid LEI
    assert not g.lei_valid("5493006w3qus5lmh6r84")  # lower case is not repaired


def test_predicates_registered_and_mapped():
    pids = {p.pid for p in load_predicates()}
    assert {g.P[k] for k in g.P} <= pids
    import yaml

    rel = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "registry/kg/relations_v4.d/v4industry.yaml").read_text()
    )
    mapped = {m["property"] for m in rel["mappings"]}
    assert mapped <= pids and "gleif:wikidata_item" not in mapped  # identity bridge stays unmapped


# ----------------------------------------------------------------------------- GLEIF counts
def test_counts_and_countries():
    countries = g.parse_countries(gl(g.countries_url()), min_countries=1)
    assert [c for c, _ in countries] == ["BV", "FR", "LU", "XK"]
    total, pd = g.parse_count(gl(g.count_url("FR", {})))
    assert total > 150_000 and pd.startswith("2026-10-17")
    issued, _ = g.parse_count(gl(g.count_url("FR", g.COUNT_FILTERS["issued"])))
    assert 0 < issued < total
    assert g.parse_count(gl(g.count_url("BV", {})))[0] == 0  # an API total of 0 over the complete index
    with pytest.raises(SourceResponseError):
        g.parse_countries(gl(g.countries_url()))  # live threshold: a 4-country list is incomplete


def test_api_error_is_typed():
    body = b'{"errors":[{"title":"Filter should contain only allowed values.","source":{"parameter":"filter"}}]}'
    with pytest.raises(SourceResponseError) as e:
        g.parse_count(body)
    assert e.value.details["kind"] == "api_error"


def test_count_rows_frame():
    u = g.count_url("FR", {})
    total, pd = g.parse_count(gl(u))
    row = g.count_row(
        dataset="lei_counts",
        indicator="lei_records|filter=all",
        concept="companies.gleif.lei_records.all",
        iso2="FR",
        value=total,
        publish=pd,
        raw_sha256=SHA,
        request_url=u,
        flags={},
    )
    xk = {**row, "provider_geo": "XK"}
    f, unmapped = validate_atlas_frame(
        atlas_frame([row, xk]), source="gleif", dataset="lei_counts", raw_sha256s=[SHA], geo_scheme="iso2"
    )
    assert f.filter(pl.col("provider_geo") == "FR")["iso3"].to_list() == ["FRA"]
    assert [u["provider_geo"] for u in unmapped] == ["XK"]  # Kosovo: no crosswalk ISO2 -> stays unmapped


def test_legal_forms():
    forms, cur, last = g.parse_elf_page(gl(g.elf_url(1)))
    assert cur == last == 1
    jp = {f.code: f for f in forms if f.country == "JP"}
    assert "T417" in jp and jp["T417"].names[0]["localName"] == "株式会社"
    n, _ = g.parse_count(gl(g.count_url("JP", {"entity.legalForm": "T417"})))
    assert n > 1000


# ----------------------------------------------------------------------------- GLEIF records + KG
@pytest.fixture(scope="module")
def pages():
    p1 = g.parse_records(gl(g.records_url(SEEDS)), requested=SEEDS)
    parents = sorted(set(p1.parent_leis))
    p2 = g.parse_records(
        gl(g.records_url(parents, with_parents=False)), requested=parents, with_parents=False, parents_page=True
    )
    return p1, p2


def test_parse_records_parents_and_exceptions(pages):
    p1, p2 = pages
    by = {r.lei: r for r in p1.records}
    assert by[SIEMENS_PK].direct_parent == g.Parent("lei", lei="529900QBVWXMWANH7H45")
    assert by[SIEMENS_PK].ultimate_parent == g.Parent("lei", lei="W38RGI023J3WT1HWRP32")
    assert by[APPLE].direct_parent and by[APPLE].direct_parent.reason == "NATURAL_PERSONS"
    assert by[TOYOTA].gem == ("E100001016208",) and by[TOYOTA].name_lang == "ja"
    assert by[GM].ocid == "us_de/4718317"
    assert {r.lei for r in p2.records} == {"529900QBVWXMWANH7H45", "W38RGI023J3WT1HWRP32"}
    assert all(r.included for r in p2.records)


def test_records_without_include_are_refused():
    body = gl(g.records_url(SEEDS))
    doc = json.loads(body)
    doc.pop("included")
    for x in doc["data"]:
        for k in ("direct-parent", "ultimate-parent"):
            x["relationships"][k].pop("data", None)
            x["relationships"][k]["links"] = {"relationship-record": "x", "lei-record": "y"}
    with pytest.raises(SourceResponseError) as e:
        g.parse_records(json.dumps(doc).encode(), requested=SEEDS)
    assert e.value.details["kind"] == "request_shape"


def test_kg_records(pages):
    p1, p2 = pages
    forms, _, _ = g.parse_elf_page(gl(g.elf_url(1)))
    ents, sts, info = g.kg_records([p1, p2], [SHA, "b" * 64], elf={f.code: f for f in forms})
    e = {x.qid: x for x in ents}
    assert e[f"lei:{TOYOTA}"].labels == {"ja": "トヨタ自動車株式会社", "en": "Toyota Motor Corporation"}
    assert e["lei:W38RGI023J3WT1HWRP32"].kind == "legal_entity"
    st = {(s.subject_qid, s.property_pid): s for s in sts}
    assert st[(f"lei:{SIEMENS_PK}", "gleif:ultimate_parent")].object_qid == "lei:W38RGI023J3WT1HWRP32"
    assert st[(f"lei:{TOYOTA}", "gleif:gem_entity")].object_qid == "gem:E100001016208"
    assert st[(f"lei:{TOYOTA}", "gleif:legal_address_country")].object_qid == "Q17"
    lf = st[(f"lei:{TOYOTA}", "gleif:legal_form")]
    assert lf.value == {"type": "string", "value": "T417"} and lf.qualifiers["elf_names"][0]["localName"] == "株式会社"
    # parents are Level 1 only: no parent statements for them
    assert (
        "lei:W38RGI023J3WT1HWRP32",
        "gleif:direct_parent",
    ) not in st and ("lei:W38RGI023J3WT1HWRP32", "gleif:parent_reporting_exception") not in st
    exc = [s for s in sts if s.subject_qid == f"lei:{APPLE}" and s.property_pid == "gleif:parent_reporting_exception"]
    assert sorted(s.value["value"] for s in exc) == [
        "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT: NATURAL_PERSONS",
        "ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT: NATURAL_PERSONS",
    ]
    assert info["stats"]["direct_parent_lei"] == 1
    assert len({s.statement_id for s in sts}) == len(sts)


def test_groups_and_child_counts():
    page = g.parse_records(gl(g.top_groups_url(3)), with_parents=False)
    assert len(page.records) == 3
    counts = {}
    for r in page.records:
        for which in ("ultimate-children", "direct-children"):
            n, pd = g.parse_count(gl(g.children_count_url(r.lei, which)))
            counts.setdefault(r.lei, {})[which] = (n, pd, SHA)
    _, sts, _ = g.kg_records([page], [SHA], counts=counts)
    q = [s for s in sts if s.property_pid == "gleif:ultimate_child_count"]
    amounts = [int(s.value["amount"]) for s in q]
    assert len(q) == 3 and all(a > 500 for a in amounts)
    assert amounts == sorted(amounts, reverse=True)  # the API sort by ultimateChildCount, verified by the totals
    assert all(s.point_in_time is not None and s.point_in_time.precision == 11 for s in q)


# ----------------------------------------------------------------------------- Wikidata P1278
def test_wikidata_p1278_bridge():
    from urllib.parse import urlencode

    from reva.adapters.connectors.wikidata import WDQS_URL

    url = f"{WDQS_URL}?{urlencode({'query': g.WD_QUERY.strip(), 'format': 'json'})}"
    doc = json.loads(_fixture("atlas.gleif_wikidata", url))
    sts, rep = g.wikidata_statements(doc["results"]["bindings"], raw_sha256=SHA, retrieved_at=T0, query_sha256=SHA)
    by = {s.subject_qid: s for s in sts}
    assert by[f"lei:{TOYOTA}"].object_qid == "Q53268" and by[f"lei:{APPLE}"].object_qid == "Q312"
    assert by[f"lei:{TOYOTA}"].qualifiers["item_sitelinks"] > 100
    assert rep["skipped"]["value_not_a_valid_lei"] == 3  # real invalid values are skipped, never repaired
    assert all(s.statement_id.startswith(g.WD_PREFIX) for s in sts)


# ----------------------------------------------------------------------------- OECD TiVA
def tv(url: str) -> bytes:
    return _fixture("atlas.oecd_tiva", url)


AREAS = ["FRA", "DEU", "CHN"]


def test_tiva_labels_and_range():
    m = t.parse_codelist(tv(t.codelist_url(t.AGENCY, "CL_TIVA_MEASURE")))
    a = t.parse_codelist(tv(t.codelist_url("OECD", "CL_ACTIVITY_ISIC4")))
    assert m["EXGR_DVA"] == "Domestic value added in gross exports" and a["C"] == "Manufacturing"
    assert t.parse_time_range(tv(t.constraint_url(t.FLOW_MAIN))) == (1995, 2022)


def test_tiva_principal_rows():
    m = t.parse_codelist(tv(t.codelist_url(t.AGENCY, "CL_TIVA_MEASURE")))
    a = t.parse_codelist(tv(t.codelist_url("OECD", "CL_ACTIVITY_ISIC4")))
    rows = []
    for meas in t.MEASURES:
        u = t.main_url(meas, areas=AREAS, start=2021, end=2022)
        recs, _ = t.parse_csv(tv(u), t.MAIN_COLS)
        rows += t.main_rows(recs, raw_sha256=SHA, request_url=u, measures=m, activities=a)
    f, unmapped = validate_atlas_frame(
        atlas_frame(rows), source="oecd", dataset="tiva_principal", raw_sha256s=[SHA], geo_scheme="iso3"
    )
    assert unmapped == [] and set(f["iso3"].unique()) == set(AREAS)
    c = f.filter((pl.col("indicator") == "EXGR|activity=C") & (pl.col("iso3") == "DEU"))
    assert c.height == 2 and json.loads(c["flags"][0])["isic4_section"] == "C"
    assert json.loads(c["flags"][0])["estimate"] == "oecd_icio_model_based"


def test_tiva_fdva_rows_bilateral():
    a = t.parse_codelist(tv(t.codelist_url("OECD", "CL_ACTIVITY_ISIC4")))
    u = t.fdva_url(va_activities=["_T"], areas=AREAS, start=2021, end=2022)
    recs, _ = t.parse_csv(tv(u), t.FDVA_COLS)
    rows = t.fdva_rows(recs, raw_sha256=SHA, request_url=u, activities=a)
    f, _ = validate_atlas_frame(
        atlas_frame(rows), source="oecd", dataset="tiva_fdva", raw_sha256s=[SHA], geo_scheme="iso3"
    )
    # 3 source economies x 3 final-demand economies x 2 years
    assert f.height == 18
    one = f.filter((pl.col("iso3") == "CHN") & (pl.col("indicator") == "FDVA|fd_area=DEU|va_activity=_T"))
    assert one.height == 2 and json.loads(one["flags"][0])["fd_area_iso3"] == "DEU"


def test_tiva_xml_envelope_is_typed():
    with pytest.raises(SourceResponseError) as e:
        t.parse_csv(b"<?xml version='1.0'?><message:Error/>", t.MAIN_COLS)
    assert e.value.details["kind"] == "not_csv"
