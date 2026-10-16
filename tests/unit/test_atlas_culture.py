"""A-CULTURE atlas sources, offline: ICH lists, World Heritage KG links, UIS culture, Olympic medal tables."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from reva.adapters.connectors import atlas_culture as ac
from reva.adapters.connectors import atlas_unesco_whc as whc
from reva.adapters.connectors.atlas_geo_common import load_country_qids
from reva.atlas.sources import discover
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
RETRIEVED = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
SHA = "a" * 64


def _get(name: str, url: str) -> bytes:
    async def go() -> bytes:
        return (await FixtureHttp(name).get(url)).body

    return asyncio.run(go())


@pytest.fixture(scope="module")
def ich() -> tuple[dict, list[ac.IchElement], dict]:
    meta = ac.parse_ods_metadata(_get(ac.ICH_NAME, ac.ICH_META_URL), ac.ICH_DATASET)
    els, info = ac.parse_ich_export(_get(ac.ICH_NAME, ac.ICH_EXPORT_URL), min_expected=1)
    return meta, els, info


@pytest.fixture(scope="module")
def qids() -> dict[str, str]:
    q = load_country_qids()
    if not q:
        pytest.skip("registry/entities/country_qids.json not available")
    return q


def test_sources_are_discovered_and_valid() -> None:
    sources, errors = discover(["reva.adapters.connectors.atlas_culture", "reva.adapters.connectors.atlas_olympics"])
    assert errors == []
    assert set(sources) == {ac.ICH_NAME, ac.WHC_LINKS_NAME, ac.UIS_NAME, "atlas.olympics_medals"}
    assert sources[ac.WHC_LINKS_NAME].spec.budget_key == "unesco.whc"
    assert sources[ac.UIS_NAME].spec.budget_key == "unesco.uis"
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    assert {e.spec.source_key for e in sources.values()} <= keys


def test_ich_metadata_licence_and_revision(ich) -> None:
    meta, els, info = ich
    assert meta["licence"] == "CC BY-SA 4.0" and meta["modified"].startswith("2026-")
    assert info["elements"] == len(els) == 20 and info["multinational"] == 7
    doc = json.loads(_get(ac.ICH_NAME, ac.ICH_META_URL))
    doc["metas"]["default"]["license"] = "All rights reserved"
    with pytest.raises(SourceResponseError) as e:
        ac.parse_ods_metadata(json.dumps(doc).encode(), ac.ICH_DATASET)
    assert e.value.details["kind"] == "licence"


def test_ich_live_threshold_refuses_a_truncated_export() -> None:
    with pytest.raises(SourceResponseError) as e:
        ac.parse_ich_export(_get(ac.ICH_NAME, ac.ICH_EXPORT_URL))  # live threshold: 700 elements
    assert e.value.details["kind"] == "incomplete"


def test_ich_rejects_malformed_records() -> None:
    good = json.loads(_get(ac.ICH_NAME, ac.ICH_EXPORT_URL))
    for patch in ({"type_acronym": "XX"}, {"countries": []}, {"countries": ["FRA"]}, {"inscription_year": "20x5"}):
        bad = [dict(good[0], **patch)]
        with pytest.raises(SourceResponseError):
            ac.parse_ich_export(json.dumps(bad).encode(), min_expected=1)
    with pytest.raises(SourceResponseError):
        ac.parse_ich_export(json.dumps([good[0], good[0]]).encode(), min_expected=1)


def test_ich_atlas_rows_are_counts_per_state(ich) -> None:
    meta, els, _ = ich
    rows = ac.ich_atlas_rows(els, raw_sha256=SHA, source_revision=meta["modified"], retrieved_at=RETRIEVED)
    frame, unmapped = validate_atlas_frame(
        ac.atlas_frame(rows), source="unesco_ich", dataset="ich001", raw_sha256s=[SHA], geo_scheme="iso2"
    )
    assert unmapped == []

    def val(iso3: str, ind: str) -> str:
        f = frame.filter((frame["iso3"] == iso3) & (frame["indicator"] == ind))
        assert f.height == 1, (iso3, ind)
        return f["value"][0]

    assert val("JPN", "ich_elements_total") == "4" and val("JPN", "ich_elements_multinational") == "0"
    assert val("VNM", "ich_elements_usl") == "1"
    assert val("FRA", "ich_elements_total") == "2" and val("FRA", "ich_elements_multinational") == "1"
    yearly = frame.filter((frame["iso3"] == "JPN") & (frame["indicator"] == "ich_elements_inscribed_in_year"))
    assert sorted(zip(yearly["period_start"].dt.year().to_list(), yearly["value"].to_list())) == [
        (2024, "1"),
        (2025, "3"),
    ]
    flags = json.loads(yearly["flags"][0])
    assert flags["country_may_have_joined_by_extension"] is True and flags["aggregation"] == "count"
    assert not frame.filter(frame["iso3"] == "USA").height  # absent from the lists: no row, never a zero


def test_ich_kg_links_multinational_elements_through_countries(ich, qids) -> None:
    meta, els, _ = ich
    b = ac.ich_kg(els, qids=qids, raw_sha256=SHA, revision=meta["modified"], retrieved="2026-10-16")
    dry = [s for s in b.statements if s.subject_qid == "ich:2106"]  # dry stone walling, 13 States
    countries = {s.object_qid for s in dry if s.property_pid == "P17"}
    assert {qids["FRA"], qids["ITA"], qids["CHE"]} <= countries and len(countries) == 13
    lst = [s for s in dry if s.property_pid == "P1435"]
    assert len(lst) == 1 and lst[0].object_qid == "unesco:ich_list:RL" and lst[0].start.iso == "2024"
    links = [s for s in b.statements if s.property_pid == "unesco:associated_whc_property"]
    assert links and all(s.object_qid.startswith("whc:") for s in links)
    assert {e.qid for e in b.entities} >= {"ich:2106", "unesco:ich_list:RL", "unesco:ich_list:USL"}
    keys = [s.statement_id for s in b.statements]
    assert len(keys) == len(set(keys))
    for u in b.unmapped_countries:  # reported, never guessed
        assert u["reason"] in ("no ISO3->QID in registry", "iso2 not in crosswalk")
        assert not any(s.statement_id == f"ich:{u['element']}:country:{u['iso2']}" for s in b.statements)


def test_whc_kg_transboundary_and_unmapped(qids) -> None:
    mi, xi = whc.fetch_items()
    meta = whc.parse_metadata(_get(ac.WHC_LINKS_NAME, mi.url))
    props, _ = whc.parse_export(_get(ac.WHC_LINKS_NAME, xi.url))
    b = ac.whc_kg(props, qids=qids, raw_sha256=SHA, revision=meta["modified"], retrieved="2026-10-15")
    spa = {s.object_qid for s in b.statements if s.subject_qid == "whc:1613" and s.property_pid == "P17"}
    assert len(spa) == 7 and qids["CZE"] in spa  # The Great Spa Towns of Europe
    des = next(s for s in b.statements if s.statement_id == "whc:1613:designation")
    assert des.object_qid == ac.WHC_SITE_QID and des.qualifiers["transboundary"] is True
    assert next(s for s in b.statements if s.statement_id == "whc:1613:id").value == {
        "type": "external_id",
        "value": "1613",
    }
    jer = [u for u in b.unmapped_countries if u["element"] == "148"]
    assert jer and "no State Party code" in jer[0]["reason"]
    assert not [s for s in b.statements if s.subject_qid == "whc:148" and s.property_pid == "P17"]
    danger = next(s for s in b.statements if s.statement_id == "whc:1810:designation")
    assert danger.qualifiers["in_danger"] is True and danger.qualifiers["danger_list_since"] == 2026


def _uis_body() -> tuple[str, bytes]:
    from reva.adapters.connectors.atlas_uis import VERSION_URL

    v = ac.parse_uis_version(_get(ac.UIS_NAME, VERSION_URL))
    return v["version"], _get(ac.UIS_NAME, ac.uis_data_url(v["version"]))


def test_uis_culture_rows_never_fill_zero_for_not_applicable() -> None:
    version, body = _uis_body()
    rows, info = ac.uis_rows(body, raw_sha256=SHA, version=version, item_key="uis.culture")
    frame, unmapped = validate_atlas_frame(
        ac.atlas_frame(rows), source="uis", dataset="culture", raw_sha256s=[SHA], geo_scheme="iso3"
    )
    assert unmapped == [] and info["renamed_indicators"] == {}
    na = frame.filter(frame["flags"].str.contains('"magnitude":"NA"'))
    assert na.height > 0 and na["value"].null_count() == na.height
    assert all(json.loads(f)["missing"] == "not_applicable" for f in na["flags"])
    nil = frame.filter(frame["flags"].str.contains('"magnitude":"NIL"'))
    assert nil.height > 0 and set(nil["value"].to_list()) == {"0"}  # nil or negligible: the provider's 0
    cin = frame.filter((frame["iso3"] == "FRA") & (frame["indicator"] == "220174"))
    assert cin.height > 0 and cin["unit"][0] == "count"


def test_uis_culture_refuses_unrequested_indicators() -> None:
    version, body = _uis_body()
    doc = json.loads(body)
    doc["records"][0]["indicatorId"] = "GER.1"
    with pytest.raises(SourceResponseError):
        ac.uis_rows(json.dumps(doc).encode(), raw_sha256=SHA, version=version, item_key="x")


def test_catalog_covers_every_series() -> None:
    cat = yaml.safe_load((REPO / "registry/atlas/catalog/culture.yaml").read_text())
    series = {s for c in cat["concepts"] for s in c["source_order"]}
    for i in ac.UIS_CULTURE:
        assert f"uis.culture.{i.code}" in series
    for ind in (*ac.ICH_SNAPSHOT, ac.ICH_YEARLY[0]):
        assert f"unesco_ich.ich001.{ind}" in series
    ids = {c["id"] for c in cat["concepts"]}
    assert {i.concept for i in ac.UIS_CULTURE} <= ids


# ----------------------------------------------------------------------------- Olympic medal tables
from reva.adapters.connectors import atlas_olympics as ao  # noqa: E402


@pytest.fixture(scope="module")
def olympics() -> tuple[list[ao.MedalTable], dict]:
    [turl] = ao.table_urls(ao.FIXTURE_GAMES)
    [aurl] = ao.article_urls(ao.FIXTURE_GAMES)
    tables = ao.parse_table_response(_get(ao.NAME, turl), expected=ao.FIXTURE_GAMES)
    return tables, ao.parse_article_qids(_get(ao.NAME, aurl))


def test_template_params_ignore_nested_pipes() -> None:
    block = "Medals table | caption = x<ref>{{cite web|url=a|title=b}}</ref> | host = [[France|FRA]] | gold_FRA = 3 "
    p = ao.template_params(block + "| name_RUS = {{flagIOC|RU1|1912 Summer}} <!-- c|d -->")
    assert p["caption"] == "x" and p["host"] == "[[France|FRA]]" and p["gold_FRA"] == "3"
    assert p["name_RUS"] == "{{flagIOC|RU1|1912 Summer}}"


def test_medal_tables_fixture(olympics) -> None:
    tables, qids = olympics
    by = {t.games: t for t in tables}
    t1912 = by[ao.Games(1912, "summer")]
    rus = next(r for r in t1912.rows if r.code == "RUS")
    assert rus.geo == "RU1"  # the template displays the Russian Empire's code
    t2024 = by[ao.Games(2024, "summer")]
    ain = next(r for r in t2024.rows if r.code == "AIN")
    assert ain.skip and not ain.host
    fra = next(r for r in t2024.rows if r.code == "FRA")
    assert fra.host and (fra.gold, fra.silver, fra.bronze) == (16, 26, 22)
    assert set(qids) == set(ao.FIXTURE_GAMES) and all(q.startswith("Q") for q in qids.values())


def test_medal_atlas_rows(olympics) -> None:
    tables, _ = olympics
    rows = ao.atlas_rows(tables, raw_sha256s={t.page_title: SHA for t in tables})
    frame, unmapped = validate_atlas_frame(
        ao.frame(rows), source="olympics", dataset="medal_tables", raw_sha256s=[SHA], geo_scheme="ioc_noc"
    )
    unm = {u["provider_geo"] for u in unmapped}
    assert {"RU1", "ROC", "AIN"} <= unm and "RUS" not in unm  # never given to a successor
    chl = frame.filter(frame["provider_geo"] == "CHI")
    assert chl.height == 0 or set(chl["iso3"].to_list()) == {"CHL"}  # IOC CHI = Chile, not Channel Islands
    tokyo = frame.filter((frame["iso3"] == "JPN") & (frame["indicator"] == "games_host|season=summer"))
    assert tokyo["period_start"].dt.year().to_list() == [2021]
    assert json.loads(tokyo["flags"][0])["held_in"] == 2021
    usa = frame.filter((frame["iso3"] == "USA") & (frame["period_start"].dt.year() == 2024))
    vals = {r["indicator"]: int(r["value"]) for r in usa.iter_rows(named=True)}
    assert (
        vals["total|season=summer"]
        == vals["gold|season=summer"] + vals["silver|season=summer"] + vals["bronze|season=summer"]
    )
    roc60 = frame.filter((frame["provider_geo"] == "ROC") & (frame["period_start"].dt.year() == 1960))
    assert json.loads(roc60["flags"][0])["unmapped_noc"] == "Republic of China (IOC code ROC)"


def test_host_without_medals_gets_no_medal_cells() -> None:
    page = {"title": "x", "pageid": 1, "revisions": [{"revid": 2, "timestamp": "t", "slots": {"main": {"content":
            "{{Medals table | event = 1976 Summer | host = CAN | gold_USA = 1 | silver_USA = 0 | bronze_USA = 0 }}"}}}]}  # fmt: skip
    t = ao.parse_medal_table(ao.Games(1976, "summer"), page)
    rows = ao.atlas_rows([t], raw_sha256s={"x": SHA})
    can = [r for r in rows if r["provider_geo"] == "CAN"]
    assert [r["indicator"] for r in can] == ["games_host|season=summer"]
    bad = {**page, "revisions": [{"revid": 2, "slots": {"main": {"content": "{{Medals table | gold_USA = 1 }}"}}}]}
    with pytest.raises(SourceResponseError):
        ao.parse_medal_table(ao.Games(1976, "summer"), bad)


def test_medal_kg(olympics, qids) -> None:
    tables, gq = olympics
    b = ao.kg_build(tables, games_qids=gq, country_qids=qids, raw_sha256s={t.page_title: SHA for t in tables},
                    retrieved="2026-10-16")  # fmt: skip
    g2024 = gq[ao.Games(2024, "summer")]
    host = [s for s in b.statements if s.subject_qid == g2024 and s.property_pid == "P17"]
    assert [s.object_qid for s in host] == [qids["FRA"]]
    ents = {e.qid for e in b.entities}
    assert {"ioc:ROC.1956-1976", "ioc:ROC.2020-2022", "ioc:RU1", "ioc:AIN"} <= ents
    part = [s for s in b.statements if s.subject_qid == qids["USA"] and s.object_qid == g2024]
    assert len(part) == 1 and part[0].property_pid == "P1344" and part[0].qualifiers["gold"].isdigit()
    keys = [s.statement_id for s in b.statements]
    assert len(keys) == len(set(keys))


def test_games_option() -> None:
    class R:
        origin = "live"

        def __init__(self, games: str) -> None:
            self.options = {"games": games}

    assert ao.select_games(R("1960s,2026w")) == (ao.Games(1960, "summer"), ao.Games(2026, "winter"))
    with pytest.raises(ValueError):
        ao.select_games(R("1940s"))
