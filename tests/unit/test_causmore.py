"""CAUSMORE connectors (COW War Data v4, PA-X v10, IDMC GIDD disasters) -- offline, no database.

Fixtures (``tests/fixtures/source/atlas.{cowwars,pax,idmc}``) are slices of the real 2026-10-17 downloads in the
providers' own formats plus the full codebook PDFs (pinned sha256); see each manifest's ``note``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_cowwars as W
from reva.adapters.connectors import atlas_idmc as I
from reva.adapters.connectors import atlas_pax as P
from reva.adapters.connectors import atlas_ucdp_dyadterm as D
from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.evidence.runner import FixtureHttp
from reva.kg.model import CAUSAL_PIDS

REPO = Path(__file__).resolve().parents[2]
MODULES = (W, P, I, D)


def _body(name: str, url: str) -> Any:
    return asyncio.run(FixtureHttp(name).get(url))


@pytest.fixture(scope="module")
def wars() -> dict[str, Any]:
    raws = {k: _body("atlas.cowwars", W.file_url(k)) for k in W.KINDS}
    return W.build(raws, strict=False)


@pytest.fixture(scope="module")
def pax() -> dict[str, Any]:
    return P.build(_body("atlas.pax", P.DATA_URL), country_qids={"COL": "Q739", "BIH": "Q225"}, strict=False)


@pytest.fixture(scope="module")
def idmc() -> dict[str, Any]:
    url, _ = I.resource_url(_body("atlas.idmc", I.PACKAGE_URL).body)
    return I.build(_body("atlas.idmc", url), country_qids={"CHN": "Q148"}, strict=False)


@pytest.fixture(scope="module")
def dyad() -> dict[str, Any]:
    return D.build(_body("atlas.ucdp_dyadterm", D.DATA_URL), strict=False)


def _claims(out: dict[str, Any]) -> list[cd.ClaimRecord]:
    c = out["claims"]
    return [x for v in c.values() for x in v] if isinstance(c, dict) else list(c)


# ----------------------------------------------------------------------------- registry


def test_relations_registry_is_consistent_with_the_causal_vocabulary() -> None:
    rel = cm.relations()
    vocab = yaml.safe_load((REPO / "registry/causal/relations.yaml").read_text())["relations"]
    for key, ds in rel["datasets"].items():
        assert ds["codebook_document"] in rel["documents"], key
        for m in ds["mappings"]:
            assert m["reva_kind"] in ("claim", "context") and m["why"].strip(), (key, m)
            if m["reva_kind"] == "claim":
                assert m["causal"] is True and m["relation"] in vocab, (key, m)
                term = m["codebook_term"]
                if term.endswith("=<value>"):
                    base = term.split("=")[0]
                    assert any(k.startswith(base + "=") for k in ds["definitions"]), (key, m)
                else:
                    assert term in ds["definitions"], (key, m)
            else:
                assert m["causal"] is False and "relation" not in m, (key, m)
        for t in ds["definitions"].values():
            assert len(t["text"]) > 30 and t["document"] in rel["documents"]
            assert t.get("pdf_page", 0) >= 1 or t.get("cell", "").startswith("README!")
    # transformations, identifiers, processes, stages and coded issues are never claims
    for key in ("cow_inter", "cow_extra", "cow_intra", "cow_nonstate"):
        trans = [m for m in rel["datasets"][key]["mappings"] if "TransTo" in m["variables"]]
        assert trans and all(m["reva_kind"] == "context" for m in trans)
    for m in rel["datasets"]["pax"]["mappings"]:
        if set(m["variables"]) & {"UcdpAgr", "PP", "Stage", "Contp"}:
            assert m["reva_kind"] == "context", m


def test_fixture_codebooks_are_the_pinned_documents(idmc: dict[str, Any]) -> None:
    for k in W.KINDS:
        assert cm.check_pdf(W.CODEBOOKS[k], _body("atlas.cowwars", W.codebook_url(k)).body)["matches"], k
    doc = cm.document("pax_codebook")
    assert cm.check_pdf("pax_codebook", _body("atlas.pax", doc["download_url"]).body)["matches"]
    assert not cm.check_pdf("pax_codebook", b"%PDF-1.4 other")["matches"]
    assert idmc["readme"]["matches"], idmc["readme"]
    bad = cm.check_texts("idmc_disasters", ["Disaster displacement: something else"])
    assert not bad["matches"] and "disaster_displacement" in bad["missing_definitions"]


def test_codebook_definitions_are_quoted_verbatim_in_deliveries() -> None:
    cb = cm.codebook("cow_inter", {"cow_inter_codebook": "a" * 64})
    assert cb["definitions"]["Outcome"].startswith("Outcome: coded as: 1 = Winner 2 = Loser")
    assert "the initial war ends and the conflict is transformed" in cb["definitions"]["transformation"]
    assert "sha256 aaaaaaaaaaaa" in cb["version"] and cb["url"].endswith("Inter-StateWars_Codebook.pdf")
    p = cm.codebook("pax")["definitions"]
    assert p["peace_agreement"].endswith("addressing conflict with a view to ending it")
    i = cm.codebook("idmc_disasters")["definitions"]
    assert "as a result, or in anticipation of the negative impact of natural hazards" in i["disaster_displacement"]


# ----------------------------------------------------------------------------- COW wars


def test_cow_war_parsing_versions_and_numbers() -> None:
    assert W.war_number("856.0") == "856" and W.war_number("502.1") == "502.1" and W.war_number("-8") is None
    assert W.war_numbers("404, 410") == ["404", "410"]
    with pytest.raises(cd.CausdataError):
        W.parse("inter", b"WarNum,WarName\r1,x\r")


def test_cow_outcome_claims(wars: dict[str, Any]) -> None:
    claims = _claims(wars)
    assert claims and {c.relation for c in claims} == {"coded_outcome"}
    assert all(c.tier == "T1" and c.codebook_term == "Outcome" and c.mechanism is None for c in claims)
    assert all(c.cause_id.startswith("cow:war:") and c.effect_id.startswith("cowwar:outcome:") for c in claims)
    inter = [c for c in wars["claims"]["cow_inter"] if c.cause_id == "cow:war:1"]
    assert {(c.scope["state"], c.scope["outcome"]) for c in inter} == {("Spain", "Loser"), ("France", "Winner")}
    # 482 is coded 5 (ongoing as of 12/31/2007): an attribute, never an outcome claim
    assert not [c for c in claims if c.cause_id == "cow:war:482"]
    st = {(s.subject, s.property): s for s in wars["statements"]}
    assert st[("cow:war:482", "cowwar:outcome")].value["value"] == "The war is ongoing as of 12/31/2007"
    assert ("cow:war:482", "P582") not in st  # ongoing: no end date
    ent = {e.qid: e for e in wars["entities"]}
    assert ent["cow:war:856"].labels["en"] == "Eighth Colombia" and ent["cow:war:856"].kind == "war"
    assert "cow:war:1" not in ent  # inter-state war entities are atlas.cow's
    assert st[("cow:war:1", "P580")].value["time"] == "1823-04-07"


def test_cow_transformations_are_context_not_claims(wars: dict[str, Any]) -> None:
    tr = [s for s in wars["statements"] if s.property == "cowwar:transformed_into"]
    pairs = {(s.subject, s.object_qid) for s in tr}
    assert ("cow:war:327", "cow:war:19") in pairs  # extra-state 327 transformed into inter-state 19
    coded = {s.qualifiers["coded_by"] for s in tr if (s.subject, s.object_qid) == ("cow:war:327", "cow:war:19")}
    assert coded == {"TransTo", "TransFrom"}  # both files code it, each kept with its own row
    war_to_war = [c for c in _claims(wars) if c.effect_id.startswith("cow:war:")]
    assert not war_to_war
    # 1541 -> 571: war 571 is not in the slice -> reported, never linked
    assert any(a.get("WarNum") == "1541" for a in wars["anomalies"]["transformation_to_unknown_war"])
    assert wars["transformations"]["transformation_pairs"] >= 5


def test_cow_intra_participants(wars: dict[str, Any]) -> None:
    p = [s for s in wars["statements"] if s.property == "P710"]
    assert p and all(s.subject.startswith("cow:war:") and s.object_qid.startswith("cow:") for s in p)
    assert all(s.qualifiers["side"] in ("A", "B") for s in p)


# ----------------------------------------------------------------------------- PA-X


def test_pax_conflict_links_are_context_not_claims(pax: dict[str, Any]) -> None:
    # the codebook says what an agreement addresses, not that it reacts to the conflict (audit M6 reading)
    assert pax["claims"] == []
    rel = {m["variables"][0]: m for m in cm.dataset_entry("pax")["mappings"]}
    assert rel["UcdpCon"]["reva_kind"] == "context" and rel["CowWar"]["reva_kind"] == "context"
    st = pax["statements"]
    wars = {s.object_qid for s in st if s.property == "pax:cow_war"}
    assert {"cow:war:856", "cow:war:215"} <= wars
    assert any(s.property == "pax:ucdp_conflict" and s.object_qid.startswith("ucdp:conflict:") for s in st)
    # no agreement -> agreement link of any kind (Imp / OthAgr name no agreement id)
    assert not [s for s in st if s.subject.startswith("pax:agt:") and (s.object_qid or "").startswith("pax:agt:")]
    ids = [s for s in st if s.property == "pax:ucdp_agreement_id"]
    assert {s.value["value"] for s in ids} >= {"1070", "1502"}
    assert any(s.property == "P361" and s.object_qid.startswith("pax:process:") for s in st)
    assert any(s.property == "P17" and s.object_qid == "Q739" for s in st)
    assert {e.kind for e in pax["entities"]} == {"peace_agreement", "peace_process"}


def test_pax_same_as_rule_is_declared() -> None:
    from reva.events.same_as import load_rules

    r = {x["id"]: x for x in load_rules()["rules"]}["pax_ucdp_agreement_id"]
    assert r["status"] == "active" and r["kind"] == "prefix_vs_property"
    assert r["id_prefix"] == "ucdp:pa:" and r["property"] == "pax:ucdp_agreement_id"


# ----------------------------------------------------------------------------- IDMC


def test_idmc_has_effect_claims(idmc: dict[str, Any]) -> None:
    claims = idmc["claims"]
    assert claims and {(c.relation, c.polarity) for c in claims} == {("has_effect", "promotes")}
    for c in claims:
        assert c.cause_id.startswith("idmc:event:") and c.effect_id.startswith("idmc:displacement:")
        assert c.effect_id.split(":")[2] == c.cause_id.split(":")[2]
        assert c.codebook_term == "disaster_displacement" and c.selector["row"] >= 2
    ent = {e.qid: e for e in idmc["entities"]}
    assert "idmc:event:1" in ent and ent["idmc:displacement:1:CHN:2016"].kind == "disaster_displacement"
    st = {(s.subject, s.property): s for s in idmc["statements"]}
    n = st[("idmc:displacement:1:CHN:2016", "idmc:internal_displacements")]
    assert n.value["type"] == "quantity" and int(n.value["amount"]) > 0
    assert st[("idmc:displacement:1:CHN:2016", "P17")].object_qid == "Q148"
    assert st[("idmc:event:1", "P580")].qualifiers["approximate"] is True
    assert any(s.property == "idmc:glide_number" for s in idmc["statements"])
    # a multi-country event is one event with one displacement record per country-year
    multi = [k for k, v in __import__("collections").Counter(c.cause_id for c in claims).items() if v > 1]
    assert multi


def test_idmc_dates_and_codes() -> None:
    assert I.excel_date("39661") == "2008-08-01"
    assert I.event_codes("FL-2016-000064-CHN:Glide Number; MDRIN028:IFRC Appeal ID") == [
        ("FL-2016-000064-CHN", "Glide Number"),
        ("MDRIN028", "IFRC Appeal ID"),
    ]
    assert (
        I.event_key(
            {
                "Event ID": "42",
                "ISO3": "",
                "Year": "",
                "Event Name": "",
                "Date of Event (start)": "",
                "Hazard Sub Type": "",
                "Disaster Internal Displacements (Raw)": "",
                "Event Codes (Code:Type)": "",
            }
        )
        == "42"
    )
    with pytest.raises(cd.CausdataError):
        I.resource_url(b'{"success": true, "result": {"name": "other", "organization": {"name": "x"}}}')


# ----------------------------------------------------------------------------- UCDP dyad termination


def test_ucdp_dyad_termination_claims(dyad: dict[str, Any]) -> None:
    claims = dyad["claims"]
    assert claims and {c.relation for c in claims} == {"coded_outcome"}
    defs = cm.codebook("ucdp_term_dyad")["definitions"]
    assert all(c.codebook_term in defs for c in claims)
    # one termination per episode, or one per terminal row when the source codes several (audit2 N6)
    assert all(c.effect_id.startswith(c.cause_id + ":termination") for c in claims)
    assert "There is no information in this dataset regarding the group-specific circumstance" in defs["d_outcome=5"]
    assert cm.check_pdf(
        "ucdp_term_codebook", _body("atlas.ucdp_dyadterm", cm.document("ucdp_term_codebook")["download_url"]).body
    )["matches"]
    st = dyad["statements"]
    assert any(s.property == "P710" and s.object_qid.startswith("ucdp:actor:") for s in st)
    assert any(s.property == "P361" and s.object_qid == "ucdp:conflict:200" for s in st)
    # ceasefire / agreement dates are dated facts on the termination, never a link to an agreement
    for s in st:
        if s.property in ("ucdpterm:dyad_ceasefire_date", "ucdpterm:dyad_peace_agreement_date"):
            assert s.object_qid is None and s.value["type"] == "time"
    assert "64901" in dyad["anomalies"].get("episodes_with_several_terminal_rows", [])


# ----------------------------------------------------------------------------- common


def test_no_causal_kg_predicate_and_unique_keys(
    wars: dict[str, Any], pax: dict[str, Any], idmc: dict[str, Any], dyad: dict[str, Any]
) -> None:
    assert not set(cm.CAUSMORE_PROPERTIES) & CAUSAL_PIDS
    allowed = set(cm.CAUSMORE_PROPERTIES) | set(cm.WIKIDATA_PIDS) | set(cd.CAUSDATA_PROPERTIES) | set(cd.WIKIDATA_PIDS)
    for out in (wars, pax, idmc, dyad):
        used = {s.property for s in out["statements"]}
        assert not used & CAUSAL_PIDS
        assert used <= allowed, used - allowed
        cm.check_unique(out["entities"], out["statements"])


def test_modules_fetch_only_through_the_run() -> None:
    for mod in (*MODULES, cm):
        src = Path(mod.__file__).read_text()
        for bad in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen(", "requests.", "User-Agent"):
            assert bad not in src, (mod.__name__, bad)
    for mod in MODULES:
        spec = mod.atlas_source()
        assert spec.budget_key == spec.source_key and spec.planned_requests >= 2
