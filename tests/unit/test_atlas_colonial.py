"""A-COLONIAL connectors (COLDAT, ICOW Colonial History + Territorial Claims, COW Territorial Change + Entities list)
on recorded fixtures -- offline.

Fixtures (``tests/fixtures/source/atlas.{coldat,icow,cowtc}``): the real downloads of 2026-10-16. COLDAT (CC0) and the
COW Territorial Change zip are the unmodified files; the two ICOW zips are re-zipped slices (codebooks unmodified,
CSV = header + selected real lines, byte-identical) because ICOW asks users not to redistribute its data (see each
manifest's ``note``). The countrycode tarball is stored once (atlas.icow) and referenced by the other manifests.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_colonial as C
from reva.adapters.connectors import atlas_terrchange as T
from reva.adapters.connectors.atlas_geopol_common import (
    StateEntityResolver,
    crosswalk_from_countrycode_tarball,
    crosswalk_keys,
    load_qid_for_iso3,
)
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

CC_URL = "https://cran.r-project.org/src/contrib/countrycode_1.9.0.tar.gz"
REPO = Path(__file__).resolve().parents[2]


def _get(name: str, url: str) -> bytes:
    return asyncio.run(FixtureHttp(name).get(url)).body


@pytest.fixture(scope="module")
def cw() -> Any:
    return crosswalk_from_countrycode_tarball(_get("atlas.icow", CC_URL), crosswalk_keys())


@pytest.fixture(scope="module")
def qids() -> dict[str, str]:
    return load_qid_for_iso3()


# ----------------------------------------------------------------------------------------------- specs / registry


def test_sources_are_discovered_with_registered_source_keys() -> None:
    from reva.atlas.sources import discover

    found, errors = discover(["reva.adapters.connectors.atlas_colonial", "reva.adapters.connectors.atlas_terrchange"])
    assert not errors, errors
    assert set(found) == {"atlas.coldat", "atlas.icow", "atlas.cowtc"}
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for e in found.values():
        assert e.spec.source_key in keys
        assert "cran.r-project.org" in e.spec.allowed_hosts  # the countrycode tarball is fetched by every run
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/a_colonial.yaml").read_text())
    assert {r["source_key"] for r in req["requests"]} == {e.spec.source_key for e in found.values()}


def test_relations_registry_declares_no_claim_and_pins_the_fixture_codebooks() -> None:
    doc = C.relations()
    assert doc["claims"] == [] and doc["owner"] == "A-COLONIAL"
    for r in doc["rules"]:
        assert r["relation"] is None and r["family"] in ("attribute", "context"), r["id"]
        assert r["codebook"].strip() and r["rationale"].strip(), r["id"]
    # the codebooks inside the fixture archives are the pinned ones
    col = C.zip_members(_get("atlas.icow", C.ICOW_COLHIST_URL), name="colhist.zip")
    assert C.check_document("icow_colcode", C.member(col, "/colcode.pdf", name="x")[1])["sha256"]
    tc = C.zip_members(_get("atlas.cowtc", T.TC_URL), name="tc.zip")
    C.check_document("cowtc_manual", C.member(tc, "tcmanual.pdf", name="x")[1])
    C.check_document("cowtc_entities", C.member(tc, "Entities.pdf", name="x")[1])
    with pytest.raises(SourceResponseError, match="codebook icow_colcode changed"):
        C.check_document("icow_colcode", b"%PDF-1.4 another codebook")


def test_no_dataset_predicate_is_causal() -> None:
    from reva.kg.model import CAUSAL_PIDS

    pids = set(C.COLONIAL_PROPERTIES) | set(T.TC_PROPERTIES)
    assert not pids & CAUSAL_PIDS
    assert all(
        not v[1].startswith(("cause", "has_cause", "effect"))
        for v in {**C.COLONIAL_PROPERTIES, **T.TC_PROPERTIES}.values()
    )


# ----------------------------------------------------------------------------------------------- COLDAT


@pytest.fixture(scope="module")
def coldat() -> dict[str, Any]:
    meta = _get("atlas.coldat", C.DV_VERSION_URL.format(version=C.COLDAT_VERSION, doi=C.COLDAT_DOI))
    bodies = {f.file_id: _get("atlas.coldat", C.DV_FILE_URL.format(f.file_id)) for f in C.COLDAT_FILES}
    return {"meta": meta, "bodies": bodies, "check": C.check_dataverse_version(meta, bodies, title="COLDAT")}


def test_coldat_version_record_checks(coldat: dict[str, Any]) -> None:
    assert coldat["check"]["licence"] == "CC0 1.0" and coldat["check"]["version"] == "3.0"
    doc = json.loads(coldat["meta"])
    doc["data"]["license"]["name"] = "CC BY-NC 4.0"
    with pytest.raises(SourceResponseError) as e:
        C.check_dataverse_version(json.dumps(doc).encode(), coldat["bodies"], title="COLDAT")
    assert e.value.details["kind"] == "licence_change"
    short = dict(coldat["bodies"]) | {7416947: coldat["bodies"][7416947][:-10]}
    with pytest.raises(SourceResponseError) as e:
        C.check_dataverse_version(coldat["meta"], short, title="COLDAT")
    assert e.value.details["kind"] == "checksum"


def test_coldat_parse_cross_check_and_atlas(coldat: dict[str, Any]) -> None:
    dyads = C.parse_coldat_dyads(coldat["bodies"][7416947])
    wide = C.parse_coldat_wide(coldat["bodies"][7416946])
    assert len(dyads) == 195 * 8 and sum(d.col for d in dyads) == 161
    assert C.coldat_cross_check(dyads, wide) == []
    br = next(d for d in dyads if d.country == "Brazil" and d.colonizer == "portugal")
    assert br.col == 1 and br.years == {
        "colstart_max": 1533,
        "colend_max": 1822,
        "colstart_mean": 1533,
        "colend_mean": 1822,
    }
    rows = C.coldat_atlas(dyads, raw_sha256="a" * 64)
    frame, unmapped = validate_atlas_frame(
        rows.frame(), source="coldat", dataset="dyads_v3", raw_sha256s=["a" * 64], geo_scheme="coldat_name"
    )
    assert unmapped == []  # every COLDAT name maps (exact crosswalk / CLDR evidence)
    assert frame["iso3"].n_unique() == 195
    spell = frame.filter(
        (pl.col("iso3") == "BRA") & (pl.col("indicator") == "colonial_rule|colonizer=portugal|agg=max")
    )
    assert spell.height == 1 and spell["value_text"][0] == "Portugal"
    assert (spell["period_start"][0], spell["period_end"][0]) == (date(1533, 1, 1), date(1823, 1, 1))
    zero = frame.filter((pl.col("iso3") == "AFG") & (pl.col("indicator") == "colonized|colonizer=britain"))
    assert zero["value"].to_list() == ["0"]  # a coded dummy, reported as such
    assert (
        country_iso3("Congo - Kinshasa", "coldat_name") == "COD"
        and country_iso3("Myanmar (Burma)", "coldat_name") == "MMR"
    )


def test_coldat_wide_long_difference_is_reported_not_resolved(coldat: dict[str, Any]) -> None:
    dyads = C.parse_coldat_dyads(coldat["bodies"][7416947])
    wide = C.parse_coldat_wide(coldat["bodies"][7416946])
    line, row = wide["Brazil"]
    wide["Brazil"] = (line, {**row, "colend.portugal_max": "1825"})
    dis = C.coldat_cross_check(dyads, wide)
    assert dis == [
        {"country": "Brazil", "colonizer": "portugal", "variable": "colend_max", "long": "1822", "wide": "1825"}
    ]
    rows = list(C.coldat_atlas(dyads, raw_sha256="a" * 64, disagreements=dis))
    flagged = [r for r in rows if json.loads(r["flags"]).get("wide_file_differs")]
    assert flagged and {r["provider_geo"] for r in flagged} == {"Brazil"}
    assert any(r["value"] == "1822" for r in flagged)  # the long file's value, as reported, not "corrected"


def test_coldat_kg_colonizers_resolve_through_cow_codes(coldat: dict[str, Any], cw: Any, qids: dict[str, str]) -> None:
    dyads = C.parse_coldat_dyads(coldat["bodies"][7416947])
    kg = C.coldat_kg(dyads, raw_sha256="a" * 64, resolver=StateEntityResolver(cw, qids), qid_for_iso3=qids)
    assert len(kg.statements) == 2 * 161
    by = {(s.subject, s.qualifiers["colonizer_as_coded"], s.qualifiers["aggregation"]): s for s in kg.statements}
    br = by[(qids["BRA"], "portugal", "last date aggregation")]
    assert (
        br.object_qid == qids["PRT"] and (br.start, br.end) == ("1533-01-01", "1822-01-01") and br.start_precision == 9
    )
    nam = by[(qids["NAM"], "germany", "last date aggregation")]
    assert nam.object_qid == "Q43287"  # COW 255 in 1884 = German Empire (historical_states_qids.json), not DEU


# ----------------------------------------------------------------------------------------------- ICOW


@pytest.fixture(scope="module")
def icow() -> dict[str, Any]:
    col = C.zip_members(_get("atlas.icow", C.ICOW_COLHIST_URL), name="colhist.zip")
    claims = C.zip_members(_get("atlas.icow", C.ICOW_CLAIMS_URL), name="ICOWprov.zip")
    return {
        "states": C.parse_icow_colhist(C.member(col, "/coldata110.csv", name="x")[1]),
        "claims": C.parse_icow_claims(C.member(claims, "/ICOWprov101.csv", name="x")[1]),
    }


def test_icow_challenge_page_is_refused() -> None:
    html = b"<!DOCTYPE html><html><title>One moment, please...</title>Please wait while your request is being verified"
    with pytest.raises(SourceResponseError, match="not a zip archive"):
        C.zip_members(html, name="colhist.zip")


def test_icow_colonial_history_as_coded(icow: dict[str, Any], cw: Any) -> None:
    st = {s.code: s for s in icow["states"]}
    br = st[140]
    assert (br.col_ruler, br.ind_from, br.ind_date.text, br.ind_viol, br.ind_type) == (235, 235, "1822-09", 0, 2)
    assert st[540].ind_viol == 1 and st[540].ind_date.text == "1975-11"  # Angola
    assert st[235].ind_date.text == "1139-07" and st[2].ind_date.text == "1783-09"
    rows = C.icow_colhist_atlas(icow["states"], raw_sha256="a" * 64, crosswalk=cw)
    frame, _ = validate_atlas_frame(rows.frame(), source="icow", dataset="colhist_v11", raw_sha256s=["a" * 64])
    get = {(r["provider_geo"], r["indicator"]): r for r in frame.to_dicts()}
    assert (
        get[("cow:140", "independence_year")]["iso3"] == "BRA"
        and get[("cow:140", "independence_year")]["value"] == "1822"
    )
    assert get[("cow:140", "independence_from")]["value_text"] == "cow:235 Portugal"
    assert get[("cow:140", "independence_violent")]["value"] == "0"
    assert get[("cow:140", "independence_type")]["value_text"] == "Decolonization"
    # month-precision dates resolve at the end of the coded month: Taiwan 1949-12 (COW 713 -> TWN from 1949-12-08)
    assert get[("cow:713", "independence_year")]["iso3"] == "TWN"
    assert get[("cow:347", "independence_year")]["iso3"] == "XKX"  # Kosovo 2008-02 (window from 2008-02-17)
    # West Germany is a historical state: iso3 null, verified QID kept
    wg = get[("cow:260", "independence_year")]
    assert wg["iso3"] is None and wg["entity_qid"] == "Q713750"
    flags = json.loads(get[("cow:140", "independence_violent")]["flags"])
    assert "not_a_causal_claim" in flags


def test_icow_kg_events_and_independence_key(icow: dict[str, Any], cw: Any, qids: dict[str, str]) -> None:
    kg = C.icow_colhist_kg(icow["states"], raw_sha256="a" * 64, resolver=StateEntityResolver(cw, qids))
    st = {s.key.split(":", 1)[1]: s for s in kg.statements}
    assert st["140:independence:from"].object_qid == qids["PRT"]
    assert st["140:independence:of"].object_qid == qids["BRA"]
    assert st["140:independence:date"].value == {"type": "time", "time": "1822-09-01", "precision": 10}
    assert st["140:independence:key"].value == {"type": "string", "value": "independence|cow:140|from|cow:235"}
    assert st["140:primary_colonial_ruler"].subject == qids["BRA"]
    assert "icowcol:independence:140" in kg.entities
    assert len({s.key for s in kg.statements}) == len(kg.statements)


def test_icow_claims(icow: dict[str, Any], cw: Any, qids: dict[str, str]) -> None:
    claims = icow["claims"]
    amapa = next(c for c in claims if c.claimdy == "12801")
    assert (amapa.name, amapa.chal, amapa.tgt, amapa.begin.text, amapa.end.text, amapa.resolved) == (
        "Amapa",
        235,
        220,
        "1816-01",
        "1822-07",
        5,
    )
    frame, _ = validate_atlas_frame(
        C.icow_claims_atlas(claims, raw_sha256="a" * 64, crosswalk=cw).frame(),
        source="icow",
        dataset="claims_prov101",
        raw_sha256s=["a" * 64],
    )
    assert frame.height == 2 * len(claims)
    kg = C.icow_claims_kg(claims, raw_sha256="a" * 64, resolver=StateEntityResolver(cw, qids), names={})
    ongoing = [c for c in claims if c.end is None]
    ends = {s.subject for s in kg.statements if s.property == "P582"}
    assert ongoing and not {f"icowclaim:{c.claimdy}" for c in ongoing} & ends
    res = next(s for s in kg.statements if s.key == "icowclaim:12801:resolution")
    assert res.value == {"type": "string", "value": "Independence"}


# ----------------------------------------------------------------------------------------------- COW Territorial Change


@pytest.fixture(scope="module")
def tc() -> dict[str, Any]:
    mem = C.zip_members(_get("atlas.cowtc", T.TC_URL), name="terr-changes-v6.zip")
    spells, report = T.parse_entities(C.member(mem, "Entities.pdf", name="x")[1])
    return {"rows": T.parse_tc(C.member(mem, "tc2018.csv", name="x")[1]), "spells": spells, "report": report}


def test_tc_rows(tc: dict[str, Any]) -> None:
    rows = {r.number: r for r in tc["rows"]}
    assert len(rows) == 842
    br = rows[46]  # Brazil's independence from Portugal as COW TC codes it
    assert (br.when.text, br.gainer, br.loser, br.entity, br.indep, br.procedure) == ("1824-12", 140, 235, 140, 1, -9)
    assert rows[717].procedure == 1 and rows[717].conflict == 1 and rows[717].loser == 235  # Goa 1961 (conquest)
    assert sum(r.indep for r in rows.values()) == 191


def test_entities_pdf_reader(tc: dict[str, Any]) -> None:
    spells, rep = tc["spells"], tc["report"]
    assert len(spells) == 2706 and rep["codes"] == 1154 and not rep["dangling"] and not rep["unparsed_status"]
    by = {(s.code, s.begin): s for s in spells}
    ang = by[(540, 1816)]
    assert (ang.name, ang.end, ang.relation, ang.counterpart) == ("Angola", 1975, "colony_of", 235)
    assert by[(615, 1831)].counterpart == 220 and by[(750, 1816)].relation == "colony_of"
    # a name printed on two lines is joined; a clipped word is glued
    assert by[(359, 1918)].name == "Moldova (Bessarabia) (Moldavian Republic)"
    assert by[(231, 1816)].name == "Gibraltar"
    # a status printed on two lines
    assert any(s.code == 2111 and s.relation == "neutral_zone_of" and s.counterpart == 210 for s in spells)
    # unreadable printed years are kept as printed, never guessed
    q = [s for s in spells if s.code == 1101 and s.raw_begin == "-198"]
    assert q and all(s.begin is None and s.end == 1981 for s in q)
    assert Counter(s.relation for s in spells)["colony_of"] == 315


def test_tc_atlas_tables_validate(tc: dict[str, Any], cw: Any) -> None:
    cy, _ = T.dedupe_iso3(T.changes_cy_atlas(tc["rows"], raw_sha256="a" * 64, crosswalk=cw))
    frame, _ = validate_atlas_frame(cy.frame(), source="cowtc", dataset="changes_cy", raw_sha256s=["a" * 64])
    prt = frame.filter((pl.col("iso3") == "PRT") & (pl.col("indicator") == "lost|independence=1"))
    assert {r["period_start"].year: r["value"] for r in prt.to_dicts()} == {1824: "1", 1974: "1", 1975: "4"}
    names = T.entity_names(tc["spells"])
    es, _ = T.entity_status_atlas(
        tc["spells"], raw_sha256="a" * 64, crosswalk=cw, names=names, alias_iso3=T.cowent_aliases()
    )
    es, _ = T.dedupe_iso3(es)
    frame, _ = validate_atlas_frame(es.frame(), source="cowtc", dataset="entity_status", raw_sha256s=["a" * 64])
    ang = frame.filter(pl.col("provider_geo") == "cowent:540")
    assert ang["iso3"].to_list() == ["AGO"] and ang["value_text"].to_list() == ["cow:235 Portugal"]
    assert ang["indicator"].to_list() == ["status|relation=colony_of|counterpart=235"]
    mac = frame.filter(pl.col("provider_geo") == "cowent:721")
    assert mac["iso3"].to_list() == ["MAC"]  # exact-name alias with evidence
    assert frame.filter(pl.col("provider_geo") == "cowent:4776")["iso3"].to_list() == [
        None
    ]  # Benin in Nigeria: not BEN


def test_tc_kg_and_shared_independence_key(
    tc: dict[str, Any], icow: dict[str, Any], cw: Any, qids: dict[str, str]
) -> None:
    names = T.entity_names(tc["spells"])
    res = StateEntityResolver(cw, qids)
    k1 = T.tc_kg(tc["rows"], raw_sha256="a" * 64, resolver=res, names=names)
    k2 = T.entities_kg(tc["spells"], raw_sha256="a" * 64, resolver=res, names=names, alias_iso3=T.cowent_aliases())
    st = {s.key: s for s in [*k1.statements, *k2.statements]}
    assert len(st) == len(k1.statements) + len(k2.statements)
    assert st["cowtc:46:gainer"].object_qid == qids["BRA"] and st["cowtc:46:loser"].object_qid == qids["PRT"]
    assert st["cowtc:46:date"].value == {"type": "time", "time": "1824-12-01", "precision": 10}
    ang = [s for s in k2.statements if s.subject == "cowent:540" and s.property == "cowent:colony_of"]
    assert (
        len(ang) == 1
        and ang[0].object_qid == qids["PRT"]
        and (ang[0].start, ang[0].end) == ("1816-01-01", "1975-01-01")
    )
    corr = [s for s in k2.statements if s.subject == "cowent:540" and s.property == "geopol:corresponds_to_iso3"]
    assert corr and corr[0].value == {"type": "string", "value": "AGO"}
    ik = C.icow_colhist_kg(icow["states"], raw_sha256="a" * 64, resolver=res)
    keys = Counter(
        (s.value or {}).get("value")
        for s in [*ik.statements, *k1.statements]
        if s.property == "colonial:independence_key"
    )
    assert keys["independence|cow:140|from|cow:235"] == 2  # ICOW + COW TC: EVENTS' same_property rule can link them
    assert keys["independence|cow:540|from|cow:235"] == 2


def test_pdf_reader_refuses_a_bomb() -> None:
    import zlib

    big = zlib.compress(b"0" * (T.PDF_MAX_STREAM + 10))
    pdf = b"1 0 obj<</Length %d/Filter/FlateDecode>>stream\n" % len(big) + big + b"\nendstream endobj"
    with pytest.raises(SourceResponseError, match="decompression bound"):
        T.pdf_pieces(pdf)


def test_tc_parse_refuses_unknown_procedure(tc: dict[str, Any]) -> None:
    mem = C.zip_members(_get("atlas.cowtc", T.TC_URL), name="terr-changes-v6.zip")
    body = C.member(mem, "tc2018.csv", name="x")[1]
    bad = body.replace(b"\n1816,7,160,1,-9,", b"\n1816,7,160,1,9,", 1)
    assert bad != body
    with pytest.raises(SourceResponseError, match="outside the manual"):
        T.parse_tc(bad)


def test_fixture_slices_are_real_lines() -> None:
    """ICOW slices: every CSV line of the fixture zip is a line of the real file (sha256 recorded in the note)."""
    man = json.loads((REPO / "tests/fixtures/source/atlas.icow/manifest.json").read_text())
    notes = [e["note"] for e in man if e["url"].endswith(".zip")]
    assert all("lines byte-identical" in n and "real zip sha256" in n for n in notes)
    z = zipfile.ZipFile(io.BytesIO(_get("atlas.icow", C.ICOW_COLHIST_URL)))
    assert sorted(n.rsplit("/", 1)[-1] for n in z.namelist()) == ["colcode.pdf", "coldata110.csv"]
