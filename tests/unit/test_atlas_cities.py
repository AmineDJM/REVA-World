"""A-CITIES offline tests: GeoNames cities15000 / admin1 / alternate names, UN WUP 2025, KG link helpers.

Fixtures are excerpts of the real bytes retrieved on 2026-10-15 (see each manifest's ``excerpt``)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_cities as C
from reva.adapters.connectors import atlas_wup as W
from reva.atlas.tables import validate_atlas_frame

FIX = Path(__file__).parents[1] / "fixtures" / "source"
NOW = datetime(2026, 10, 15, 23, 35, tzinfo=UTC)
SHA = "a" * 64


def _fixture(source: str, url: str) -> bytes:
    man = json.loads((FIX / source / "manifest.json").read_text())
    (e,) = [e for e in man if e["url"] == url]
    return (FIX / source / e["file"]).read_bytes()


@pytest.fixture(scope="module")
def places() -> list[C.Place]:
    return C.parse_cities(_fixture(C.CONNECTOR_NAME, C.URL_CITIES))


@pytest.fixture(scope="module")
def admin1() -> list[C.Admin1]:
    return C.parse_admin1(_fixture(C.CONNECTOR_NAME, C.URL_ADMIN1))


def _validate(rows: list[dict[str, Any]], source: str, dataset: str, scheme: str) -> pl.DataFrame:
    frame = rows if isinstance(rows, pl.DataFrame) else C._frame(rows)
    out = validate_atlas_frame(frame, source=source, dataset=dataset, raw_sha256s=[SHA], geo_scheme=scheme)
    return out[0]


# ------------------------------------------------------------------------------------------- GeoNames
def test_parse_cities_and_admin1(places, admin1) -> None:
    assert len(places) == 20
    by = {p.geonameid: p for p in places}
    assert by["2267057"].cells["name"] == "Lisbon" and by["2267057"].feature_code == "PPLC"
    assert by["786714"].country == "XK"
    assert {a.country for a in admin1} >= {"PT", "XK", "BO", "MY"}
    assert C.validate_cities(places, min_rows=1) == []
    assert C.validate_cities(places)[0].startswith("only 20 places")  # live completeness bound


def test_parse_cities_refuses_layout_change() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities15000.txt", "1\tA\tA\n")
    with pytest.raises(C.GeoNamesCitiesError, match="columns"):
        C.parse_cities(buf.getvalue())
    with pytest.raises(C.GeoNamesCitiesError, match=r"no cities15000\.txt"):
        C.parse_cities(_zip({"other.txt": "x"}))


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_city_rows_values_as_declared(places, admin1) -> None:
    rows, omitted = C.city_rows(places, {a.code: a for a in admin1}, raw_sha256=SHA, retrieved_at=NOW)
    f = _validate(rows, "geonames", C.DS_CITIES, C.GEO_SCHEME)
    assert omitted == {"elevation_empty": 20, "population_zero_unknown": 2, "dem_no_data": 1}
    lis = f.filter(pl.col("indicator") == "population|geonameid=2267057").row(0, named=True)
    assert lis["value"] == "517802" and lis["iso3"] == "PRT" and lis["unit"] == "persons"
    flags = json.loads(lis["flags"])
    assert flags["geonames_modification_date"] and flags["feature_code"] == "PPLC"
    assert str(lis["period_start"]) == "2026-10-15" and str(lis["period_end"]) == "2026-10-16"
    assert json.loads(lis["selector"])["column"] == "population"
    # GeoNames population 0 = unknown: never stored as a number (Plymouth MS, Ngerulmud PW)
    assert (
        f.filter(pl.col("indicator").is_in(["population|geonameid=3578069", "population|geonameid=8063361"])).height
        == 0
    )
    assert f.filter(pl.col("indicator") == "dem_elevation|geonameid=6949382").height == 0  # -9999 = no data
    assert f.filter(pl.col("provider_geo") == "XK")["iso3"].unique().to_list() == ["XKX"]
    a1 = f.filter(pl.col("indicator") == "admin1_code|geonameid=2267057").row(0, named=True)
    assert a1["value_text"] == "14" and json.loads(a1["flags"])["admin1_name"] == "Lisbon"


def test_country_aggregates_exact_and_never_pick_a_capital(places) -> None:
    f = _validate(C.country_rows(places, raw_sha256=SHA, retrieved_at=NOW), "geonames", C.DS_COUNTRY, C.GEO_SCHEME)

    def get(cc: str, ind: str) -> dict[str, Any] | None:
        r = f.filter((pl.col("provider_geo") == cc) & (pl.col("indicator") == ind))
        return r.row(0, named=True) if r.height else None

    assert get("PT", "places_pop_gt_15000|scope=all_ppl")["value"] == "4"  # Lisbon, Porto + 2 PPLX sections
    assert get("PT", "places_pop_gt_15000|scope=excl_sections_historical")["value"] == "2"
    assert get("PT", "largest_place")["value_text"] == "Lisbon"
    assert get("PT", "capital_pplc_population")["value"] == "517802"
    assert get("BO", "capital_pplc")["value_text"] == "Sucre"  # La Paz is PPLG (seat of government)
    assert get("IL", "capital_pplc_count")["value"] == "0" and get("IL", "capital_pplc") is None
    # PPLC with population 0 (Plymouth, Montserrat): capital named, no population
    assert get("MS", "capital_pplc")["value_text"] == "Plymouth" and get("MS", "capital_pplc_population") is None
    two = [
        C.Place(1, {**places[0].cells, "geonameid": "1", "name": "A", "feature code": "PPLC"}),
        C.Place(2, {**places[0].cells, "geonameid": "2", "name": "B", "feature code": "PPLC"}),
    ]
    rows = {r["indicator"]: r for r in C.country_rows(two, raw_sha256=SHA, retrieved_at=NOW)}
    assert rows["capital_pplc"]["value_text"] == "A | B" and json.loads(rows["capital_pplc"]["flags"])["multiple_pplc"]
    assert "capital_pplc_population" not in rows and rows["capital_pplc_count"]["value"] == "2"


def test_admin1_rows(admin1) -> None:
    f = _validate(C.admin1_rows(admin1, raw_sha256=SHA, retrieved_at=NOW), "geonames", C.DS_ADMIN1, C.GEO_SCHEME)
    pt = f.filter((pl.col("provider_geo") == "PT") & (pl.col("indicator") == "admin1_count"))
    assert int(pt["value"][0]) == sum(1 for a in admin1 if a.country == "PT")
    assert f.filter(pl.col("indicator") == "admin1_name|code=PT.14")["value_text"][0] == "Lisbon"


def _altnames_zip(lines: list[str]) -> bytes:
    return _zip({"alternateNamesV2.txt": "\n".join(lines) + "\n", "iso-languagecodes.txt": "x"})


def test_altnames_filter_and_bound() -> None:
    rows = [
        "1\t2267057\tfr\tLisbonne\t1\t\t\t\t\t",
        "2\t2267057\ten\tLisbon\t1\t\t\t\t\t",
        "3\t2267057\tpt\tLisboa\t1\t\t\t\t\t",  # other language: dropped
        "4\t2267057\tlink\thttps://en.wikipedia.org/wiki/Lisbon\t\t\t\t\t\t",
        "5\t999\tfr\tAilleurs\t\t\t\t\t\t",  # not a cities15000 place
        *[f"{10 + i}\t2267057\tfr\tNom{i}\t\t\t\t{1 if i == 0 else ''}\t\t" for i in range(6)],
        "30\t2267057\ten\tOlisipo\t\t\t\t1\t\t1500",
    ]
    found = list(C.iter_altnames(_altnames_zip(rows), {"2267057"}))
    assert {a.lang for a in found} == {"fr", "en"} and len(found) == 9
    kept, dropped = C.bounded_names(found, per_lang=C.MAX_NAMES_PER_LANG)
    fr = [a.name for a in kept if a.lang == "fr"]
    assert fr[0] == "Lisbonne" and len(fr) == 5 and dropped == 2
    assert "Nom0" not in fr  # colloquial names rank last
    out = C.name_rows(
        kept, {"2267057": "PT"}, raw_sha256=SHA, retrieved_at=NOW, totals={("2267057", "fr"): 7, ("2267057", "en"): 2}
    )
    f = _validate(out, "geonames", C.DS_NAMES, C.GEO_SCHEME)
    old = f.filter(pl.col("value_text") == "Olisipo").row(0, named=True)
    assert json.loads(old["flags"])["historic"] is True and json.loads(old["flags"])["to"] == "1500"
    with pytest.raises(C.GeoNamesCitiesError, match="expected 10"):
        list(C.iter_altnames(_altnames_zip(["1\t2267057\tfr\tX"]), {"2267057"}))


def test_spec_wants_names_only_on_request() -> None:
    class R:
        def __init__(self, dataset: str | None, options: dict[str, str]):
            self.dataset, self.options = dataset, options

    assert not C._wants_names(R(None, {}))
    assert C._wants_names(R(None, {"altnames": "1"}))
    assert C._wants_names(R(C.DS_NAMES, {}))
    assert not C._wants_names(R(C.DS_CITIES, {"altnames": "1"}))
    spec = C.atlas_source()
    assert spec.source_key == "geonames.cities" and spec.allowed_hosts == ("download.geonames.org",)


# ------------------------------------------------------------------------------------------- KG link
def test_kg_specs_are_valid_and_marker_substitution() -> None:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import validate_spec

    allow = load_allowlist()
    for path in (C.SPEC_GEONAMES_IDS, C.SPEC_CAPITALS):
        spec = C._load_spec(path)
        assert validate_spec(spec, allow) == [] and spec.data["enabled"] is False
    spec = C._load_spec(C.SPEC_GEONAMES_IDS)
    q = C.ids_query(spec, ["2267057", "786714"])
    assert 'VALUES ?gn { "2267057" "786714" }' in q and C.IDS_MARKER not in q
    with pytest.raises(ValueError, match="non-numeric"):
        C.ids_query(spec, ['1" } ?x ?y ?z {'])


def test_geonames_statements_only_on_mapped_items(places, admin1) -> None:
    gn = {
        "2267057": ("Q597", "Q597$abc"),
        "2735943": ("Q36433", None),
        "786714": ("Q25270", None),
        "2267056": ("Q244521", None),
    }  # 2267056 = Lisbon district (admin1 PT.14)
    sts, info = C.geonames_statements(
        places, admin1, gn, {"PRT": "Q45", "XKX": "Q1246"}, cities_sha=SHA, admin_sha="b" * 64, retrieved_at=NOW
    )
    by = {(s.subject_qid, s.property_pid): s for s in sts}
    assert by[("Q597", "P1376")].object_qid == "Q45" and ("Q36433", "P1376") not in by  # Porto is PPLA
    assert by[("Q597", "P17")].object_qid == "Q45" and by[("Q25270", "P17")].object_qid == "Q1246"
    assert by[("Q597", "P131")].object_qid == "Q244521"
    assert by[("Q597", "P1082")].value == {"type": "quantity", "amount": "517802", "unit": None}
    st = by[("Q597", "P1376")]
    assert st.qualifiers["subject_mapped_via"] == {"property": "P1566", "wikidata_statement_id": "Q597$abc"}
    assert st.source_key == "geonames.cities" and st.statement_id == "geonames:2267057:P1376"
    assert info["places_mapped"] == 3 and info["places_unmapped"] == 17
    sts2, info2 = C.geonames_statements(places, admin1, gn, {}, cities_sha=SHA, admin_sha="b" * 64, retrieved_at=NOW)
    assert {s.property_pid for s in sts2} == {"P1082", "P131"} and info2["country_qid_missing"] == 3


def test_capital_crosscheck_reports_without_choosing(places) -> None:
    wd = [
        {
            "country": "http://www.wikidata.org/entity/Q45",
            "iso3": "PRT",
            "capital": "http://www.wikidata.org/entity/Q597",
            "gn": "2267057",
            "rank": "http://wikiba.se/ontology#NormalRank",
        },
        # Bolivia: Wikidata lists Sucre and La Paz; GeoNames codes Sucre PPLC
        {
            "country": "http://www.wikidata.org/entity/Q750",
            "iso3": "BOL",
            "capital": "http://www.wikidata.org/entity/Q2971",
            "gn": "3903987",
            "rank": "http://wikiba.se/ontology#PreferredRank",
        },
        {
            "country": "http://www.wikidata.org/entity/Q750",
            "iso3": "BOL",
            "capital": "http://www.wikidata.org/entity/Q1491",
            "gn": "3911925",
            "rank": "http://wikiba.se/ontology#NormalRank",
        },
        # Israel: Wikidata P36 Jerusalem, GeoNames has no PPLC
        {
            "country": "http://www.wikidata.org/entity/Q801",
            "iso3": "ISR",
            "capital": "http://www.wikidata.org/entity/Q1218",
            "gn": "281184",
        },
        # Malaysia: a former capital with an end date is not current
        {
            "country": "http://www.wikidata.org/entity/Q833",
            "iso3": "MYS",
            "capital": "http://www.wikidata.org/entity/Q1865",
            "gn": "1735161",
        },
        {
            "country": "http://www.wikidata.org/entity/Q833",
            "iso3": "MYS",
            "capital": "http://www.wikidata.org/entity/Q1",
            "gn": "1",
            "end": "1999-01-01T00:00:00Z",
        },
        # Kosovo: Wikidata capital without the PPLC's geonameid
        {
            "country": "http://www.wikidata.org/entity/Q1246",
            "iso3": "XKX",
            "capital": "http://www.wikidata.org/entity/Q25270",
            "gn": "786713",
        },
    ]
    out = C.capital_crosscheck(places, wd)
    st = {k: v["status"] for k, v in out["countries"].items()}
    assert st["PRT"] == "agree" and st["MYS"] == "agree" and st["BOL"] == "agree_partial_multiple"
    assert st["ISR"] == "wikidata_only" and st["XKX"] == "disagree"
    assert st["MSR"] == "geonames_only"
    assert len(out["countries"]["BOL"]["wikidata_p36_current"]) == 2
    assert out["summary"]["wikidata_several_current_capitals"] == 1


class _FakeRes:
    def __init__(self, bindings: list[dict[str, Any]]):
        self.bindings = bindings
        self.raw_sha256 = "c" * 64
        self.query_sha256 = "d" * 64
        self.retrieved_at = NOW
        self.variables = ("item", "statement", "gn", "rank")

    def column(self, v: str) -> list[str]:
        return ["2026-10-15T00:00:00Z"]


class _FakeClient:
    def __init__(self, extra_rows: int = 0) -> None:
        self.queries: list[str] = []
        self.extra = extra_rows

    async def sparql(self, query: str, *, meta: Any = None) -> _FakeRes:
        import re

        self.queries.append(query)
        if "dateModified" in query:
            return _FakeRes([])
        ids = re.findall(r'"(\d+)"', query)
        rows = [
            {
                "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/Q{1000 + int(i)}"},
                "statement": {
                    "type": "uri",
                    "value": f"http://www.wikidata.org/entity/statement/Q{1000 + int(i)}-x{i}",
                },
                "gn": {"type": "literal", "value": i},
                "rank": {"type": "uri", "value": "http://wikiba.se/ontology#NormalRank"},
            }
            for i in ids
        ]
        return _FakeRes(rows + rows[:1] * self.extra)


class _Sink:
    def __init__(self) -> None:
        self.batches: list[Any] = []

    def load(self, batch: Any) -> dict[str, Any]:
        self.batches.append(batch)
        return {}


async def test_harvest_p1566_chunks_resumes_and_bounds(tmp_path) -> None:
    client, sink = _FakeClient(), _Sink()
    ids = [str(i) for i in range(1, 8)]
    out = await C.harvest_p1566(client, sink, ids, chunk_size=3, state_dir=tmp_path, max_chunks=2)
    assert out["chunks_total"] == 3 and out["statements"] == 6 and out["chunks_left"] == 1
    s = sink.batches[0].statements[0]
    assert s.property == "P1566" and s.value == {"value": "1"} and s.source_key == "wikidata.wdqs"
    assert s.statement_id == "Q1001$x1"
    again = await C.harvest_p1566(client, sink, ids, chunk_size=3, state_dir=tmp_path)
    assert again["chunks_skipped_done"] == 2 and again["statements"] == 1  # resumed: only the last chunk
    from reva.kg.harvest import RowBoundExceeded

    with pytest.raises(RowBoundExceeded):
        await C.harvest_p1566(_FakeClient(extra_rows=10), _Sink(), ["1", "2"], chunk_size=2, state_dir=tmp_path / "b")


# ------------------------------------------------------------------------------------------- WUP
def _wup(ds: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    body = _fixture(W.CONNECTOR_NAME, W.url_of(ds))
    frame, rep = W.BUILDERS[ds](body, SHA)
    return _validate(frame, "un_wup", ds, W.GEO_SCHEME), rep


def test_wup_urban_rural_projections_and_aggregates() -> None:
    f, _ = _wup(W.DS_URBAN)
    prt = f.filter((pl.col("provider_geo") == "620") & pl.col("indicator").str.starts_with("percPop|cat=Urban"))
    by_year = {r["period_start"].year: r for r in prt.iter_rows(named=True)}
    assert by_year[2025]["indicator"] == "percPop|cat=Urban" and by_year[2025]["iso3"] == "PRT"
    assert by_year[2026]["indicator"] == "percPop|cat=Urban|proj=wup2025"
    assert json.loads(by_year[2026]["flags"])["projection"] is True
    assert json.loads(by_year[2025]["flags"])["projection"] is False
    assert f.filter(pl.col("provider_geo") == "412")["iso3"].unique().to_list() == ["XKX"]
    assert f.filter(pl.col("provider_geo") == "158")["iso3"].unique().to_list() == ["TWN"]
    assert (
        f.filter(pl.col("provider_geo") == "900")["iso3"].null_count()
        == f.filter(pl.col("provider_geo") == "900").height
    )
    world = json.loads(f.filter(pl.col("provider_geo") == "900")["flags"][0])
    assert world["loc_type"] == "World" and "null" not in f["flags"][0]


def test_wup_degurba_units() -> None:
    f, _ = _wup(W.DS_DEGURBA)
    bu = f.filter((pl.col("provider_geo") == "620") & (pl.col("indicator") == "BU_km2|cat=Total"))
    assert bu["unit"].unique().to_list() == ["km2"]
    assert "thousands" in json.loads(bu["flags"][0])["unit_metadata_note"]
    assert set(f["indicator"].str.extract(r"cat=([^|]+)").unique().to_list()) == set(W.DEGURBA_CATEGORIES)


def test_wup_cities_tables() -> None:
    f, _rep = _wup(W.DS_CITIES)
    lis = f.filter(pl.col("indicator") == "Pop|city=217").sort("period_start")
    assert [d.year for d in lis["period_start"]] == [1975, 2025] and lis["unit"].unique().to_list() == ["thousands"]
    flags = json.loads(lis["flags"][0])
    assert flags["city"] == "Lisboa (Lisbon)" and flags["capital"] is True and flags["projection"] is False
    assert f.filter(pl.col("indicator") == "Pop|city=217|proj=wup2025").height == 2  # 2026, 2050
    cen = f.filter(pl.col("indicator") == "pwc_latitude|city=217").row(0, named=True)
    assert cen["period_start"].year == 2025  # latest estimate year
    a, _ = _wup(W.DS_CITIES_AREA)
    assert set(a["indicator"].str.split("|").list.first().unique().to_list()) == {"AREA_km2", "BU_km2", "Pop_density"}


def test_wup_capitals_keep_every_capital() -> None:
    f, rep = _wup(W.DS_CAPITALS)
    assert set(rep["several_national_capitals"]) == {"458", "144"}  # Malaysia, Sri Lanka
    assert f.filter((pl.col("provider_geo") == "458") & (pl.col("indicator") == "national_capital_pop")).height == 0
    my = f.filter((pl.col("provider_geo") == "458") & pl.col("indicator").str.starts_with("name|capital="))
    assert set(my["value_text"].to_list()) >= {"Kuala Lumpur", "Putrajaya"}
    pt = f.filter((pl.col("provider_geo") == "620") & (pl.col("indicator") == "national_capital_pop")).row(
        0, named=True
    )
    assert pt["iso3"] == "PRT" and json.loads(pt["flags"])["city"] == "Lisboa (Lisbon)"
    # a capital that is no DEGURBA settlement (Montserrat's evacuated Plymouth): named, no population
    ms = f.filter(pl.col("provider_geo") == "500")
    assert ms.filter(pl.col("indicator").str.starts_with("Pop|capital=index")).height == 0
    assert ms.filter(pl.col("value_text") == "Plymouth").height == 1
    assert W.atlas_source().licence == "CC BY 3.0 IGO"


def test_wup_refuses_html_and_layout_change() -> None:
    with pytest.raises(W.WupError, match="HTML"):
        W.build_urban_rural(b"<!DOCTYPE html><html></html>", SHA)
    with pytest.raises(W.WupError, match="missing columns"):
        W.build_urban_rural(b"LocID,Year\n4,1950\n", SHA)


def test_priority_ids_capitals_regions_first(places, admin1) -> None:
    ids = C.priority_ids(places, admin1)
    assert len(ids) == len(set(ids)) == len({p.geonameid for p in places} | {a.geonameid for a in admin1})
    first = {p.geonameid for p in places if p.feature_code in ("PPLC", "PPLG")}
    assert set(ids[: len(first)]) == first
    assert ids.index(admin1[0].geonameid) < ids.index("2735943")  # regions before PPLA seats (Porto)


def test_altnames_real_excerpt(places) -> None:
    body = _fixture(C.CONNECTOR_NAME, C.URL_ALTNAMES)
    found = list(C.iter_altnames(body, {p.geonameid for p in places}))
    assert found and {a.lang for a in found} == {"fr", "en"}
    kept, _dropped = C.bounded_names(found)
    lis = [a for a in kept if a.geonameid == "2267057"]
    assert {a.lang for a in lis} == {"fr", "en"} and len([a for a in lis if a.lang == "fr"]) <= C.MAX_NAMES_PER_LANG
    assert "Lisbonne" in {a.name for a in lis if a.lang == "fr"}
