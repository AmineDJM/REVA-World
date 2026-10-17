"""A-HAZARDS: NOAA NCEI Global Historical Hazard Database parser, atlas rows, KG records and coded triggers (offline).

The fixture ``tests/fixtures/source/atlas.noaa_hazards`` holds verbatim NCEI hazard-service v1 responses for the run
option window ``min_year=2018, max_year=2018`` (71 earthquakes, 25 tsunami sources, 927 runups, 15 eruptions) plus
the 8 code-list descriptors; BCE and invalid-date cases use verbatim NCEI records quoted inline.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from reva.adapters.connectors import atlas_noaa_hazards as hz
from reva.atlas.ingest import AtlasRun
from reva.evidence.runner import FixtureHttp

REPO = Path(__file__).resolve().parents[2]
WINDOW = (2018, 2018)

# verbatim NCEI records (hazard-service v1, 2026-10-16)
EQ_2150BC = {
    "id": 1,
    "year": -2150,
    "locationName": "JORDAN:  BAB-A-DARAA,AL-KARAK",
    "latitude": "31.1",
    "longitude": "35.5",
    "eqMagnitude": "7.3",
    "damageAmountOrder": 3,
    "eqMagUnk": "7.3",
    "publish": True,
    "country": "JORDAN",
    "regionCode": 140,
}
VO_4360BC = {
    "id": 5238,
    "year": -4360,
    "volcanoLocationId": 402021,
    "volcanoLocationNewNum": 242021,
    "volcanoLocationNum": "0402-021",
    "name": "Macauley",
    "location": "Kermadec Is",
    "country": "New Zealand",
    "latitude": "-30.21",
    "longitude": "-178.475",
    "elevation": 238,
    "morphology": "Caldera",
    "vei": 6,
    "significant": True,
    "publish": False,
    "eruption": True,
    "status": "Holocene",
    "timeErupt": "U",
}
TS_1854_FEB29 = {
    "id": 3437,
    "year": 1854,
    "month": 2,
    "day": 29,
    "eventValidity": 1,
    "causeCode": 0,
    "numDeposits": 0,
    "country": "FRANCE",
    "locationName": "LIGURIAN SEA",
    "regionCode": 50,
    "numRunups": 0,
    "publish": True,
    "oceanicTsunami": True,
}


@pytest.fixture(scope="module")
def data() -> hz.HazardData:
    async def go() -> hz.HazardData:
        run = AtlasRun.offline(hz.atlas_source()[0], FixtureHttp(hz.NAME))
        d, raws = await hz.fetch(run, WINDOW)
        assert len(raws) == 16  # 1 + 1 + 5 + 1 pages + 8 descriptors
        return d

    return asyncio.run(go())


def _page(
    ep: str, items: list[dict[str, object]], page: int = 1, total_pages: int = 1, total: int | None = None
) -> hz.Page:
    body = json.dumps(
        {
            "items": items,
            "page": page,
            "totalPages": total_pages,
            "itemsPerPage": 200,
            "totalItems": len(items) if total is None else total,
        }
    ).encode()
    return hz.parse_page(ep, body)


def _descriptors() -> dict[str, bytes]:
    fix = REPO / "tests/fixtures/source/atlas.noaa_hazards"
    out = {}
    for e in json.loads((fix / "manifest.json").read_text()):
        for k in hz.DESCRIPTORS:
            if e["url"] == hz.descriptor_url(k):
                out[k] = (fix / e["file"]).read_bytes()
    return out


# ------------------------------------------------------------------------------------------------- time
def test_ncei_bc_years_become_astronomical() -> None:
    assert hz.astronomical_year(-2150) == -2149  # 2150 BC
    assert hz.astronomical_year(-1) == 0  # 1 BC
    assert hz.astronomical_year(2018) == 2018
    with pytest.raises(hz.NoaaHazardsError):
        hz.astronomical_year(0)
    iso, prec, q = hz.event_time(EQ_2150BC)
    assert (iso, prec) == ("-2149-01-01", 9) and q["ncei_year"] == -2150
    assert hz.event_time(VO_4360BC)[:2] == ("-4359-01-01", 9)
    assert hz.event_time({"year": 2010, "month": 1, "day": 12})[:2] == ("2010-01-12", 11)
    assert hz.event_time({"year": 2010, "month": 3})[:2] == ("2010-03-01", 10)
    iso, prec, q = hz.event_time(TS_1854_FEB29)  # 1854 is not a leap year: kept at month precision, noted
    assert (iso, prec) == ("1854-02-01", 10) and "date_note" in q and q["ncei_day"] == 29


def test_kg_time_strings_parse_with_bce_bounds() -> None:
    from reva.kg.model import KgTime, parse_time

    assert parse_time(KgTime("-2149-01-01", 9)) == ("2150-01-01 BC", "2150-12-31 BC")


def test_decimals_are_kept_verbatim_and_plain() -> None:
    doc = hz.parse_json(b'{"a": 6.60, "b": 1E+3, "c": 316000, "d": 0.01}')
    assert doc["a"] == "6.60" and hz.dec(doc["a"]) == "6.6"
    assert hz.dec(doc["b"]) == "1000" and hz.dec(doc["c"]) == "316000" and hz.dec(doc["d"]) == "0.01"
    assert hz.dec(None) is None and hz.dec(True) is None and hz.dec("x") is None


# ------------------------------------------------------------------------------------------------- paging
def test_page_errors_are_refused() -> None:
    with pytest.raises(hz.NoaaHazardsError, match="formErrors"):
        hz.parse_page("earthquakes", b'{"flashErrors":["Invalid Request"],"formErrors":{"itemsPerPage":["x"]}}')
    with pytest.raises(hz.NoaaHazardsError, match="integer id"):
        _page("earthquakes", [{"id": "1", "year": 1}])


def test_assemble_checks_completeness() -> None:
    desc = _descriptors()
    eq = [dict(EQ_2150BC)]
    ok = {
        "earthquakes": [_page("earthquakes", eq)],
        "tsunamis": [_page("tsunamis", [])],
        "runups": [_page("runups", [])],
        "volcanoes": [_page("volcanoes", [])],
    }
    d = hz.assemble(ok, desc, strict=False)
    assert len(d.records["earthquakes"]) == 1 and d.revision().startswith("hazel-v1:")
    with pytest.raises(hz.NoaaHazardsError, match="totalItems"):
        hz.assemble({**ok, "earthquakes": [_page("earthquakes", eq, total=2)]}, desc, strict=False)
    with pytest.raises(hz.NoaaHazardsError, match="pages"):
        hz.assemble({**ok, "earthquakes": [_page("earthquakes", eq, total_pages=2, total=1)]}, desc, strict=False)
    dup = [_page("earthquakes", eq, 1, 2, 2), _page("earthquakes", eq, 2, 2, 2)]
    with pytest.raises(hz.NoaaHazardsError, match="duplicate"):
        hz.assemble({**ok, "earthquakes": dup}, desc, strict=False)
    with pytest.raises(hz.NoaaHazardsError, match="truncated"):  # a full live run must be complete
        hz.assemble(ok, desc, strict=True)


def test_fixture_window_contents(data: hz.HazardData) -> None:
    assert {k: len(v) for k, v in data.records.items()} == {
        "earthquakes": 71,
        "tsunamis": 25,
        "runups": 927,
        "volcanoes": 15,
    }
    assert data.descriptors["tsunami_causes"]["1"] == "Earthquake"
    assert data.descriptors["effect_people"]["3"] == "Many (~101 to 1000 people)"
    assert "page=1" in hz.page_url("runups", 1, WINDOW) and "sourceMinYear=2018" in hz.page_url("runups", 1, WINDOW)
    assert hz.page_url("earthquakes", 3) == f"{hz.API}/earthquakes?page=3"


# ------------------------------------------------------------------------------------------------- atlas rows
def test_country_year_rows_are_exact_sums(data: hz.HazardData) -> None:
    rows = hz.country_year_rows(data)
    by = {(r["indicator"], r["provider_geo"], r["year"]): r for r in rows}
    idn = [r for r in data.records["earthquakes"] if r.data.get("country") == "INDONESIA"]
    cell = by[("eq_deaths", "IDN", 2018)]
    expected = sum((Decimal(str(r.data["deaths"])) for r in idn if r.data.get("deaths") is not None), Decimal(0))
    assert cell["value"] == hz.dec(expected) and cell["iso3"] == "IDN"
    flags = json.loads(cell["flags"])
    assert flags["records"] == len(idn) and flags["records_with_value"] + flags["records_without_value"] == len(idn)
    assert by[("eq_events", "IDN", 2018)]["value"] == str(len(idn))
    sel = json.loads(cell["selector"])
    assert sel["field"] == "deaths" and set(sel["record_ids"]) <= {r.id for r in idn}
    # runup deaths are attributed to the country where the runup was observed
    ru = [
        r for r in data.records["runups"] if r.data.get("country") == "INDONESIA" and r.data.get("deaths") is not None
    ]
    assert by[("ts_runup_deaths", "IDN", 2018)]["value"] == hz.dec(sum(Decimal(str(r.data["deaths"])) for r in ru))
    # no filled zeros, no cross-hazard sums, strict decimals
    assert all(r["value"] is not None and Decimal(r["value"]) >= 0 for r in rows)
    assert {r["indicator"] for r in rows} <= set(hz.INDICATORS)
    # unmapped NCEI names are kept with their (upper-cased) NCEI name
    usa_t = [r for r in rows if r["provider_geo"] == "USA TERRITORY"]
    assert usa_t and all(r["iso3"] is None for r in usa_t)


def test_validity_filters_on_tsunami_counts(data: hz.HazardData) -> None:
    rows = hz.country_year_rows(data)
    total = sum(int(r["value"]) for r in rows if r["indicator"] == "ts_events")
    valid = sum(int(r["value"]) for r in rows if r["indicator"] == "ts_events_valid34")
    recs = data.records["tsunamis"]
    assert total == sum(1 for r in recs if r.data.get("eventValidity") != -1)
    assert valid == sum(1 for r in recs if r.data.get("eventValidity") in (3, 4))


def test_atlas_frame_validates_with_bce_years() -> None:
    from reva.atlas.tables import validate_atlas_frame

    d = hz.assemble(
        {
            "earthquakes": [_page("earthquakes", [dict(EQ_2150BC)])],
            "tsunamis": [_page("tsunamis", [])],
            "runups": [_page("runups", [])],
            "volcanoes": [_page("volcanoes", [dict(VO_4360BC)])],
        },
        _descriptors(),
        strict=False,
    )
    rows = hz.country_year_rows(d)
    frame = hz.atlas_frame(rows)
    assert {(r["indicator"], r["provider_geo"], r["year"]) for r in rows} == {
        ("eq_events", "JOR", -2149),
        ("vo_eruptions", "NZL", -4359),
    }
    assert str(frame["period_start"].cast(str).to_list()[0]).startswith("-2149-01-01")
    validate_atlas_frame(
        frame,
        source=hz.ATLAS_SOURCE,
        dataset=hz.DATASET,
        raw_sha256s=sorted({r["raw_sha256"] for r in rows}),
        geo_scheme=hz.GEO_SCHEME,
    )


# ------------------------------------------------------------------------------------------------- KG
def test_kg_records(data: hz.HazardData) -> None:
    ents, sts, rep = hz.kg_records(data, country_qids={"IDN": "Q252", "PNG": "Q691"})
    assert rep["entities"] == {"earthquake": 71, "tsunami": 25, "volcanic_eruption": 15, "volcano": 12}
    ids = [s.statement_id for s in sts]
    assert len(ids) == len(set(ids))
    assert len({e.qid for e in ents}) == len(ents)
    palu = [s for s in sts if s.subject_qid == hz.ts_qid(5689)]  # Sulawesi (Palu) 2018-09-28
    pid = {s.property_pid for s in palu}
    assert {"P585", "P625", "ncei:tsunami_cause", "ncei:associated_earthquake", "ncei:runup_country"} <= pid
    date = next(s for s in palu if s.property_pid == "P585")
    assert date.value == {"type": "time", "time": "2018-09-28", "precision": 11}
    link = next(s for s in palu if s.property_pid == "ncei:associated_earthquake")
    assert link.object_qid == hz.eq_qid(10369) and "not a causal claim" in link.qualifiers["nature"]
    # every statement cites its raw page and its record
    assert all(s.raw_sha256 and s.references[0]["selector"]["id"] for s in sts)
    # impacts: number + NCEI description code with its decoded scale text
    deaths = [s for s in sts if s.property_pid == "ncei:deaths_description"]
    assert deaths and all(s.qualifiers["description"] for s in deaths)
    # GVP volcano number as an external id on the volcano entity
    krak = [
        s
        for s in sts
        if s.subject_qid.startswith(f"{hz.QID_NS}:volcano:")
        and s.property_pid == "ncei:gvp_volcano_number"
        and s.value["value"] == "262000"
    ]
    assert len(krak) == 1


def test_kg_bce_event_dates() -> None:
    d = hz.assemble(
        {
            "earthquakes": [_page("earthquakes", [dict(EQ_2150BC)])],
            "tsunamis": [_page("tsunamis", [])],
            "runups": [_page("runups", [])],
            "volcanoes": [_page("volcanoes", [])],
        },
        _descriptors(),
        strict=False,
    )
    ents, sts, _ = hz.kg_records(d, country_qids={"JOR": "Q810"})
    assert ents[0].labels["en"].startswith("Earthquake 2150 BC")
    date = next(s for s in sts if s.property_pid == "P585")
    assert date.value["time"] == "-2149-01-01" and date.value["precision"] == 9
    country = next(s for s in sts if s.property_pid == "ncei:country")
    assert country.object_qid == "Q810" and country.qualifiers["ncei_country"] == "JORDAN"


def test_kg_statements_pass_model_validation(data: hz.HazardData) -> None:
    from reva.kg.model import _normalise_statement

    _, sts, _ = hz.kg_records(data, country_qids={"IDN": "Q252"})
    kinds = {p: k for p, (k, *_rest) in hz.PREDICATES.items()} | {"P585": "time", "P625": "globe_coordinate"}
    raws = set(data.all_shas())
    for i, s in enumerate(sts):
        _normalise_statement(i, s, hz.SOURCE_KEY, raws, kinds)


# ------------------------------------------------------------------------------------------------- coded triggers
def test_coded_triggers_follow_ncei_codes(data: hz.HazardData) -> None:
    links, rep = hz.coded_links(data)
    pairs = {(lk.cause_id, lk.effect_id): lk for lk in links}
    # Sulawesi (Palu) 2018-09-28: earthquake 10369 -> tsunami 5689 (cause code 3 Earthquake and Landslide)
    assert pairs[(hz.eq_qid(10369), hz.ts_qid(5689))].codebook_term == "cause_code:3"
    # Bali Sea (Lombok) 2018-08-05: earthquake 10351 -> tsunami 5685 (cause code 1 Earthquake)
    assert pairs[(hz.eq_qid(10351), hz.ts_qid(5685))].codebook_term == "cause_code:1"
    # Krakatau eruption 7264 -> tsunami 5699 (cause code 7 Volcano and Landslide)
    assert pairs[(hz.eruption_qid(7264), hz.ts_qid(5699))].codebook_term == "cause_code:7"
    # Kilauea 2018-05-04: cause code 4 (Volcano and Earthquake) -> both coded triggers
    assert (hz.eq_qid(10332), hz.ts_qid(5678)) in pairs and (hz.eruption_qid(6864), hz.ts_qid(5678)) in pairs
    # Sumisujima eruption 7541 is linked to tsunami 5796 whose cause code is 1 (Earthquake): context only
    assert (hz.eruption_qid(7541), hz.ts_qid(5796)) not in pairs
    assert rep["volcano_link_context_only_cause_code_1"] == 1
    assert rep["earthquake_to_tsunami_claims"] == 16 and rep["volcano_to_tsunami_claims"] == 3
    lk = pairs[(hz.eq_qid(10351), hz.ts_qid(5685))]
    assert lk.selector["columns"]["earthquakeEventId"] == 10351 and lk.selector["reciprocal"]["tsunamiEventId"] == 5685
    defs = hz.codebook_definitions(data)
    assert defs["cause_code:7"].endswith('7 = "Volcano and Landslide" (NCEI descriptor tsunami/causes)')
    assert all(lk.codebook_term in defs for lk in links)


def test_no_claim_for_erroneous_entries_or_other_causes() -> None:
    eq = {"id": 10, "year": 1900, "tsunamiEventId": 20, "country": "JAPAN"}
    vo = {"id": 30, "year": 1900, "tsunamiEventId": 21, "name": "X", "volcanoLocationId": 1, "country": "Japan"}
    ts = [
        {"id": 20, "year": 1900, "earthquakeEventId": 10, "causeCode": 1, "eventValidity": -1, "country": "JAPAN"},
        {"id": 21, "year": 1900, "volcanoEventId": 30, "causeCode": 8, "eventValidity": 4, "country": "JAPAN"},
    ]
    d = hz.assemble(
        {
            "earthquakes": [_page("earthquakes", [eq])],
            "tsunamis": [_page("tsunamis", ts)],
            "runups": [_page("runups", [])],
            "volcanoes": [_page("volcanoes", [vo])],
        },
        _descriptors(),
        strict=False,
    )
    links, rep = hz.coded_links(d)
    assert links == []
    assert rep == {"earthquake_link_skipped_erroneous_entry": 1, "volcano_link_context_only_cause_code_8": 1}


def test_claims_carry_ncei_validity_codes_verbatim() -> None:
    """Audit H4 (AUDFIX): every claim carries NCEI's eventValidity and causeCode with the archived descriptor label
    and the registry's certainty reading; "Erroneous entry" gives no claim; doubtful codes stay claims."""
    eqs = [{"id": 10 + i, "year": 1900, "tsunamiEventId": 20 + i, "country": "JAPAN"} for i in range(5)]
    ts = [
        {"id": 20, "year": 1900, "earthquakeEventId": 10, "causeCode": 1, "eventValidity": 0, "country": "JAPAN"},
        {"id": 21, "year": 1900, "earthquakeEventId": 11, "causeCode": 2, "eventValidity": 1, "country": "JAPAN"},
        {"id": 22, "year": 1900, "earthquakeEventId": 12, "causeCode": 1, "eventValidity": 2, "country": "JAPAN"},
        {"id": 23, "year": 1900, "earthquakeEventId": 13, "causeCode": 3, "eventValidity": 4, "country": "JAPAN"},
        {"id": 24, "year": 1900, "earthquakeEventId": 14, "causeCode": 1, "eventValidity": -1, "country": "JAPAN"},
    ]
    d = hz.assemble(
        {
            "earthquakes": [_page("earthquakes", eqs)],
            "tsunamis": [_page("tsunamis", ts)],
            "runups": [_page("runups", [])],
            "volcanoes": [_page("volcanoes", [])],
        },
        _descriptors(),
        strict=False,
    )
    links, rep = hz.coded_links(d)
    by = {lk.effect_id: {e["field"]: e for e in lk.source_validity} for lk in links}
    assert hz.ts_qid(24) not in by and rep["earthquake_link_skipped_erroneous_entry"] == 1
    assert by[hz.ts_qid(20)]["eventValidity"]["label"] == "Event that only caused a seiche"
    assert by[hz.ts_qid(21)]["eventValidity"] == {
        "field": "eventValidity",
        "code": "1",
        "label": "Very Doubtful Tsunami",
        "codelist": "NCEI descriptor tsunami/validity",
        "codelist_sha256": d.descriptor_shas["tsunami_validity"],
        "certainty": "doubtful",
    }
    assert by[hz.ts_qid(21)]["causeCode"]["label"] == "Questionable Earthquake"
    assert by[hz.ts_qid(21)]["causeCode"]["certainty"] == "doubtful"
    assert by[hz.ts_qid(22)]["eventValidity"]["certainty"] == "doubtful"
    assert by[hz.ts_qid(23)]["eventValidity"]["certainty"] == "definite_or_probable"
    assert by[hz.ts_qid(23)]["causeCode"]["certainty"] == "definite_or_probable"
    assert rep["claims_eventValidity_doubtful"] == 3 and rep["claims_causeCode_doubtful"] == 1
    # the definitions are the archived descriptor labels only (audit L5: no typed help-page prefix)
    defs = hz.codebook_definitions(d)
    assert defs["cause_code:2"] == 'causeCode 2 = "Questionable Earthquake" (NCEI descriptor tsunami/causes)'


def test_validity_registry_labels_equal_the_archived_descriptors() -> None:
    """Every registry label is the NCEI descriptor label verbatim (a mismatch would make the code "unclassified")."""
    desc = {k: hz.parse_descriptor(b) for k, b in _descriptors().items()}
    reg = hz.validity_registry()
    for fld, key in hz.VALIDITY_FIELDS.items():
        for code, entry in reg[fld]["codes"].items():
            assert desc[key][str(code)] == entry["label"], (fld, code)


def test_changed_validity_label_is_unclassified_not_guessed() -> None:
    eq = {"id": 10, "year": 1900, "tsunamiEventId": 20, "country": "JAPAN"}
    ts = [{"id": 20, "year": 1900, "earthquakeEventId": 10, "causeCode": 1, "eventValidity": 3, "country": "JAPAN"}]
    desc = _descriptors()
    desc["tsunami_validity"] = desc["tsunami_validity"].replace(b"Probable Tsunami", b"Likely Tsunami")
    d = hz.assemble(
        {
            "earthquakes": [_page("earthquakes", [eq])],
            "tsunamis": [_page("tsunamis", ts)],
            "runups": [_page("runups", [])],
            "volcanoes": [_page("volcanoes", [])],
        },
        desc,
        strict=False,
    )
    links, _ = hz.coded_links(d)
    sv = {e["field"]: e for e in links[0].source_validity}
    assert sv["eventValidity"]["label"] == "Likely Tsunami" and sv["eventValidity"]["certainty"] == "unclassified"


# ------------------------------------------------------------------------------------------------- geography
def test_geo_alias_file_is_evidenced_and_covers_the_fixture(data: hz.HazardData) -> None:
    doc = yaml.safe_load((REPO / "registry/atlas/geo_aliases/a_hazards.yaml").read_text())
    assert doc["scheme"] == hz.GEO_SCHEME and doc["base"] == "iso3"
    codes = Counter(a["code"] for a in doc["aliases"])
    assert max(codes.values()) == 1
    assert all(
        a["source"].startswith(("exact normalised name match", "manual (agent A-HAZARDS")) for a in doc["aliases"]
    )
    assert hz.unmapped_names(data) == {"USA TERRITORY": 2}
    from reva.atlas.tables import country_iso3

    for name, iso in (
        ("Solomon Is.", "SLB"),
        ("CONGO, DRC", "COD"),
        ("CONGO", "COG"),
        ("Turkey", "TUR"),
        ("PACIFIC OCEAN", None),
        ("KOREA", None),
        ("USA TERRITORY", None),
    ):
        assert country_iso3(hz.country_key(name), hz.GEO_SCHEME) == iso, name


# ------------------------------------------------------------------------------------------------- Wikidata xref
def test_wikidata_same_as_only_for_unambiguous_shared_gvp_numbers() -> None:
    from datetime import UTC, datetime

    body = json.loads((REPO / "tests/fixtures/source/atlas.noaa_hazards_wikidata/results.json").read_text())
    gvp = {"273030": [hz.volcano_qid(273030)], "262000": [hz.volcano_qid(262000)], "999999": [hz.volcano_qid(1)]}
    sts, rep = hz.wikidata_statements(
        body["results"]["bindings"],
        gvp=gvp,
        raw_sha256="0" * 64,
        retrieved_at=datetime(2026, 10, 16, tzinfo=UTC),
        query_sha256="1" * 64,
    )
    assert [(s.subject_qid, s.object_qid) for s in sts] == [(hz.volcano_qid(273030), "Q1484")]  # Mayon
    assert rep["gvp_number_on_several_items"] == 1  # 262000: two Wikidata items carry it -> no statement
    assert rep["ncei_gvp_number_not_on_wikidata"] == 1
    assert "P1886" in sts[0].qualifiers["evidence"]


# ------------------------------------------------------------------------------------------------- specs
def test_atlas_sources_are_discovered() -> None:
    from reva.atlas.sources import discover

    found, failed = discover([hz.__name__])
    assert set(found) == {hz.NAME, hz.WD_NAME} and not failed
    main, wd = hz.atlas_source()
    assert main.source_key == "noaa.ncei_hazards" and main.allowed_hosts == ("www.ngdc.noaa.gov",)
    assert wd.budget_key == "wikidata.wdqs" and wd.allowed_hosts == ("query.wikidata.org",)
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/a_hazards.yaml").read_text())
    assert req["requests"][0]["source_key"] == main.source_key
    assert req["requests"][0]["daily_requests"] >= 2 * main.planned_requests
    src = yaml.safe_load((REPO / "registry/sources/noaa_hazards.yaml").read_text())
    assert src["source_key"] == main.source_key and src["metadata"]["allowed_hosts"] == list(main.allowed_hosts)


def test_catalog_concepts_match_indicators() -> None:
    doc = yaml.safe_load((REPO / "registry/atlas/catalog/hazard_events.yaml").read_text())
    ids = {c["id"] for c in doc["concepts"]}
    assert ids == {f"hazard_events.{i}" for i in hz.INDICATORS}
    for c in doc["concepts"]:
        ind = c["id"].split(".", 1)[1]
        assert c["source_order"] == [f"ncei_hazards.country_year.{ind}"]
        assert c["unit"] == hz.INDICATORS[ind][3] and c["definition"].startswith("COMPUTED")
