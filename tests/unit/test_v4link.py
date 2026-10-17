"""V4LINK: UNSD classification concordances and Wikidata identifier bridges (parsers and rules, no DB, no network).

Fixture: tests/fixtures/source/atlas.unsd_classif (trimmed from the live UNSD files of 2026-10-17; licence in the
manifest: UN terms of use, HS descriptions (c) WCO flagged)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reva.adapters.connectors import atlas_unsd_classif as uc
from reva.adapters.connectors import atlas_v4link_wikidata as wd
from reva.adapters.connectors import v4link_common as common
from reva.adapters.connectors.v4link_common import Held

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "atlas.unsd_classif"


def _bodies() -> dict[str, bytes]:
    man = {e["url"]: e for e in json.loads((FIX / "manifest.json").read_text())}
    return {f.key: (FIX / man[f.url]["file"]).read_bytes() for f in uc.FILES}


@pytest.fixture(scope="module")
def parsed() -> uc.Parsed:
    return uc.parse_all(_bodies())


def test_manifest_covers_every_file_with_licence() -> None:
    man = json.loads((FIX / "manifest.json").read_text())
    assert {e["url"] for e in man} == {f.url for f in uc.FILES}
    assert all("World Customs Organization" in e["licence"] and e["fixture_note"] for e in man)


def test_structures_and_hierarchy(parsed: uc.Parsed) -> None:
    c = parsed.codes
    assert c["hs2022:2709"].title == "Petroleum oils and oils obtained from bituminous minerals; crude"
    assert c["hs2022:270900"].parent == "2709" and c["hs2022:2709"].parent == "27" and c["hs2022:27"].parent is None
    assert c["hs1992:270900"].level == "subheading"
    # CPC: each level adds one digit; ISIC: a division belongs to the section row before it
    assert c["cpc21:12010"].parent == "1201" and c["cpc21:120"].parent == "12" and c["cpc21:1"].parent is None
    assert c["isic4:0610"].parent == "061" and c["isic4:06"].parent == "B" and c["isic4:19"].parent == "C"
    assert c["isic4:0610"].title == "Extraction of crude petroleum"
    assert c["sitc4:333"].parent == "33"
    # the TOTAL rows are not codes; SITC divisions absent from the file are reported, never invented
    assert parsed.report["hs2022_structure"]["total_row"] == 1
    assert parsed.report["sitc4_structure"]["parent_missing"] > 0 and "sitc4:28" not in c


def test_concordances_keep_the_tables_flags(parsed: uc.Parsed) -> None:
    hs_cpc = {(p.left, p.right): p for p in parsed.pairs["hs2017_cpc21"]}
    p = hs_cpc[("hs2017:270900", "cpc21:12010")]
    assert p.quals == {"table": "CPC21-HS2017", "hs_partial": "0", "cpc_partial": "0", "hs_code_as_stated": "2709.00"}
    # 2710.12 is split over several CPC subclasses: HS partial = 1 as stated
    assert {q.quals["hs_partial"] for (a, _b), q in hs_cpc.items() if a == "hs2017:271012"} == {"1"}
    cpc_isic = {(p.left, p.right) for p in parsed.pairs["cpc21_isic4"]}
    assert ("cpc21:12010", "isic4:0610") in cpc_isic and ("cpc21:12020", "isic4:0620") in cpc_isic
    hs92 = {(p.left, p.right): p for p in parsed.pairs["hs2022_hs1992"]}
    assert hs92[("hs2022:270900", "hs1992:270900")].quals == {
        "table": "HS2022-HS1992 Correlations",
        "relationship": "1:1",
        "unsd_conversion": True,
    }
    sitc = {(p.left, p.right) for p in parsed.pairs["hs2022_sitc4"]}
    assert ("hs2022:270900", "sitc4:3330") in sitc


def test_kg_build_cites_raw_and_flags_hs(parsed: uc.Parsed) -> None:
    raw = {f.key: f"{i:064x}" for i, f in enumerate(uc.FILES)}
    rev = {f.key: f"{f.key}@2026-10-17" for f in uc.FILES}
    ents, sts, _rep = uc.build_kg(parsed, raw=raw, revisions=rev)
    assert len(ents) == len(parsed.codes) and all(e.labels["en"] for e in ents)
    by = {(s.subject_qid, s.property_pid, s.object_qid): s for s in sts}
    st = by[("hs2017:270900", common.P_CORRESPONDS, "cpc21:12010")]
    assert st.raw_sha256 == raw["hs2017_cpc21"] and st.qualifiers["redistribution"].startswith("unverified")
    assert st.statement_id == "concord:hs2017_cpc21:hs2017:270900=cpc21:12010"
    isic = by[("cpc21:12010", common.P_CORRESPONDS, "isic4:0610")]
    assert "redistribution" not in isic.qualifiers and isic.references[0]["selector"]["file"] == "cpc21_isic4"
    assert ("isic4:0610", common.P_CLASS_PARENT, "isic4:061") in by
    assert len({s.statement_id for s in sts}) == len(sts)


def test_parse_rejects_drifted_header() -> None:
    b = _bodies()
    b["cpc21_isic4"] = b["cpc21_isic4"].replace(b"ISIC4code", b"ISIC4_code")
    with pytest.raises(uc.ClassifError, match="missing"):
        uc.parse_all(b)


# ============================================================================================== identity rules
def test_rules_file_loads_and_guards() -> None:
    rules = common.load_rules()
    r = rules.rules
    assert r["P683"].key("15377") == "chebi:15377" and r["P683"].key("CHEBI:15377") == "chebi:15377"
    assert r["P5270"].key("MONDO_0005148") == "mondo:0005148"
    assert r["P699"].key("DOID:9352") == "doid:9352"
    assert r["P5531"].key("0000320193") == "sec_cik:320193"
    assert r["P5471"].key("2709.00") == "hs2022:270900" and r["P5471"].relation == "classification"
    assert r["P300"].key("FR-IDF") == "iso3166_2:FR-IDF" and r["P300"].key("fr-idf") is None
    assert r["P494"].key("I") == "icd10:I" and r["P494"].key("E11.9") == "icd10:E11.9"
    assert r["P13333"].status == "inactive"  # GEM wiki titles are not valid KG keys (owner decision)
    assert all(rules.packs[p].properties for p in rules.packs)


def _h(item: str, pid: str, v: str, rank: str = "normal", sid: str | None = None) -> Held:
    return Held(item=item, property=pid, value=v, statement_id=sid or f"{item}${pid}-{v}", rank=rank)


def test_derive_bridges_ambiguity_rank_and_format() -> None:
    rules = common.load_rules().rules
    rows = [
        _h("Q283", "P683", "15377"),
        _h("Q283", "P662", "962"),
        _h("Q1", "P683", "100"),  # value on two items -> ambiguous, never linked
        _h("Q2", "P683", "100"),
        _h("Q3", "P683", "200"),  # item with two values -> ambiguous holder
        _h("Q3", "P683", "201"),
        _h("Q4", "P683", "300", rank="preferred"),  # best rank: the normal one is ignored
        _h("Q4", "P683", "301"),
        _h("Q5", "P683", "abc"),  # malformed
        _h("Q6", "P662", "777"),
    ]
    br = common.derive_bridges(rows, rules, global_duplicates={"P662": {"pubchem_cid:777"}})
    linked = {(h.item, k) for h, k in br.links}
    assert linked == {("Q283", "chebi:15377"), ("Q283", "pubchem_cid:962"), ("Q4", "chebi:300")}
    p683 = br.report["by_property"]["P683"]
    assert p683["ambiguous_value"] == 2 and p683["ambiguous_holder"] == 2 and p683["malformed"] == 1
    assert br.report["by_property"]["P662"]["ambiguous_value"] == 1  # held elsewhere in Wikidata


def test_classification_rule_allows_several_codes() -> None:
    rules = common.load_rules().rules
    rows = [_h("Q22656", "P5471", "2709"), _h("Q22656", "P5471", "2710"), _h("Q9", "P5471", "2709")]
    br = common.derive_bridges(rows, rules)
    assert {(h.item, k) for h, k in br.links} == {
        ("Q22656", "hs2022:2709"),
        ("Q22656", "hs2022:2710"),
        ("Q9", "hs2022:2709"),
    }


def test_bridge_statements_direction_and_provenance() -> None:
    from datetime import UTC, datetime

    rules = common.load_rules()
    rows = [_h("Q90", "P300", "FR-IDF"), _h("Q90", "P605", "FR1"), _h("Q22656", "P5471", "2709")]
    sts, _rep = wd.bridge_statements(
        rows,
        rules,
        rules.packs["places"],
        raw_sha256="a" * 64,
        query_sha256="b" * 64,
        retrieved_at=datetime(2026, 10, 17, tzinfo=UTC),
    )
    by = {(s.subject_qid, s.property_pid, s.object_qid) for s in sts}
    assert ("iso3166_2:FR-IDF", common.P_WIKIDATA_ITEM, "Q90") in by and (
        "nuts:FR1",
        common.P_WIKIDATA_ITEM,
        "Q90",
    ) in by
    assert ("Q22656", common.P_CLASSIFIED_AS, "hs2022:2709") in by
    s0 = next(s for s in sts if s.subject_qid == "iso3166_2:FR-IDF")
    assert s0.statement_id.startswith("v4xref:places:") and s0.qualifiers["value_as_stated"] == "FR-IDF"
    assert s0.raw_sha256 == "a" * 64 and s0.source_revision == "wdqs@2026-10-17T00:00:00Z"


def test_pack_queries_are_bounded_and_rank_filtered() -> None:
    rules = common.load_rules()
    q = wd.pack_query(rules.packs["chemistry"])
    assert "wdt:P683 [] ; wdt:P662 []" in q and "?sl >= 10" in q and "DeprecatedRank" in q
    q = wd.pack_query(rules.packs["health"])
    assert "HAVING (COUNT(DISTINCT ?pd) >= 2)" in q
    assert "GROUP BY ?v HAVING (COUNT(DISTINCT ?i) > 1)" in wd.duplicates_query("P683")


def test_held_rows_parses_wdqs_bindings() -> None:
    b = {
        "item": {"value": "http://www.wikidata.org/entity/Q90"},
        "st": {"value": "http://www.wikidata.org/entity/statement/Q90-ABC"},
        "prop": {"value": "P300"},
        "v": {"value": "FR-IDF"},
        "rank": {"value": "http://wikiba.se/ontology#NormalRank"},
        "en": {"value": "Paris"},
    }
    rows, labels, skipped = wd.held_rows([b, b, {**b, "rank": {"value": "http://wikiba.se/ontology#DeprecatedRank"}}])
    assert rows == [Held("Q90", "P300", "FR-IDF", "Q90$ABC", "normal")] and labels == {"Q90": {"en": "Paris"}}
    assert skipped["row_incomplete_or_deprecated"] == 1


def test_sources_declared() -> None:
    s = uc.atlas_source()
    assert s.allowed_hosts == ("unstats.un.org",) and s.planned_requests == len(uc.FILES)
    w, q = wd.atlas_source()
    assert w.budget_key == "wikidata.wdqs" and w.allowed_hosts == ("query.wikidata.org",)
    assert q.name == "atlas.v4link_qlever" and q.allowed_hosts == ("qlever.dev",) and q.source_key == "qlever.wikidata"


def test_atlas_dimension_declaration_points_at_loaded_codes(parsed: uc.Parsed) -> None:
    import yaml

    from reva.adapters.connectors import atlas_baci

    doc = yaml.safe_load((common.REPO_ROOT / "registry" / "kg" / "v4link_atlas_dimensions.yaml").read_text())
    hs2 = next(d for d in doc["dimensions"] if d["dimension"] == "hs2")
    assert hs2["kg_prefix"] == "hs1992:" and atlas_baci.HS == "HS92"
    assert "27" in atlas_baci.HS2_CHAPTERS and f"{hs2['kg_prefix']}27" in parsed.codes


def test_statement_ids_from_wdqs_and_qlever_iris() -> None:
    assert wd.statement_id("http://www.wikidata.org/entity/statement/Q90-ABC-1") == "Q90$ABC-1"
    assert wd.statement_id("http://www.wikidata.org/entity/statement/q100166-60EB") == "Q100166$60EB"
    assert wd.statement_id("http://example.org/x") is None
