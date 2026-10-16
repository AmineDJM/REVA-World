"""A-SCI atlas connectors (OpenAlex, Nobel, WIPO/GII): offline parser and catalog tests."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import atlas_nobel as nobel
from reva.adapters.connectors import atlas_openalex as oa
from reva.adapters.connectors import atlas_wipo as wipo
from reva.adapters.connectors.atlas_sci_common import atlas_frame
from reva.atlas.tables import validate_atlas_frame
from reva.kernel.errors import SourceResponseError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
T0 = datetime(2026, 10, 15, 22, 5, tzinfo=UTC)
SHA = "a" * 64


def _fixture(connector: str, url: str) -> bytes:
    from reva.evidence.runner import _fixture_fingerprint

    entries = json.loads((FIX / connector / "manifest.json").read_text())
    fp = _fixture_fingerprint(url)
    (e,) = [e for e in entries if e["request_fingerprint"] == fp]
    return (FIX / connector / e["file"]).read_bytes()


# ----------------------------------------------------------------------------- Nobel
@pytest.fixture(scope="module")
def nobel_records():
    recs, info = nobel.parse_laureates(_fixture("atlas.nobel", nobel.laureates_url(0)), min_expected=1)
    return recs, info


def test_nobel_parse_excerpt(nobel_records):
    recs, info = nobel_records
    assert info["laureates"] == 18
    curie = [r for r in recs if r.laureate_id == "6"]
    assert [(r.award_year, r.category) for r in curie] == [(1903, "phy"), (1911, "che")]
    assert curie[0].birth_country_now and curie[0].birth_country_now.name == "Poland"
    icrc = [r for r in recs if r.laureate_id == "482"]
    assert {r.kind for r in icrc} == {"organization"} and len(icrc) == 3
    assert all(r.prize_qid == nobel.CATEGORIES["Peace"][1] for r in icrc)


def test_nobel_partial_page_refused():
    body = _fixture("atlas.nobel", nobel.laureates_url(0, 3))  # real live capture, limit=3
    with pytest.raises(SourceResponseError) as e:
        nobel.parse_laureates(body)
    assert e.value.details["kind"] == "incomplete"


def test_nobel_full_list_threshold(nobel_records):
    with pytest.raises(SourceResponseError):
        nobel.parse_laureates(_fixture("atlas.nobel", nobel.laureates_url(0)))  # 18 < 1000 expected


def test_nobel_unknown_category_is_schema_drift():
    doc = json.loads(_fixture("atlas.nobel", nobel.laureates_url(0)))
    doc["laureates"][0]["nobelPrizes"][0]["category"]["en"] = "Mathematics"
    with pytest.raises(SourceResponseError) as e:
        nobel.parse_laureates(json.dumps(doc).encode(), min_expected=1)
    assert e.value.details["kind"] == "schema_drift"


def test_nobel_atlas_counts(nobel_records):
    recs, _ = nobel_records
    f = nobel.frame(recs, raw_sha256=SHA, retrieved_at=T0)
    v, unmapped = validate_atlas_frame(f, source="nobel", dataset="laureates", raw_sha256s=[SHA], geo_scheme="iso3")
    # Taiwan (Yuan T. Lee) maps to its own crosswalk entity, never merged into CHN
    assert unmapped == []
    assert v.filter((pl.col("iso3") == "TWN") & (pl.col("indicator") == "born_total|category=all")).height == 1
    # Marie Curie: born in present-day Poland, counted in 1903 (physics) and 1911 (chemistry)
    pol = v.filter((pl.col("iso3") == "POL") & (pl.col("indicator") == "born|category=all"))
    years = {r["period_start"].year: r["value"] for r in pol.to_dicts()}
    assert years[1903] == "1" and years[1911] == "1"
    # Organisations have no birth place: the ICRC (Switzerland) is not among 'born'
    born_che = v.filter((pl.col("iso3") == "CHE") & pl.col("indicator").str.starts_with("born"))
    assert born_che.height == 0 or "482" not in "".join(born_che["selector"].to_list())
    # Constituent countries of the UK map to GBR and say so
    gbr = v.filter((pl.col("iso3") == "GBR") & pl.col("indicator").str.starts_with("born_total|category=all"))
    flags = json.loads(gbr["flags"][0])
    assert flags["aggregation"] == "count" and flags["computed_by"] == "reva"
    assert set(flags["subnational_mapped"]) <= {"Scotland", "Northern Ireland"} and flags["subnational_mapped"]
    # no zero is ever emitted
    assert "0" not in v["value"].to_list()


def test_nobel_declined_flagged(nobel_records):
    recs, _ = nobel_records
    v = nobel.frame(recs, raw_sha256=SHA, retrieved_at=T0)
    declined = [json.loads(x) for x in v["flags"].to_list() if "includes_prize_status" in x]
    assert any("declined" in d["includes_prize_status"] for d in declined)


def test_nobel_affiliation_full_counting(nobel_records):
    recs, _ = nobel_records
    multi = [r for r in recs if len({p.name for _, p in r.affiliations if p}) > 1]
    assert multi, "fixture keeps a laureate affiliated in two countries"
    r = multi[0]
    v = nobel.frame(recs, raw_sha256=SHA, retrieved_at=T0)
    hits = v.filter(
        (pl.col("indicator") == f"affiliated|category={r.category}")
        & (pl.col("period_start").dt.year() == r.award_year)
    )
    countries = {nobel.COUNTRY_NOW_ISO3[p.name] for _, p in r.affiliations if p}
    assert countries <= set(hits["iso3"].to_list())


def test_nobel_kg_statements(nobel_records):
    from reva.kg.model import KgStatement

    recs, _ = nobel_records
    st = nobel.kg_statements(recs, raw_sha256=SHA, retrieved_at=T0)
    awards = [s for s in st if s["property"] == "P166"]
    assert len(awards) == sum(1 for r in recs if r.laureate_qid)
    curie = [s for s in awards if s["subject"] == "Q7186"]
    assert {(s["object"], s["point_in_time"][0]) for s in curie} == {("Q38104", 1903), ("Q44585", 1911)}
    births = [s for s in st if s["property"] == "P19"]
    assert len({s["subject"] for s in births}) == len(births)  # one birthplace per laureate
    kg = nobel.to_kg_statements(st)
    assert all(isinstance(k, KgStatement) and k.source_key == nobel.SOURCE_KEY for k in kg)
    assert all(k.statement_id and k.raw_sha256 == SHA for k in kg)


def test_nobel_country_table_targets_are_crosswalk_keys():
    from reva.atlas.tables import known_country_keys

    assert set(nobel.COUNTRY_NOW_ISO3.values()) <= known_country_keys()


def test_atlas_source_specs():
    from reva.atlas.sources import discover

    found, errors = discover(
        [f"reva.adapters.connectors.atlas_{m}" for m in ("nobel", "openalex", "wipo", "sci_common")]
    )
    assert errors == []
    assert set(found) == {"atlas.nobel", "atlas.openalex", "atlas.wipo_ipstats", "atlas.wipo_gii"}
    spec = found["atlas.nobel"].spec
    assert spec.template == "atlas_bulk" and spec.allowed_hosts == ("api.nobelprize.org",)
    assert spec.source_key == "nobelprize.api" and spec.datasets == ("laureates",)
    assert found["atlas.wipo_ipstats"].spec.datasets == tuple(b.dataset for b in wipo.BULK_FILES)
    assert found["atlas.openalex"].spec.planned_requests >= len(oa.queries())


# ----------------------------------------------------------------------------- WIPO bulk
@pytest.fixture(scope="module")
def patent_zip():
    return wipo.open_zip(_fixture("atlas.wipo_ipstats", wipo.ZIPS["patent"]))


def test_wipo_bulk_frame(patent_zip):
    bf = wipo.bulk_members("patent")[0]
    f = wipo.bulk_frame(patent_zip, bf, raw_sha256=SHA, retrieved_at=T0)
    v, unmapped = validate_atlas_frame(f, source="wipo", dataset=bf.dataset, raw_sha256s=[SHA], geo_scheme="iso2")
    assert {u["provider_geo"] for u in unmapped} <= {"**", "YU", "AN", "CS", "DD"}
    assert "**" in {u["provider_geo"] for u in unmapped}
    row = v.filter(pl.col("indicator") == "patent_publications|office=EP|tech=6").filter(
        (pl.col("iso3") == "FRA") & (pl.col("period_start").dt.year() == 2020)
    )
    assert row["value"].to_list() == ["529"]  # verbatim from the WIPO file (row 2020,EP,FR,6,529)
    sel = json.loads(row["selector"][0])
    assert sel["file"] == bf.member and sel["row"] > 0
    star = v.filter(pl.col("indicator").str.contains(r"office=\*\*"))
    assert star.height and all("code_undocumented" in x for x in star["flags"].to_list())


def test_wipo_never_sums(patent_zip):
    bf = wipo.bulk_members("patent")[0]
    f = wipo.bulk_frame(patent_zip, bf, raw_sha256=SHA, retrieved_at=T0)
    rows = sum(1 for _ in wipo.iter_bulk_csv(patent_zip, bf))
    assert f.height == rows  # one atlas cell per provider row, no derived totals


def test_wipo_tech_names(patent_zip):
    names = wipo.tech_field_names(patent_zip)
    assert names["0"]["en"] == "Unknown" and names["6"]["en"] == "Computer technology"
    assert names["6"]["fr"] == "Informatique" and len(names) == 36


def _zip(member: str, text: bytes) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, text)
    return wipo.open_zip(buf.getvalue())


def test_wipo_header_drift_and_bad_counts():
    bf = wipo.bulk_members("patent")[0]
    with pytest.raises(SourceResponseError):
        list(wipo.iter_bulk_csv(_zip(bf.member, b"year,office,origin,tech,value\r\n2020,EP,FR,6,1\r\n"), bf))
    for bad in (b"2020,EP,FR,6,1.5", b"2020,EP,FR,6,", b"2020,EP,FRA,6,3", b"20x0,EP,FR,6,3"):
        with pytest.raises(SourceResponseError):
            list(wipo.iter_bulk_csv(_zip(bf.member, b"year,office,origin,tec_id,count\r\n" + bad + b"\r\n"), bf))


def test_wipo_zip_bomb_guard(monkeypatch):
    monkeypatch.setattr(wipo, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(SourceResponseError):
        _zip("x.csv", b"0123456789abcdef")


def test_wipo_all_excerpt_zips_parse():
    for key, url in wipo.ZIPS.items():
        zf = wipo.open_zip(_fixture("atlas.wipo_ipstats", url))
        for bf in wipo.bulk_members(key):
            f = wipo.bulk_frame(zf, bf, raw_sha256=SHA, retrieved_at=T0)
            validate_atlas_frame(f, source="wipo", dataset=bf.dataset, raw_sha256s=[SHA], geo_scheme=wipo.GEO_SCHEME)
            if bf.id_name == "class":
                assert all("class_count" in x for x in f["flags"].to_list())


# ----------------------------------------------------------------------------- GII
@pytest.fixture(scope="module")
def gii():
    return wipo.gii_rows(_fixture("atlas.wipo_gii", wipo.GII_URL), raw_sha256=SHA, retrieved_at=T0)


def test_gii_composites_only(gii):
    rows, info = gii
    assert info["economies"] == 139
    f = atlas_frame(rows)
    v, unmapped = validate_atlas_frame(f, source="wipo", dataset="gii_2025", raw_sha256s=[SHA], geo_scheme="iso3")
    assert unmapped == []
    nums = {i.split("|")[0] for i in v["indicator"].unique().to_list()}
    # 31 composite levels; no third-party indicator-level values (e.g. IN.1.1.1)
    assert len(nums) == 31 and not any(n.count(".") >= 3 for n in nums)
    top = v.filter(pl.col("indicator") == "GII|measure=rank").filter(pl.col("value") == "1")
    assert top["iso3"].to_list() == ["CHE"]
    che = v.filter((pl.col("indicator") == "GII|measure=score") & (pl.col("iso3") == "CHE"))
    assert che["value"][0].startswith("65.96")  # stored decimal text, no float round-trip


def test_xlsx_reader_rejects_non_zip():
    with pytest.raises(SourceResponseError):
        wipo.read_xlsx(b"<html>not a workbook</html>")


# ----------------------------------------------------------------------------- OpenAlex
def _group_doc(groups, count=1000, next_cursor=None):
    return json.dumps(
        {
            "meta": {"count": count, "page": None, "per_page": 200, "next_cursor": next_cursor, "groups_count": None},
            "results": [],
            "group_by": [{"key": k, "key_display_name": n, "count": c} for k, n, c in groups],
        }
    ).encode()


def test_openalex_query_urls():
    q = oa.Query("works_country_domain", 2020, "domain", "3")
    url = oa.query_url(q, api_key="SECRET")
    assert url.startswith("https://api.openalex.org/works?filter=publication_year:2020,type:article|review,")
    assert "primary_topic.domain.id:3" in url and "group_by=authorships.countries" in url and "cursor" not in url
    assert "cursor=*" in oa.query_url(q, cursor="*")
    from reva.adapters.http.safe_client import redact_url

    assert "SECRET" not in redact_url(url) and "mailto" not in url
    qs = oa.queries()
    assert len([x for x in qs if x.dataset == "works_country_year"]) == 36
    assert len([x for x in qs if x.dataset == "works_country_domain"]) == 11 * 4


def test_openalex_rows_keep_world_and_unknown():
    q = oa.Query("works_country_year", 2020)
    page = oa.parse_group_page(
        _group_doc(
            [
                ("https://openalex.org/countries/US", "United States", 500),
                ("https://openalex.org/countries/FR", "France", 80),
                ("unknown", "unknown", 300),
            ]
        )
    )
    rows = oa.atlas_rows(q, [oa.Fetched(page, SHA, 1, q.filter)], retrieved_at=T0)
    f = atlas_frame(rows)
    v, unmapped = validate_atlas_frame(
        f, source="openalex", dataset=q.dataset, raw_sha256s=[SHA], geo_scheme=oa.GEO_SCHEME
    )
    assert {u["provider_geo"] for u in unmapped} == {"_all_works", "unknown"}
    assert dict(zip(v["provider_geo"], v["value"], strict=True)) == {
        "US": "500",
        "FR": "80",
        "unknown": "300",
        "_all_works": "1000",
    }
    assert v.filter(pl.col("iso3") == "FRA")["concept_id"].to_list() == ["sci.openalex.works"]


def test_openalex_bad_pages():
    for body in (
        b"<html>",
        json.dumps({"meta": {}, "group_by": []}).encode(),
        json.dumps({"error": "Rate limit exceeded", "message": "Insufficient budget"}).encode(),
        _group_doc([("https://openalex.org/countries/FR", "France", -1)]),
    ):
        with pytest.raises(SourceResponseError):
            oa.parse_group_page(body)
    q = oa.Query("works_country_year", 2020)
    p1 = oa.parse_group_page(_group_doc([("https://openalex.org/countries/FR", "France", 3)]))
    with pytest.raises(SourceResponseError):  # a group repeated across cursor pages
        oa.atlas_rows(q, [oa.Fetched(p1, SHA, 1, q.filter), oa.Fetched(p1, SHA, 2, q.filter)], retrieved_at=T0)


# ----------------------------------------------------------------------------- catalog
def test_science_catalog_series_match_parsers(nobel_records, gii):
    from reva.atlas.catalog import load_catalog

    cat = load_catalog()
    concepts = (
        [c for c in cat.concepts.values() if c.domain == "science"]
        if isinstance(cat.concepts, dict)
        else [c for c in cat.concepts if c.domain == "science"]
    )
    assert len(concepts) >= 100
    recs, _ = nobel_records
    produced = {
        f"nobel.laureates.{i}" for i in nobel.frame(recs, raw_sha256=SHA, retrieved_at=T0)["indicator"].unique()
    }
    produced |= {f"wipo.gii_2025.{r['indicator']}" for r in gii[0]}
    produced |= {f"openalex.{q.dataset}.{q.indicator}" for q in oa.queries(field_years=oa.FIELD_YEARS)}
    for c in concepts:
        for s in c.source_order:
            if s.source in ("nobel", "openalex") or s.dataset == "gii_2025":
                assert s.key in produced or (s.source == "nobel" and "|category=" in s.indicator), s.key
            else:
                assert s.dataset == "patent_pub_tech" and s.indicator.startswith("patent_publications|office=")


async def test_openalex_complement_queries_without_key():
    """Without an API key OpenAlex refuses cursor paging: the countries beyond the 200 largest groups come
    from complement queries; co-author countries in those responses are ignored (counted from the first
    page); a country cut off by a full complement response is re-queried alone, then reported unresolved."""
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from reva.atlas.tables import geo_index

    codes = sorted(geo_index().maps[oa.GEO_SCHEME])
    first = [*[c for c in codes if c not in ("US", "NR")][:199], "US"]
    calls: list[str] = []

    class Run:
        origin = "live"

        async def get(self, url: str, **kw: object) -> SimpleNamespace:
            calls.append(url)
            flt = parse_qs(urlsplit(url).query)["filter"][0]
            if "authorships.countries:" not in flt:
                groups = [(f"https://openalex.org/countries/{c}", c, 1000) for c in first]
                return SimpleNamespace(body=_group_doc(groups, count=5000))
            chunk = flt.split("authorships.countries:")[1].split("|")
            if "NR" in chunk:  # NR's co-authors fill a full response that never shows NR
                filler = [(f"https://openalex.org/countries/{c}", c, 9) for c in first]
                return SimpleNamespace(body=_group_doc(filler, count=99))
            groups = [(f"https://openalex.org/countries/{c}", c, 7) for c in chunk if c != "TV"] + [
                ("https://openalex.org/countries/US", "United States", 3)
            ]
            return SimpleNamespace(body=_group_doc(groups, count=99))

        def archive(self, raw: object) -> str:
            return SHA

    q = oa.Query("works_country_year", 2020)
    fetched = await oa.fetch_query(Run(), q, api_key=None, limit=100, state={"requests": 0})
    rows = oa.atlas_rows(q, fetched, retrieved_at=T0)
    vals = {r["provider_geo"]: r["value"] for r in rows}
    assert vals["US"] == "1000" and vals["_all_works"] == "5000"
    rest = sorted(set(codes) - set(first) - {"NR", "TV"})
    assert all(vals[c] == "7" for c in rest)
    assert "TV" not in vals  # absent from a complete (< 200 groups) response: no work, no row (never a 0)
    assert "NR" not in vals
    all_works = next(r for r in rows if r["provider_geo"] == "_all_works")
    assert json.loads(all_works["flags"])["unresolved_countries"] == ["NR"]
    assert not any("cursor" in u for u in calls)
