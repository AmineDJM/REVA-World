"""V4MIND atlas sources (well-being, mental health, time use, cultural values, occupations, OpenAlex taxonomy):
offline parsing against captured fixtures.

Fixtures (tests/fixtures/source/atlas.{oecd.wellbeing,who_mental,un_sdg_timeuse,hofstede,ilostat_occupation,
openalex_taxonomy}) are real responses captured on 2026-10-17 with restricted keys (3 countries, 5 countries, 3
M49 areas, A-ILO's 6 reference areas); the GHO indicator list and the OpenAlex snapshot files are cut to documented
subsets (manifest ``note`` + ``derived_from_sha256``). atlas.openalex_mind has no fixture (OpenAlex API budget
exhausted on 2026-10-17): its row builder is checked on A-SCI's captured group_by page.
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

from reva.adapters.connectors import atlas_mind_common as mc
from reva.adapters.connectors import atlas_mind_hofstede as hof
from reva.adapters.connectors import atlas_mind_occupations as occ
from reva.adapters.connectors import atlas_mind_oecd as oecd
from reva.adapters.connectors import atlas_mind_openalex as oax
from reva.adapters.connectors import atlas_mind_timeuse as tu
from reva.adapters.connectors import atlas_mind_who as who
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import atlas_sources
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
WITH_FIXTURES = (
    "atlas.oecd.wellbeing", "atlas.who_mental", "atlas.un_sdg_timeuse", "atlas.hofstede", "atlas.ilostat_occupation",
    "atlas.openalex_taxonomy",
)  # fmt: skip
MODULES = (mc, hof, occ, oecd, oax, tu, who)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def _get(name: str, url: str) -> bytes:
    return (await FixtureHttp(name).get(url, max_bytes=100_000_000)).body


def _validate(frame: pl.DataFrame, geo_scheme: str | None = None) -> None:
    validate_atlas_frame(
        frame,
        source=frame["source"][0],
        dataset=frame["dataset"][0],
        raw_sha256s=frame["raw_sha256"].unique().to_list(),
        geo_scheme=geo_scheme,
    )


# ----------------------------------------------------------------------------- platform contract
def test_sources_are_discovered_with_registered_source_keys() -> None:
    found = atlas_sources()
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for n in (*WITH_FIXTURES, "atlas.openalex_mind"):
        assert n in found, n
        spec = found[n].spec
        assert spec.source_key in keys and spec.budget_key in keys, n
        assert spec.code_digest.startswith("sha256:")
    for n in WITH_FIXTURES:
        man = json.loads((REPO / "tests/fixtures/source" / n / "manifest.json").read_text())
        assert man and all(e.get("note") and e.get("licence") for e in man), n
        assert not any(EMAIL.search(e["url"]) or "mailto" in e["url"] for e in man), "no address in any recorded URL"


def test_modules_never_build_an_http_client() -> None:
    import inspect

    for m in MODULES:
        src = inspect.getsource(m)
        assert "httpx" not in src and "SafeHttpClient(" not in src and "mailto" not in src.replace("no mailto", ""), m


def test_mind_catalog_is_valid_and_points_at_v4mind_tables() -> None:
    cat = load_catalog()
    assert not [e for e in cat.errors if "mind" in str(e)]
    doc = yaml.safe_load((REPO / "registry/atlas/catalog/mind.yaml").read_text())
    tables = {"oecd.hsl_mind_current", "oecd.hsl_mind_social_capital", "who.gho_mental", "un_sdg.sdg_timeuse",
              "hofstede.dimensions_2015", "ilostat.emp_2emp_sex_ocu_nb_a"}  # fmt: skip
    for c in doc["concepts"]:
        assert c["id"].startswith("mind.") and c.get("theme") in ("behaviour", "labour"), c["id"]
        for s in c["source_order"]:
            assert ".".join(s.split(".")[:2]) in tables, s


# ----------------------------------------------------------------------------- OECD How's Life?
async def test_oecd_wellbeing_measures_and_concepts() -> None:
    for fl in oecd.FLOWS:
        rq = oecd.requests(fl, True)
        files = []
        for r in rq:
            b = await _get(oecd.CONNECTOR_NAME, r.url)
            files.append((r.label, b, _sha(b)))
        frame, _rep, _note = oecd.build(fl, files)
        _validate(frame, "iso3")
        assert set(frame["provider_geo"].unique().to_list()) <= {"FRA", "JPN", "MEX"}
        assert frame["value"].null_count() == 0  # rows without a value are not cells
        names = {json.loads(f)["measure_name"] for f in frame["flags"].to_list()}
        assert names <= set(oecd.MEASURE_NAMES.values())
        if fl.dataset == "hsl_mind_current":
            ls = frame.filter(pl.col("indicator") == "11_1|unit_measure=0_TO_10|age=_T|sex=_T|education_lev=_T")
            assert ls.height > 0 and set(ls["concept_id"].unique().to_list()) == {"mind.oecd.life_satisfaction"}
            assert set(ls["unit"].unique().to_list()) == {"0-10 scale"}
            lonely = frame.filter(pl.col("indicator").str.starts_with("7_4|unit_measure=PT_POP_Y_GE16|"))
            assert lonely.height > 0
        else:
            assert frame.filter(pl.col("concept_id") == "mind.oecd.trust_in_government").height > 0


async def test_oecd_refuses_a_measure_it_did_not_request() -> None:
    fl = oecd.FLOW_SOCIAL_CAPITAL
    rq = oecd.requests(fl, True)[0]
    body = (await _get(oecd.CONNECTOR_NAME, rq.url)).replace(b",14_1,Trust in others,", b",14_9,Something else,")
    with pytest.raises(SourceResponseError, match="not requested"):
        oecd.build(fl, [(rq.label, body, _sha(body))])


# ----------------------------------------------------------------------------- WHO mental health
async def test_who_mental_health_atlas_cells() -> None:
    bodies = {it.key: await _get(who.CONNECTOR_NAME, it.url) for it in who.items(who.FIXTURE_COUNTRIES)}
    frame, rep = who.frame(bodies, {k: _sha(v) for k, v in bodies.items()})
    _validate(frame, "iso3")
    assert "MH_12" not in rep["rows"]  # A-HEALTHEDU's who.gho has it
    psy = frame.filter((pl.col("indicator") == "MH_6") & (pl.col("provider_geo") == "FRA"))
    assert psy.height == 1 and psy["concept_id"][0] == "mind.who.psychiatrists"
    f = json.loads(psy["flags"][0])
    assert f["estimate_class"] == "country_reported" and f["indicator_name"].startswith("Psychiatrists")
    policy = frame.filter(pl.col("indicator") == "MH_1")
    assert policy.height > 0 and policy["value"].null_count() == policy.height  # yes/no answers stay text
    assert set(policy["value_text"].unique().to_list()) <= {"Yes", "No"}
    rsud = frame.filter(pl.col("indicator").str.starts_with("RSUD_1|sex="))
    assert set(rsud["indicator"].unique().to_list()) <= {"RSUD_1|sex=SEX_FMLE", "RSUD_1|sex=SEX_MLE"}


async def test_who_refuses_an_archived_indicator() -> None:
    bodies = {it.key: await _get(who.CONNECTOR_NAME, it.url) for it in who.items(who.FIXTURE_COUNTRIES)}
    doc = json.loads(bodies["gho.indicator_list"])
    for v in doc["value"]:
        if v["IndicatorCode"] == "MH_9":
            v["IndicatorName"] = "ARCHIVED " + v["IndicatorName"]
    bodies["gho.indicator_list"] = json.dumps(doc).encode()
    with pytest.raises(SourceResponseError, match="MH_9"):
        who.frame(bodies, {k: _sha(v) for k, v in bodies.items()})


# ----------------------------------------------------------------------------- time use
async def _tu_bodies() -> dict[str, bytes]:
    return {it.key: await _get(tu.CONNECTOR_NAME, it.url) for it in tu.items(tu.FIXTURE_AREAS)}


async def test_time_use_cells_and_survey_waves() -> None:
    bodies = await _tu_bodies()
    frame, rep, waves = tu.parse(bodies, {k: _sha(v) for k, v in bodies.items()})
    _validate(frame, "m49")
    assert rep["left_out"] == [] and set(rep["series"]) == set(tu.SERIES)
    mex = frame.filter(
        (pl.col("provider_geo") == "484")
        & (pl.col("indicator") == "SL_DOM_TSPD|Age=15+|Location=ALLAREA|Reporting Type=G|Sex=FEMALE")
    )
    assert mex.height >= 3  # several Mexican surveys (2002 ... 2024), one cell per survey year
    assert set(mex["concept_id"].unique().to_list()) == {"mind.time_use.unpaid_domestic_and_care_work.women"}
    assert all("provider_source" in json.loads(f) for f in mex["flags"].to_list())
    # one survey wave per (geography, year, source text); never merged across texts
    assert len(waves) == len({(w["geo"], w["year"], w["source"]) for w in waves})
    ents, sts, krep = tu.kg_records(waves)
    assert krep["geo_without_qid"] == []
    kinds = {e.kind for e in ents}
    assert kinds == {"survey_wave", "atlas_table"}
    loc = [s for s in sts if s.property_pid == "unsd_tus:survey_in"]
    assert {s.object_qid for s in loc} == {"Q222", "Q142", "Q96"}  # Albania, France, Mexico (country_qids.json)
    assert all(s.raw_sha256 in {_sha(b) for b in bodies.values()} for s in sts)


async def test_time_use_leaves_out_a_series_whose_count_does_not_match() -> None:
    bodies = await _tu_bodies()
    doc = json.loads(bodies["sdg:SL_DOM_TSPDCW"])
    doc["totalElements"] += 1
    bodies["sdg:SL_DOM_TSPDCW"] = json.dumps(doc).encode()
    frame, rep, _ = tu.parse(bodies, {k: _sha(v) for k, v in bodies.items()})
    assert [x["series"] for x in rep["left_out"]] == ["SL_DOM_TSPDCW"]
    assert not frame.filter(pl.col("indicator").str.starts_with("SL_DOM_TSPDCW|")).height


# ----------------------------------------------------------------------------- Hofstede
async def test_hofstede_codes_mapped_by_name_and_nulls_typed() -> None:
    page = await _get(hof.CONNECTOR_NAME, hof.PAGE_URL)
    body = await _get(hof.CONNECTOR_NAME, hof.CSV_URL)
    assert hof.check_page(page)["licence_text"].startswith("Researchers can use them")
    frame, rep = hof.parse(body, raw_sha256=_sha(body))
    _validate(frame, hof.GEO_SCHEME)
    by = {r["provider_geo"]: r["iso3"] for r in frame.select("provider_geo", "iso3").unique().to_dicts()}
    assert by["CHI"] == "CHN" and by["TAI"] == "TWN" and by["GBR"] == "GBR"  # CHI is China here, not Channel Islands
    assert by["BEN"] is None and by["CAF"] is None and by["ARA"] is None  # Belgium Netherl, Canada French, region
    alb = frame.filter((pl.col("provider_geo") == "ALB") & (pl.col("indicator") == "pdi"))
    f = json.loads(alb["flags"][0])
    assert alb["value"][0] is None and f["missing"] == "provider_missing_code" and f["provider_flag"] == "#NULL!"
    assert all(json.loads(x)["licence_restriction"] == "research_only_no_redistribution_grant" for x in frame["flags"])
    assert rep["units"] == 111 and frame["period_start"].dt.year().unique().to_list() == [2010]


async def test_hofstede_refuses_a_changed_licence_sentence() -> None:
    page = (await _get(hof.CONNECTOR_NAME, hof.PAGE_URL)).replace(b"without asking for permission", b"with permission")
    with pytest.raises(SourceResponseError, match="licence"):
        hof.check_page(page)


# ----------------------------------------------------------------------------- ISCO-08 groups
async def test_isco_groups_from_classif1_and_kg_records() -> None:
    from reva.adapters.connectors import atlas_ilo as ilo

    body = await _get(occ.CONNECTOR_NAME, ilo.dictionary_url("classif1"))
    groups = occ.isco_groups(ilo.parse_dictionary(body, "classif1"))
    majors = {g["isco"] for g in groups if g["level"] == "major"}
    assert majors == set("0123456789")
    sub = {g["isco"]: g for g in groups if g["level"] == "sub_major"}
    assert sub["26"]["parent"] == "2" and sub["26"]["title"] == "Legal, social and cultural professionals"
    codes = {g["classif1"] for g in groups}
    assert not codes & {"OCU_ISCO08_X", "OCU_ISCO08_TOTAL", "OCU_ISCO08_96", "OC2_ISCO08_X"}  # ILOSTAT categories
    assert "OC2_ISCO08_96" in codes  # 96 Refuse workers and other elementary workers: a real ISCO sub-major group
    _ents, sts = occ.kg_records(groups, {"emp_2emp_sex_ocu_nb_a": {"OCU_ISCO08_2"}}, dic_sha=_sha(body))
    part = {(s.subject_qid, s.object_qid) for s in sts if s.property_pid == "isco:part_of"}
    assert ("isco08:26", "isco08:2") in part and len(part) == len(sub)
    meas = [s for s in sts if s.property_pid == "ilostat:measured_by"]
    assert [(s.subject_qid, s.object_qid) for s in meas] == [("isco08:2", "atlas:ilostat.emp_2emp_sex_ocu_nb_a")]


def test_isco_label_without_its_code_is_refused() -> None:
    with pytest.raises(SourceResponseError, match="does not carry"):
        occ.isco_groups({"OCU_ISCO08_2": "Occupation (ISCO-08): 3. Professionals"})


# ----------------------------------------------------------------------------- OpenAlex
async def _taxonomy() -> Any:
    manifests, files = {}, {}
    for e in oax.TAX_ENTITIES:
        manifests[e] = await _get(oax.TAX_CONNECTOR, oax.manifest_url(e))
        for url, _n in oax.parse_manifest(manifests[e], e)["files"]:
            b = await _get(oax.TAX_CONNECTOR, oax.file_url(url))
            files.setdefault(e, []).append((url, b, _sha(b)))
    return manifests, files


async def test_openalex_taxonomy_hierarchy_by_ids() -> None:
    manifests, files = await _taxonomy()
    tax = oax.build_taxonomy(manifests, files)
    ents, sts, _stats = oax.taxonomy_kg(tax)
    qids = {e.qid: e for e in ents}
    assert qids["openalex:subfield.3207"].kind == "research_subfield"
    assert qids["openalex:T10475"].labels["en"] == "Psychological Well-being and Life Satisfaction"
    part = {(s.subject_qid, s.object_qid) for s in sts if s.property_pid == "openalex:part_of"}
    assert ("openalex:subfield.3207", "openalex:field.32") in part
    assert ("openalex:field.32", "openalex:domain.2") in part
    wd = {s.subject_qid: s.value["value"] for s in sts if s.property_pid == "openalex:wikidata_id"}
    assert all(v.startswith("Q") for v in wd.values()) and "openalex:subfield.3207" in wd
    counts = [s for s in sts if s.property_pid == "openalex:works_count"]
    assert counts and all(s.qualifiers["live_index"] for s in counts)


async def test_openalex_taxonomy_refuses_a_file_missing_from_the_manifest() -> None:
    manifests, files = await _taxonomy()
    files["topics"] = files["topics"][:-1]
    with pytest.raises(SourceResponseError, match="differ from the manifest"):
        oax.build_taxonomy(manifests, files)


def test_openalex_mind_rows_on_a_captured_group_by_page() -> None:
    from datetime import UTC, datetime

    from reva.adapters.connectors import atlas_openalex as oa

    man = json.loads((REPO / "tests/fixtures/source/atlas.openalex/manifest.json").read_text())
    e = next(x for x in man if "group_by=authorships.countries" in x["url"] and "cursor" not in x["url"])
    body = (REPO / "tests/fixtures/source/atlas.openalex" / e["file"]).read_bytes()
    page = oa.parse_group_page(body)
    q = oax.mind_queries((2020, 2020))[-1]
    assert q.filter == "publication_year:2020,type:article|review,primary_topic.subfield.id:3312"
    assert q.indicator == "works|type=article,review|subfield=3312"
    rows = oax.mind_rows(
        q, [oa.Fetched(page, _sha(body), 1, q.filter)], retrieved_at=datetime(2026, 10, 17, tzinfo=UTC)
    )
    assert rows[0]["provider_geo"] == "_all_works"
    assert all(json.loads(r["flags"])["subfield"] == "Sociology and Political Science" for r in rows)
    assert all(r["concept_id"] is None for r in rows)
