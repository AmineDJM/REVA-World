"""CAUSDATA connectors (ICB v16, COW MID 5.0/5.01, UCDP termination + peace agreements) -- offline, no database.

Fixtures (``tests/fixtures/source/atlas.{icb,mid,ucdp_term}``) are slices of the real 2026-10-16 downloads in the
providers' own formats plus the full codebook PDFs (pinned sha256), see each manifest's ``note``.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_geopol_common as G
from reva.adapters.connectors import atlas_icb as I
from reva.adapters.connectors import atlas_mid as M
from reva.adapters.connectors import atlas_ucdp_term as T
from reva.adapters.connectors import causdata_common as cd
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kg.model import CAUSAL_PIDS

REPO = Path(__file__).resolve().parents[2]
MODULES = (I, M, T)


def _body(name: str, url: str) -> Any:
    return asyncio.run(FixtureHttp(name).get(url))


@pytest.fixture(scope="module")
def crosswalk() -> G.StateCodeCrosswalk:
    return G.crosswalk_from_countrycode_tarball(_body("atlas.icb", G.COUNTRYCODE_URL).body, G.crosswalk_keys())


@pytest.fixture(scope="module")
def icb(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    raws = {k: _body("atlas.icb", I.data_url(k)) for k in I.DATA_FILES}
    return I.build(raws, crosswalk=crosswalk, qid_for_iso3={}, countrycode_sha256="0" * 64, strict=False)


@pytest.fixture(scope="module")
def mid(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    raws = {k: _body("atlas.mid", M.zip_url(k)) for k in M.ZIPS}
    return M.build(raws, crosswalk=crosswalk, qid_for_iso3={}, countrycode_sha256="0" * 64, strict=False)


@pytest.fixture(scope="module")
def term(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    raws = {k: _body("atlas.ucdp_term", u) for k, u in T.URLS.items()}
    return T.build(raws, crosswalk=crosswalk, strict=False)


def _st(out: dict[str, Any]) -> dict[tuple[str, str], Any]:
    return {(s.subject, s.property): s for s in out["statements"]}


# ----------------------------------------------------------------------------- registry: relations_datasets.yaml


def test_relations_registry_is_consistent_with_the_causal_vocabulary() -> None:
    rel = cd.relations()
    vocab = yaml.safe_load((REPO / "registry/causal/relations.yaml").read_text())["relations"]
    for key, ds in rel["datasets"].items():
        for m in ds["mappings"]:
            assert m["reva_kind"] in ("claim", "context"), (key, m)
            assert m["why"].strip(), (key, m)
            if m["reva_kind"] == "claim":
                assert m["causal"] is True and m["relation"] in vocab, (key, m)
                term = m["codebook_term"].split("=")[0]
                assert any(t.split("=")[0] == term for t in ds["definitions"]), (key, term)
            else:
                assert m["causal"] is False and "relation" not in m, (key, m)
        if ds.get("codebook_document"):
            doc = rel["documents"][ds["codebook_document"]]
            assert len(doc["sha256"]) == 64
            for t in ds["definitions"].values():
                assert len(t["text"]) > 40 and t["document"] in rel["documents"] and t["pdf_page"] >= 1
    # the coded issue is never a claim
    for key in ("mid5", "term_conflict", "ucdp_acd_incompatibility"):
        issue = [
            m
            for m in rel["datasets"][key]["mappings"]
            if "coded_issue" in str(m.get("predicate", "")) or "incompatibility" in m["variables"]
        ]
        assert issue and all(m["reva_kind"] == "context" for m in issue), key


def test_fixture_codebooks_are_the_pinned_documents(icb: dict[str, Any], mid: dict[str, Any]) -> None:
    for ds, key in I.CODEBOOKS.items():
        doc = cd.document(key)
        assert cd.check_document(key, _body("atlas.icb", doc["download_url"]).body)["matches"], ds
    assert all(v["matches"] for v in mid["documents"].values())
    for key in T.CODEBOOKS.values():
        doc = cd.document(key)
        assert cd.check_document(key, _body("atlas.ucdp_term", doc["download_url"]).body)["matches"]
    assert not cd.check_document("icb1_codebook", b"%PDF-1.4 something else")["matches"]


def test_codebook_definitions_are_quoted_verbatim_in_deliveries() -> None:
    cb = cd.codebook("icb2", {"icb2_codebook": "f" * 64})
    assert cb["definitions"]["TRIGGR"].startswith("The trigger or precipitating cause of a foreign policy crisis")
    assert "sha256 ffffffffffff" in cb["version"] and cb["url"].startswith("https://duke.box.com/")
    t = cd.codebook("term_conflict")["definitions"]
    assert "There is no information in this dataset regarding the group-specific circumstance" in t["c_outcome=5"]
    m = cd.codebook("mid5")["definitions"]
    assert "the type of revisions sought by that state" in m["revision_type"]


# ----------------------------------------------------------------------------- dates


def test_coded_dates_lower_precision_never_guess() -> None:
    assert cd.coded_date("1962", "10", "16") == ("1962-10-16", 11)
    assert cd.coded_date("1953", "10", "88") == ("1953-10-01", 10)  # ICB 88 = unknown day
    assert cd.coded_date("1902", "7", "-9", missing=("", "-9")) == ("1902-07-01", 10)  # MID -9
    assert cd.coded_date("1918", "5", "") == ("1918-05-01", 10)
    assert cd.coded_date("1918", "", "") == ("1918-01-01", 9)
    assert cd.coded_date("1918", "6", "31") == ("1918-06-01", 10)  # impossible day: kept at month precision
    assert cd.coded_date("", "5", "1") is None
    assert cd.ucdp_date("1949-09-15", "3").value == ("1949-09-01", 10)
    assert cd.ucdp_date("1949-09-15", "5").value == ("1949-01-01", 9)


# ----------------------------------------------------------------------------- ICB


def test_icb_events_dates_and_trigger(icb: dict[str, Any]) -> None:
    st = _st(icb)
    assert icb["row_counts"] == {"icb1": 7, "icb2": 29}
    assert st[("icb:crisis:196", "P580")].value == {"type": "time", "time": "1962-10-16", "precision": 11}
    assert st[("icb:crisis:196", "P582")].value["time"] == "1962-11-20"
    assert st[("icb:crisis:143", "P582")].value == {"type": "time", "time": "1953-10-01", "precision": 10}
    assert st[("icb:crisis:196:trigger", "icb:trigger_type")].value["value"] == "External change"
    # TRIGENT 365 in 1962 = the Soviet Union (historical QID), never Russia
    assert st[("icb:crisis:196:trigger", "icb:triggering_entity")].object_qid == "Q15180"
    assert st[("icb:crisis:1:trigger", "icb:triggering_entity_kind")].value["value"] == "non-state actor"
    kinds = Counter(e.kind for e in icb["entities"])
    assert kinds["international_crisis"] == 7 and kinds["foreign_policy_crisis"] == 29
    # crises inside a protracted conflict are part of it; regional buckets 40-44 are not
    assert st[("icb:crisis:196", "P361")].object_qid == "icb:pc:27"
    assert not any(s.object_qid in {f"icb:pc:{b}" for b in I.PC_BUCKETS} for s in icb["statements"])


def test_icb_claims(icb: dict[str, Any]) -> None:
    c1, c2 = icb["claims"]["icb1"], icb["claims"]["icb2"]
    assert Counter(c.relation for c in c1) == {"coded_trigger": 6, "coded_outcome": 6}  # 510 is a placeholder
    assert not any("icb:crisis:510" in (c.cause_id + c.effect_id) for c in c1 + c2)
    by = {(c.cause_id, c.relation): c for c in c2}
    t = by[("icb:crisis:196:actor:450:trigger", "coded_trigger")]
    assert t.effect_id == "icb:crisis:196:actor:450" and t.codebook_term == "TRIGGR" and t.tier == "T1"
    assert t.selector["file"] == "icb2v16.csv" and "triggr" in t.selector["columns"] and t.mechanism is None
    r = by[("icb:crisis:196:actor:450:trigger", "reaction_to")]
    assert r.effect_id == "icb:crisis:196:actor:450:response" and r.codebook_term == "MAJRES"
    o = by[("icb:crisis:196:actor:450", "coded_outcome")]
    assert o.effect_id == "icb:crisis:196:actor:450:outcome" and o.codebook_term == "OUTCOM"
    assert len({c.source_claim_id for c in c1}) == len(c1) and len({c.source_claim_id for c in c2}) == len(c2)
    ids = {e.qid for e in icb["entities"]}
    assert all(c.cause_id in ids and c.effect_id in ids for c in c1 + c2)


def test_icb_atlas_rows(icb: dict[str, Any]) -> None:
    frame = G.atlas_frame(icb["actor_cy"])
    validate_atlas_frame(frame, source="icb", dataset="actor_cy", raw_sha256s=[icb["shas"]["icb2"]])
    v = {(r["provider_geo"], r["indicator"], r["period_start"].year): r for r in icb["actor_cy"]}
    assert v[("icb:2", "icb_crises_as_actor", 1962)]["iso3"] == "USA"
    assert v[("icb:365", "icb_crises_triggered", 1962)]["iso3"] is None  # USSR: historical state, kept unmapped
    assert json.loads(v[("icb:2", "icb_crises_as_actor", 1962)]["flags"])["computed_from"].startswith("ICB")


# ----------------------------------------------------------------------------- MID


def test_mid_outcome_claims_and_issue_is_context(mid: dict[str, Any]) -> None:
    assert mid["row_counts"]["mida"] == 6
    assert {c.relation for c in mid["claims"]} == {"coded_outcome"}
    assert all(c.cause_id.startswith("mid:dispute:") and c.effect_id.endswith(":outcome") for c in mid["claims"])
    assert all(c.codebook_term in cd.dataset_entry("mid5")["definitions"] for c in mid["claims"])
    issues = [s for s in mid["statements"] if s.property == "causdata:coded_issue"]
    assert ("mid:dispute:2", "Territory", "USA") in {
        (s.subject, s.value["value"], s.qualifiers["stabb"]) for s in issues
    }
    assert all("not a cause" in s.qualifiers["note"] for s in issues)
    p = [s for s in mid["statements"] if s.property == "P710" and s.subject == "mid:dispute:2"]
    assert {s.qualifiers["side"] for s in p} == {"A", "B"}


def test_mid_incidents_follow_in_order_not_cause(mid: dict[str, Any]) -> None:
    f = {s.subject: s for s in mid["statements"] if s.property == "P155"}
    assert f["mid:incident:3551002"].object_qid == "mid:incident:3551001"
    assert "not a causal link" in f["mid:incident:3551002"].qualifiers["basis"]
    parts = {s.subject: s.object_qid for s in mid["statements"] if s.property == "P361"}
    assert parts["mid:incident:3551001"] == "mid:dispute:3551"
    frame = G.atlas_frame(mid["mid5_cy"])
    validate_atlas_frame(frame, source="cow", dataset="mid5_cy", raw_sha256s=[mid["shas"]["mid5"]])


# ----------------------------------------------------------------------------- UCDP termination + peace agreements


def test_ucdp_termination_claims(term: dict[str, Any]) -> None:
    c = term["claims"]["term_conflict"]
    # 17 episodes; 20901 has two terminal rows, both kept (audit2 N6)
    assert len(c) == 18 and {x.relation for x in c} == {"coded_outcome"}
    by = {x.cause_id: x for x in c}
    assert by["ucdp:episode:20001"].codebook_term == "c_outcome=4"
    assert by["ucdp:episode:28702"].codebook_term == "c_outcome=5"
    assert term["anomalies"] == {"episodes_with_several_terminal_rows": {"20901": 2}}
    st = _st(term)
    assert st[("ucdp:episode:20001:termination", "P585")].value == {
        "type": "time",
        "time": "1946-07-21",
        "precision": 11,
    }
    assert st[("ucdp:episode:20001", "P361")].object_qid == "ucdp:conflict:200"
    assert st[("ucdp:episode:20002", "P155")].object_qid == "ucdp:episode:20001"
    # conflict start (ACD start_date replicated in the termination data) dates KGGEOPOL's conflict entity
    assert st[("ucdp:conflict:200", "P580")].value == {"type": "time", "time": "1946-07-18", "precision": 11}


def test_peace_agreements_and_window_pairing_are_not_claims(term: dict[str, Any]) -> None:
    # audit M6 (AUDFIX): "conflicts related to the dyads signing" is relatedness -> context ucdppa:conflict, no claim
    pa = term["claims"]["peace_agreements"]
    assert pa == []
    ctx = {(s.subject, s.object_qid) for s in term["statements"] if s.property == "ucdppa:conflict"}
    assert ("ucdp:pa:1565", "ucdp:conflict:333") in ctx
    m = next(x for x in cd.dataset_entry("peace_agreements")["mappings"] if "conflict_id" in x["variables"])
    assert (m["reva_kind"], m["predicate"], m["causal"]) == ("context", "ucdppa:conflict", False)
    w = [s for s in term["statements"] if s.property == "causdata:agreement_in_termination_window"]
    assert {s.subject for s in w} >= {"ucdp:episode:33201:termination", "ucdp:episode:41602:termination"}
    assert all("not a causal claim" in s.qualifiers["rule"] for s in w)
    # nothing in any claim joins a termination to an agreement
    assert not any(x.cause_id.startswith("ucdp:pa:") for x in term["claims"]["term_conflict"] + pa)
    for ds in ("term_cy", "pa_cy"):
        validate_atlas_frame(
            G.atlas_frame(term[ds]),
            source="ucdp",
            dataset=ds,
            raw_sha256s=[term["shas"]["term" if ds == "term_cy" else "pa"]],
        )


def test_icb_inaction_is_coded_but_not_a_reaction(crosswalk: G.StateCodeCrosswalk, icb: dict[str, Any]) -> None:
    """Audit L3 (AUDFIX): MAJRES = 1 "No response-inaction" stays the coded response type, never a reaction_to."""
    import csv
    import dataclasses
    import io

    raws = {k: _body("atlas.icb", I.data_url(k)) for k in I.DATA_FILES}
    text = raws["icb2"].body.decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    key = next(k for k in rows[0] if k.strip().lower() == "majres")
    target = next(r for r in rows if r[key].strip() not in ("", "1"))
    target[key] = "1"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    raws["icb2"] = dataclasses.replace(raws["icb2"], body=buf.getvalue().encode("utf-8"))
    out = I.build(raws, crosswalk=crosswalk, qid_for_iso3={}, countrycode_sha256="0" * 64, strict=False)
    crisno, cracno = target["crisno"].strip(), target["cracno"].strip()
    claims = {c.source_claim_id for c in out["claims"]["icb2"] if c.relation == "reaction_to"}
    before = {c.source_claim_id for c in icb["claims"]["icb2"] if c.relation == "reaction_to"}
    assert f"{crisno}:{cracno}:majres" in before and f"{crisno}:{cracno}:majres" not in claims
    assert out["majres_inaction_no_claim"] == icb["majres_inaction_no_claim"] + 1
    rt = [s for s in out["statements"] if s.property == "icb:response_type" and f"actor:{cracno}:" in s.subject]
    assert rt and rt[0].value["value"] == "No response-inaction"


# ----------------------------------------------------------------------------- claims delivery + safety


def test_staged_delivery_round_trips(tmp_path: Path, icb: dict[str, Any]) -> None:
    d = cd.ClaimDelivery(
        source_key="icb.data",
        dataset="icb2",
        dataset_revision="v16",
        codebook=cd.codebook("icb2"),
        raw_sha256s=[icb["shas"]["icb2"]],
        retrieved_at=datetime.fromisoformat("2026-10-16T19:30:00+00:00"),
        origin="test_fixture",
        claims=icb["claims"]["icb2"],
    )
    run = SimpleNamespace(options={"claims_staging": str(tmp_path)}, engine=None, store=None)
    rep = cd.deliver_claims(run, [d])
    assert rep["icb2"]["status"] == "staged" and rep["icb2"]["claims"] == len(icb["claims"]["icb2"]) == 72
    doc = json.loads(Path(rep["icb2"]["file"]).read_text())
    assert doc["interface"] == "reva.causal.load_dataset_claims" and doc["complete"] is True
    assert set(doc["claims"][0]) == {
        "cause_id",
        "effect_id",
        "relation",
        "selector",
        "raw_sha256",
        "codebook_term",
        "source_claim_id",
        "polarity",
        "mechanism",
        "scope",
        "stance",
        "contests",
        "tier",
        "citation",
    }
    back = cd.ClaimDelivery.from_json(doc)
    assert back.claims == d.claims and back.retrieved_at == d.retrieved_at
    assert cd.deliver_claims(SimpleNamespace(options={}, engine=None), [d])["icb2"]["status"] == "causal_unavailable"


def test_no_causal_kg_predicate_and_unique_keys(icb: dict[str, Any], mid: dict[str, Any], term: dict[str, Any]) -> None:
    assert not set(cd.CAUSDATA_PROPERTIES) & CAUSAL_PIDS
    for out in (icb, mid, term):
        assert not {s.property for s in out["statements"]} & CAUSAL_PIDS
        cd.check_unique_keys(out["statements"])
        used = {s.property for s in out["statements"]}
        assert used <= set(cd.CAUSDATA_PROPERTIES) | set(cd.WIKIDATA_PIDS) | {"geopol:corresponds_to_iso3"}


def test_modules_fetch_only_through_the_run() -> None:
    for mod in (*MODULES, cd):
        src = Path(mod.__file__).read_text()
        for bad in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen(", "requests."):
            assert bad not in src, (mod.__name__, bad)
    for mod in MODULES:
        spec = mod.atlas_source()
        assert spec.budget_key == spec.source_key and spec.planned_requests >= 5
