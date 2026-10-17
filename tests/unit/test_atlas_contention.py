"""A-CONTENTION connectors (NAVCO 1.2/1.3/2.1, MEC, Mass Mobilization) on trimmed recorded fixtures -- offline.

Fixtures (``tests/fixtures/source/atlas.{navco,mec,massmob}``) are slices of the real Harvard Dataverse downloads of
2026-10-16 (selected countries, provider lines byte-identical; MMP sources/notes cells emptied), with the pinned
version records trimmed to the data file and the quoted codebook (see each manifest's ``note``).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_massmob as M
from reva.adapters.connectors import atlas_navco as N
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp


def _get(name: str, url: str) -> bytes:
    return asyncio.run(FixtureHttp(name).get(url)).body


def _parsed(name: str, f: N.DvFile) -> N.Parsed:
    meta, body = _get(name, f.version_url), _get(name, f.file_url)
    info = N.check_version(meta, body, f)
    return N.Parsed(
        f,
        SimpleNamespace(body=meta),
        SimpleNamespace(body=body),
        N.sha256(body),
        N.read_tab(body, f, strict=False),
        info,
    )


QIDS = {"AFG": "Q889", "RUS": "Q159", "MMR": "Q836", "SYR": "Q858", "GEO": "Q230", "MDG": "Q1019", "CAN": "Q16"}


@pytest.fixture(scope="module")
def navco() -> dict[str, Any]:
    parsed = {k: _parsed("atlas.navco", N.FILES[k]) for k in ("n12", "n13", "n21")}
    return {"parsed": parsed, "out": N.build_navco(parsed, qid_for_iso3=QIDS)}


@pytest.fixture(scope="module")
def mec() -> dict[str, Any]:
    p = _parsed("atlas.mec", N.FILES["mec"])
    return {"parsed": p, "out": N.build_mec(p, qid_for_iso3=QIDS)}


@pytest.fixture(scope="module")
def mmp() -> dict[str, Any]:
    p = _parsed("atlas.massmob", M.FILE)
    return {"parsed": p, "out": M.build(p, qid_for_iso3=QIDS)}


def _frame(rows: Any, source: str, dataset: str, shas: list[str]) -> pl.DataFrame:
    frame, _ = validate_atlas_frame(N.atlas_frame(rows), source=source, dataset=dataset, raw_sha256s=shas)
    return frame


# ----------------------------------------------------------------------------- provider checks


def test_version_record_checks_licence_size_and_quoted_codebook() -> None:
    f = N.FILES["n13"]
    meta, body = _get("atlas.navco", f.version_url), _get("atlas.navco", f.file_url)
    assert N.check_version(meta, body, f)["licence"] == "CC0 1.0"
    doc = json.loads(meta)
    bad = json.loads(meta)
    bad["data"]["license"]["name"] = "CC BY-NC 4.0"
    with pytest.raises(N.ContentionParseError, match="licence changed"):
        N.check_version(json.dumps(bad).encode(), body, f)
    with pytest.raises(N.ContentionParseError, match="bytes, version record says"):
        N.check_version(meta, body + b"\n", f)
    for x in doc["data"]["files"]:
        if x["dataFile"]["id"] == f.codebook_id:
            x["dataFile"]["md5"] = "0" * 32
    with pytest.raises(N.ContentionParseError, match="quoted codebook"):
        N.check_version(json.dumps(doc).encode(), body, f)


def test_tab_reader_skips_blank_rows_and_refuses_missing_columns(navco: dict[str, Any]) -> None:
    t12 = navco["parsed"]["n12"].table
    assert t12.blank_rows == 3 and all(r["CAMPAIGN"] for r in t12.rows)
    assert t12.lines[0] == 2  # provider line numbers (header = line 1)
    f = N.FILES["n13"]
    body = _get("atlas.navco", f.file_url).replace(b"NAVCOID", b"NAVCO_ID", 1)
    with pytest.raises(N.ContentionParseError, match="missing columns"):
        N.read_tab(body, f)
    with pytest.raises(N.ContentionParseError, match="truncated"):
        N.read_tab(_get("atlas.navco", f.file_url), f, strict=True)


def test_value_helpers() -> None:
    assert N.int_val("4.0") == 4 and N.int_val("-99") is None and N.int_val("") is None
    with pytest.raises(N.ContentionParseError):
        N.int_val("4.5", what="x")
    assert N.dec_text("1.4E7") == "14000000" and N.dec_text("6.41E-4") == "0.000641" and N.dec_text("-99") is None
    assert N.parse_ymd("1991-1-5").isoformat() == "1991-01-05" and N.parse_ymd("31/12/2017") is None


# ----------------------------------------------------------------------------- geography


def test_location_names_map_through_the_alias_schemes() -> None:
    assert country_iso3("Burma", "navco_loc") == country_iso3("Burma/Myanmar", "navco_loc") == "MMR"
    assert country_iso3("Myanmar/Burma", "mec_loc") == "MMR"
    assert country_iso3("Palestinian Territories", "navco_loc") == "PSE"
    for name, scheme in (("USSR", "navco_loc"), ("Yugoslavia", "navco_loc"), ("West Papua", "mec_loc"),
                         ("Israel and Palestine", "mec_loc"), ("Germany West", "mmp_loc"), ("USSR", "mmp_loc")):  # fmt: skip
        assert country_iso3(name, scheme) is None, name
    assert country_iso3("Congo Kinshasa", "mmp_loc") == "COD" and country_iso3("Congo Brazzaville", "mmp_loc") == "COG"


def test_places_use_qids_historical_qids_and_provider_names() -> None:
    p = N.Places("navco_loc", "navco", {"AFG": "Q889"})
    assert p.resolve("Afghanistan", 1980).node == "Q889"
    ussr = p.resolve("USSR", 1950)
    assert (ussr.node, ussr.how) == ("Q15180", "historical_qid")  # registry/entities/historical_states_qids.json
    assert p.resolve("Yugoslavia", 1995).node == "Q838261"  # Federal Republic of Yugoslavia 1992-2002
    wp = p.resolve("West Papua", 1970)
    assert wp.node == "navco:place:west_papua" and wp.iso3 is None
    burma = p.resolve("Burma", 1990)
    assert burma.node == "iso3:MMR" and burma.how == "iso3_node_qid_unresolved"
    assert {e.qid for e in p.entities()} == {"navco:place:west_papua", "iso3:MMR"}


# ----------------------------------------------------------------------------- NAVCO


def test_navco1_keys_are_unique_and_shared_ids_are_disambiguated(navco: dict[str, Any]) -> None:
    rows13, _stats13 = N.navco1_rows(navco["parsed"]["n13"].table, N.FILES["n13"])
    keys = [r.key for r in rows13]
    assert len(keys) == len(set(keys))
    # id 408 is two different campaigns in NAVCO 1.3 (Karens 1948, Burma pro-democracy 1988)
    burma = {r.key: r.name for r in rows13 if r.navco_id == 408}
    assert burma == {"408-1948": "Karens", "408-1988": "Pro-Democracy movement in Burma"}
    rows12, stats12 = N.navco1_rows(navco["parsed"]["n12"].table, N.FILES["n12"])
    syria = sorted((r.key, r.name) for r in rows12 if r.navco_id == 282)
    assert [k for k, _ in syria] == ["282-2011-1", "282-2011-2"] and stats12["rows_sharing_id_and_byear"] == 2
    assert stats12["blank_rows_skipped"] == 3


def test_n13_country_year_counts_are_exact(navco: dict[str, Any]) -> None:
    out, p = navco["out"], navco["parsed"]["n13"]
    frame = _frame(out["n13_cy"], "navco", "n13_cy", [p.sha])
    rows13, _ = N.navco1_rows(p.table, N.FILES["n13"])
    for iso3, year in (("AFG", 1980), ("MMR", 1990), ("RUS", 1917), ("SYR", 2012)):
        expected = [r for r in rows13 if country_iso3(r.location, "navco_loc") == iso3 and r.byear <= year <= r.eyear]
        cell = frame.filter(
            (pl.col("iso3") == iso3)
            & (pl.col("indicator") == "campaigns_active")
            & (pl.col("period_start").dt.year() == year)
        )
        assert cell.height == 1 and int(cell["value"][0]) == len(expected) > 0, (iso3, year)
        flags = json.loads(cell["flags"][0])
        assert sorted(flags["items"]) == sorted(r.key for r in expected)
        nv = frame.filter(
            (pl.col("iso3") == iso3)
            & (pl.col("indicator") == "campaigns_active|method=nonviolent")
            & (pl.col("period_start").dt.year() == year)
        )
        assert int(nv["value"][0]) == sum(r.method == "nonviolent" for r in expected)
    # Burma and Burma/Myanmar are one country-year cell (no double mapping), names listed
    mmr = frame.filter(
        (pl.col("iso3") == "MMR")
        & (pl.col("indicator") == "campaigns_active")
        & (pl.col("period_start").dt.year() == 1990)
    )
    assert json.loads(mmr["flags"][0])["provider_locations"] == ["Burma"]
    ussr = frame.filter(pl.col("provider_geo") == "USSR")
    assert ussr.height > 0 and ussr["iso3"].is_null().all()


def test_n21_counts_repression_levels(navco: dict[str, Any]) -> None:
    p = navco["parsed"]["n21"]
    frame = _frame(navco["out"]["n21_cy"], "navco", "n21_cy", [p.sha])
    camps = N.n21_campaigns(p.table, N.FILES["n21"])
    total = sum(1 for c in camps.values() for y in c.years if y.repression == "3")
    got = frame.filter(pl.col("indicator") == "campaign_years|repression=extreme")["value"].cast(pl.Int64).sum()
    assert got == total > 0
    by_cy = frame.filter(pl.col("indicator") == "campaign_years")["value"].cast(pl.Int64).sum()
    assert by_cy == sum(len(c.years) for c in camps.values())


def test_navco_kg_records(navco: dict[str, Any]) -> None:
    out = navco["out"]
    keys = [s.key for s in out["statements"]]
    assert len(keys) == len(set(keys))
    ents = {e.qid: e for e in out["entities"]}
    assert len(ents) == len(out["entities"])
    st = {(s.subject, s.property): s for s in out["statements"]}
    # NAVCO 1.2 rows link to NAVCO 2.1 by the shared id (codebook); split phases are phase_of
    assert any(s.property == "contention:same_campaign_as" for s in out["statements"])
    phases = [s for s in out["statements"] if s.property == "contention:phase_of"]
    assert {s.subject for s in phases} == {"navco12:282-2011-1", "navco12:282-2011-2"}
    assert {s.object_qid for s in phases} == {"navco21:282"}
    # USSR campaign located at the Soviet Union QID
    ussr = [s for s in out["statements"] if s.property == "contention:location" and s.object_qid == "Q15180"]
    assert ussr and all(ents[s.subject].kind in ("event",) for s in ussr)
    # repression events point at their campaign; coded outcomes are entities of their own
    rep = [e for e in ents if ":repression:" in e]
    assert rep and all(st[(e, "contention:directed_at")].object_qid == e.split(":repression:")[0] for e in rep)
    assert all(ents[e].kind == "campaign_outcome" for e in ents if e.endswith(":outcome"))
    # nothing causal in kg.statement
    assert not any("caus" in s.property or "reaction" in s.property for s in out["statements"])
    assert set(s.property for s in out["statements"]) <= set(N.CONTENTION_PROPERTIES) | set(N.WIKIDATA_PIDS)
    assert not set(N.WIKIDATA_PIDS) & set(N.CONTENTION_PROPERTIES)


def _all(claims: dict[str, list[Any]]) -> list[Any]:
    return [c for cs in claims.values() for c in cs]


def test_navco_claims(navco: dict[str, Any]) -> None:
    out, parsed = navco["out"], navco["parsed"]
    claims = out["claims"]
    assert set(claims) == {"navco12", "navco13", "navco21"}
    for ds, cs in claims.items():
        f = N.FILES[N.CLAIM_DATASETS[ds]]
        defs = N.codebook_definitions(f)
        assert len({c.source_claim_id for c in cs}) == len(cs)
        for c in cs:
            assert c.tier == "T1" and c.relation in ("coded_outcome", "reaction_to") and c.polarity is None
            assert c.codebook_term in defs and c.raw_sha256 == parsed[N.CLAIM_DATASETS[ds]].sha
            assert c.selector["file"] == f.file_name and c.selector["line"] > 1 and c.selector["coded"]
            assert c.cause_id != c.effect_id and c.effect_id.startswith(c.cause_id + ":")
            assert c.mechanism is None and c.stance == "asserts"
    rows13, _ = N.navco1_rows(parsed["n13"].table, N.FILES["n13"])
    # audit L2 (AUDFIX): a campaign coded ONGOING has no coded_outcome claim (its "failure" is censoring)
    assert any(r.ongoing for r in rows13)
    assert len(claims["navco13"]) == sum(r.outcome is not None and not r.ongoing for r in rows13)
    assert not any(c.selector["coded"].endswith("(ongoing)") or c.codebook_term == "ONGOING" for c in claims["navco13"])
    assert "ongoing as of 12/31/2019" in N.CODEBOOK["n13"]["ONGOING"]
    # audit L1: NAVCO 2.1 outcomes rest on "success" only when success = 1, else on "progress"
    n21 = [c for c in claims["navco21"] if c.relation == "coded_outcome"]
    assert n21 and all((c.codebook_term == "success") == c.selector["coded"].endswith("success = 1") for c in n21)
    assert any(c.codebook_term == "progress" for c in n21)
    camps = N.n21_campaigns(parsed["n21"].table, N.FILES["n21"])
    rows12, _ = N.navco1_rows(parsed["n12"].table, N.FILES["n12"])
    expected = sum(y.repression in ("1", "2", "3") for c in camps.values() for y in c.years)
    expected += sum(N.flag01(r.raw["REGVIOL"]) is True for r in rows12)
    assert sum(c.relation == "reaction_to" for c in _all(claims)) == expected > 0
    # every effect / cause is a KG entity of the same build (no dangling claim ends)
    ents = {e.qid for e in out["entities"]}
    assert all(c.cause_id in ents and c.effect_id in ents for c in _all(claims))


def test_navco12_regime_violence_has_no_borrowed_date(navco: dict[str, Any]) -> None:
    """Audit M3 (AUDFIX): NAVCO 1.2 codes no date for REGVIOL -> unknown value + typed missing, not the BYEAR."""
    out = navco["out"]
    rv = [s for s in out["statements"] if s.subject.endswith(":regime_violence") and s.property == "P580"]
    assert rv and all(s.value == {"type": "somevalue"} for s in rv)
    assert all(s.qualifiers["missing"]["reason"] == "not_collected" for s in rv)
    claims = [c for c in out["claims"]["navco12"] if c.relation == "reaction_to"]
    assert claims and all(c.scope["effect_time"]["kind"] == "missing" for c in claims)


def test_claim_deliveries_have_the_causcore_shape(navco: dict[str, Any]) -> None:
    from datetime import UTC, datetime

    p = navco["parsed"]["n13"]
    d = N.delivery(p.f, "navco13", navco["out"]["claims"]["navco13"], source_key=N.SOURCE_KEY_NAVCO,
                   raw_sha256=p.sha, retrieved_at=datetime(2026, 10, 16, tzinfo=UTC), origin="test_fixture")  # fmt: skip
    doc = d.as_json()
    assert doc["interface"] == "reva.causal.load_dataset_claims" and doc["complete"] is True
    assert doc["dataset_revision"] == "doi:10.7910/DVN/ON9XND@1.0#3769104"
    assert set(doc["codebook"]["definitions"]) == set(N.CODEBOOK["n13"])
    assert p.f.codebook_md5 in doc["codebook"]["version"] and doc["raw_sha256s"] == [p.sha]
    assert "sources" not in doc["codebook"]  # no codebook fetched: nothing is said about archived bytes
    fetched = {"sha256": p.f.codebook_sha256, "matches": True, "archived": True}
    d2 = N.delivery(p.f, "navco13", [], source_key=N.SOURCE_KEY_NAVCO, raw_sha256=p.sha,
                    retrieved_at=datetime(2026, 10, 16, tzinfo=UTC), origin="test_fixture", codebook_fetch=fetched)  # fmt: skip
    doc2 = d2.as_json()
    assert doc2["raw_sha256s"] == sorted([p.sha, p.f.codebook_sha256])
    src = doc2["codebook"]["sources"]["ONGOING"]
    assert (src["sha256"], src["pages"]) == (p.f.codebook_sha256, [12])
    assert N.codebook_ok(fetched, "live") and not N.codebook_ok({**fetched, "matches": False}, "test_fixture")
    assert N.codebook_ok({"status": "fixture_missing", "matches": False}, "test_fixture")
    assert not N.codebook_ok({"status": "fixture_missing", "matches": False}, "live")
    assert {c["codebook_term"] for c in doc["claims"]} <= set(doc["codebook"]["definitions"])
    with pytest.raises(ValueError, match="no codebook definition"):
        N.claim(key=f"{N.SOURCE_KEY_NAVCO}:n13:x", cause="a", effect="b", relation="coded_outcome", f=p.f,
                source_key=N.SOURCE_KEY_NAVCO, raw_sha256=p.sha, selector={}, coded_value="1", codebook_term="NOPE")  # fmt: skip


def test_event_dates_are_the_grader_time_properties(navco: dict[str, Any], mec: dict[str, Any]) -> None:
    props = Counter(s.property for s in navco["out"]["statements"] + mec["out"]["statements"])
    assert props["P580"] > 0 and props["P585"] > 0 and props["P582"] > 0
    assert not any(p.startswith("contention:") and p.endswith("_date") for p in props)
    ev = [s for s in mec["out"]["statements"] if s.property == "P580" and s.subject.count(":") == 1]
    assert ev and all(s.value["precision"] == 11 for s in ev)
    n13 = [s for s in navco["out"]["statements"] if s.property == "P580" and s.subject.startswith("navco13:")]
    assert n13 and all(s.value["precision"] == 9 for s in n13)


# ----------------------------------------------------------------------------- MEC


def test_mec(mec: dict[str, Any]) -> None:
    out, p = mec["out"], mec["parsed"]
    frame = _frame(out["episodes_cy"], "mec", "episodes_cy", [p.sha])
    rows, stats = N.mec_rows(p.table, N.FILES["mec"])
    unknown = [r for r in rows if r.outcome is None]
    assert stats["outcome_unknown"] == len(unknown)
    ongoing = [r for r in rows if r.outcome is not None and r.ongoing]
    assert sum(c.relation == "coded_outcome" for c in out["claims"]["mec"]) == len(rows) - len(unknown) - len(ongoing)
    assert sum(c.relation == "reaction_to" for c in out["claims"]["mec"]) == sum(bool(r.regviol) for r in rows)
    # audit M3: the regime violence has no date of its own -> "unknown value" + typed missing, never the episode's
    rv = [s for s in out["statements"] if s.subject.endswith(":regime_violence") and s.property in ("P580", "P582")]
    assert rv and all(s.value == {"type": "somevalue"} for s in rv)
    assert all(s.qualifiers["missing"] == {"kind": "missing", "reason": "not_collected"} for s in rv)
    react = [c for c in out["claims"]["mec"] if c.relation == "reaction_to"]
    assert all(c.scope["effect_time"]["kind"] == "missing" for c in react)
    # Tibet episodes are placed in China by the provider (ccode 710): counted in CHN with the name listed
    chn = frame.filter((pl.col("iso3") == "CHN") & (pl.col("indicator") == "episodes_active"))
    tibet_years = {y for r in rows if r.location == "Tibet" for y in range(r.begin.year, r.end.year + 1)}
    assert tibet_years and any("Tibet" in json.loads(f)["provider_locations"] for f in chn["flags"])
    assert frame.filter(pl.col("provider_geo") == "Israel and Palestine")["iso3"].is_null().all()


# ----------------------------------------------------------------------------- Mass Mobilization


def test_mmp_counts_zero_years_and_no_news_text(mmp: dict[str, Any]) -> None:
    out, p = mmp["out"], mmp["parsed"]
    frame = _frame(out["protests_cy"], "mmp", "protests_cy", [p.sha])
    protests, zeros, _ = M.mmp_rows(p)
    can = frame.filter((pl.col("iso3") == "CAN") & (pl.col("indicator") == "protests")).sort("period_start")
    expected = Counter(x.year for x in protests if x.country == "Canada")
    zero_years = {z.year for z in zeros if z.country == "Canada"}
    assert zero_years and expected
    for row in can.iter_rows(named=True):
        y = row["period_start"].year
        assert int(row["value"]) == expected.get(y, 0)
        if y in zero_years:
            assert "provider-reported zero" in json.loads(row["flags"])["zero_semantics"]
            assert json.loads(row["selector"])["line"] > 1
    assert set(can["period_start"].dt.year()) == set(expected) | zero_years
    # sources / notes are never stored
    blob = json.dumps([s.as_dict() for s in out["statements"]], default=str)
    assert (
        "notes" not in {k for s in out["statements"] for k in s.selector.get("column", [])}
        and "new york times" not in blob.lower()
    )
    assert frame.filter(pl.col("provider_geo") == "USSR")["iso3"].is_null().all()


def test_mmp_state_responses_and_claims(mmp: dict[str, Any]) -> None:
    out, p = mmp["out"], mmp["parsed"]
    protests, _, _ = M.mmp_rows(p)
    acting = [x for x in protests if any(r != "ignore" for r in x.responses)]
    claims = out["claims"]["mmp"]
    assert len(claims) == len(acting) > 0
    assert all("ignore" not in c.selector["coded"].split(", ") for c in claims)
    ents = {e.qid for e in out["entities"]}
    assert all(f"mmp:{x.pid}:state_response" in ents for x in acting)
    only_ignore = [x for x in protests if x.responses == ["ignore"]]
    assert only_ignore and all(f"mmp:{x.pid}:state_response" not in ents for x in only_ignore)
    c = claims[0]
    assert (
        c.relation == "reaction_to"
        and c.codebook_term == "state_response"
        and c.effect_id == c.cause_id + ":state_response"
    )
    resp = [s for s in out["statements"] if s.subject == c.effect_id and s.property in ("P580", "P582")]
    prot = [s for s in out["statements"] if s.subject == c.cause_id and s.property in ("P580", "P582")]
    # audit M3 (AUDFIX): MMP codes no date for the response -> unknown value + typed missing, never the protest's
    assert len(resp) == 2 and all(s.value == {"type": "somevalue"} for s in resp)
    assert all(s.qualifiers["missing"]["reason"] == "not_collected" for s in resp)
    assert all(s.value["type"] == "time" for s in prot)
    assert c.scope["effect_time"] == {
        "kind": "missing",
        "reason": "not_collected",
        "note": "MMP codes no separate date for the state response",
    }
    # audit L3: the coded response list is kept verbatim, inaction marked (not a reaction)
    mixed = [x for x in acting if "ignore" in x.responses]
    by_cause = {cl.cause_id: cl for cl in claims}
    for x in mixed:
        sel = by_cause[f"mmp:{x.pid}"].selector
        assert sel["coded_responses"] == x.responses and sel["inaction_not_a_reaction"] == ["ignore"]


def test_mmp_refuses_unknown_categories(mmp: dict[str, Any]) -> None:
    p = mmp["parsed"]
    rows = [dict(r) for r in p.table.rows]
    i = next(k for k, r in enumerate(rows) if r["protest"] == "1")
    rows[i]["stateresponse1"] = "tea and biscuits"
    bad = N.Parsed(p.f, p.meta_raw, p.data_raw, p.sha, N.Table(rows, p.table.lines), p.meta)
    with pytest.raises(N.ContentionParseError, match="unknown state response"):
        M.mmp_rows(bad)


# ----------------------------------------------------------------------------- platform


def test_specs_are_discovered() -> None:
    from reva.atlas.sources import atlas_source_errors, atlas_sources

    names = set(atlas_sources())
    assert {"atlas.navco", "atlas.mec", "atlas.massmob"} <= names
    assert not [e for e in atlas_source_errors() if "navco" in str(e) or "massmob" in str(e)]


def test_claims_are_staged_without_the_causal_loader_and_refused_in_production(tmp_path: Any, monkeypatch: Any) -> None:
    from reva.adapters.connectors import causdata_common as cd

    monkeypatch.setattr(cd, "causal_available", lambda engine: False)
    d = SimpleNamespace(dataset="navco13", claims=[1, 2], source_key=N.SOURCE_KEY_NAVCO, origin="test_fixture",
                        as_json=lambda: {"claims": [1, 2]})  # fmt: skip
    run = SimpleNamespace(options={}, settings=SimpleNamespace(reva_env="test"), engine=None, store=None)
    assert N.deliver(run, [d]) == {"navco13": {"status": "causal_unavailable", "claims": 2}}
    run.options = {"claims_staging": str(tmp_path)}
    out = N.deliver(run, [d])["navco13"]
    assert out["status"] == "staged" and json.loads(Path(out["file"]).read_text()) == {"claims": [1, 2]}
    run.settings.reva_env = "production"
    with pytest.raises(N.ContentionParseError):
        N.deliver(run, [d])


# ----------------------------------------------------------------------------- NAVCO 3.0 (counts only)


def test_n30_action_line_counts() -> None:
    f = N.FILES["n30"]
    meta, body = _get("atlas.navco", f.version_url), _get("atlas.navco", f.file_url)
    N.check_version(meta, body, f)  # non-tabular upload: md5 checked
    with pytest.raises(N.ContentionParseError, match="md5"):
        doc = json.loads(meta)
        for x in doc["data"]["files"]:
            if x["dataFile"]["id"] == f.file_id:
                x["dataFile"]["md5"] = "0" * 32
        N.check_version(json.dumps(doc).encode(), body, f)
    lines, stats = N.n30_lines(body, f, strict=False)
    assert stats["countries"] == 3 and {x.country for x in lines} == {"estonia", "egypt", "us"}
    assert all(x.row > 1 for x in lines)  # provider workbook row numbers
    egypt = [x for x in lines if x.country == "egypt"]
    assert egypt and all(x.day.year == 2011 for x in egypt)  # Excel serial dates, 1900 system
    rows = N.n30_atlas(lines, N.Places("navco3_loc", "navco", {}), raw_sha256=N.sha256(body), f=f)
    frame = _frame(rows, "navco", "n30_cy", [N.sha256(body)])
    est = Counter(x.day.year for x in lines if x.country == "estonia")
    got = {
        r["period_start"].year: int(r["value"])
        for r in frame.filter((pl.col("iso3") == "EST") & (pl.col("indicator") == "action_lines")).iter_rows(named=True)
    }
    assert got == dict(est)
    protest = frame.filter((pl.col("iso3") == "EGY") & (pl.col("indicator") == "action_lines|verb=14_protest"))
    assert int(protest["value"][0]) == sum(x.verb == 14 for x in egypt)
    assert set(frame["iso3"].unique()) == {"EST", "EGY", "USA"}
    assert "items" not in json.loads(frame["flags"][0])  # row numbers are not listed (the selector has them)
