"""FIX4A (audit AUDIT3, docs/dev/review_v4_v5_audit.md; report docs/dev/v6_fix4a.md) -- offline tests.

H1  Mondo "disruption / dysfunction / alteration" axioms never make the normal process or structure the cause or
    effect: the endpoint is a REVA-made altered-state node, the verbatim source relation stays in the selector.
H2  ClinGen Refuted / Disputed / No Known Disease Relationship are not associated_with (separate, unmapped predicate).
M1  DOID inheritance modes (IDO:0000664 + GENO filler) are doid:inheritance_mode statements, never causal claims.
M9  causal classification by the RO hierarchy or the explicit DEFINED_AS_CAUSATION allow-list only, never a label word.
L8  REVA-made ids keyed by a source name (Carbon Brief events, RESOLVE realms) carry an explicit identity-rule statement.

Inputs: the captured V4HEALTH / V4CLIMATE fixtures plus small synthetic stanzas appended in memory (marked below; not
source data).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_attribution as at
from reva.adapters.connectors import atlas_clingen as cg
from reva.adapters.connectors import atlas_ecoregions as eco
from reva.adapters.connectors import atlas_health_onto as ho
from reva.adapters.connectors.health_obo_common import (
    DEFINED_AS_CAUSATION,
    HealthSourceError,
    Relation,
    RelationIndex,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
REPO = Path(__file__).resolve().parents[2]
T = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)

#: synthetic RO stanzas (test input, wording of the real ro.obo 2026-09-04 definitions)
RO_EXTRA = b"""
[Typedef]
id: RO:0004021
name: disease has basis in disruption of
def: "A relation that holds between the disease and a process where the physical basis of the disease disrupts execution of a key biological process." []
is_a: RO:0004019 ! disease has basis in

[Typedef]
id: RO:0004030
name: disease arises from alteration in structure
def: "A relationship between a disease and an anatomical structure where the material basis of the disease is some pathological change in the structure." []
is_a: RO:0004019 ! disease has basis in

[Typedef]
id: RO:0004024
name: disease causes disruption of
def: "A relationship between a disease and a process where the disease process disrupts the execution of the process." []
is_a: RO:0004023 ! causal relationship with disease as subject

[Typedef]
id: RO:0007000
name: has driver
def: "A relation between two entities, in which one of the entities is any natural or human-influenced factor that directly or indirectly causes a change in the other entity." []

[Typedef]
id: RO:0007001
name: has disease driver
is_a: RO:0007000 ! has driver
"""
#: synthetic Mondo term (test input)
MONDO_EXTRA = b"""
[Term]
id: MONDO:9999901
name: synthetic test disease
relationship: RO:0004021 GO:0003824
relationship: RO:0004030 UBERON:0000948
relationship: RO:0004024 GO:0007049
relationship: RO:0014001 NCBITaxon:1773
"""


def _fx(source: str, name: str) -> bytes:
    return (FIX / source / name).read_bytes()


def _ro(source: str) -> RelationIndex:
    ri, _ = ho.ro_index(_fx(source, "ro.obo") + RO_EXTRA, raw_sha256="a" * 64)
    return ri


# ------------------------------------------------------------------------------------------------ M9
def test_causal_rule_reads_no_label_word() -> None:
    ri = _ro("atlas.doid")
    assert not ri.is_causal("RO:0007001")  # "has disease driver": under RO:0007000, not RO:0002410
    assert ri.is_causal("RO:0004021") and ri.is_causal("RO:0004024")
    # a relation whose label is full of causal words but is not under RO:0002410 is not causal
    ri.add(Relation("X:1", "causes and triggers and drives", None, (), (), "test"))
    assert not ri.is_causal("X:1")
    assert not ri.is_causal("predisposes_towards")


def test_defined_as_causation_allow_list_is_checked_verbatim() -> None:
    assert set(DEFINED_AS_CAUSATION) == {"IDO:0000664"}
    ri = RelationIndex()
    ri.add(Relation("IDO:0000664", "has material basis in", "Relating the disease to it’s causation (x).", (), (), "d"))
    assert ri.is_causal("IDO:0000664")
    changed = RelationIndex()
    changed.add(Relation("IDO:0000664", "has material basis in", "Something else.", (), (), "d"))
    with pytest.raises(HealthSourceError):
        changed.is_causal("IDO:0000664")


# ------------------------------------------------------------------------------------------------ M1 + M9 (DOID)
@pytest.fixture(scope="module")
def doid() -> ho.Parts:
    doc = json.loads(_fx("atlas.doid", "doid.json"))
    main = next(g for g in doc["graphs"] if str(g.get("id", "")).endswith("/doid.owl"))
    edge = main["edges"][0]
    # synthetic "has disease driver" edge on an existing DOID class (test input)
    main["edges"].append(
        {
            "sub": edge["sub"],
            "pred": "http://purl.obolibrary.org/obo/RO_0007001",
            "obj": "http://purl.obolibrary.org/obo/CHEBI_15377",
        }
    )
    parts, _ = ho.doid_kg(json.dumps(doc).encode(), raw_sha256="b" * 64, ri=_ro("atlas.doid"))
    return parts


def test_inheritance_modes_are_statements_not_claims(doid: ho.Parts) -> None:
    assert doid.claims, "the fixture still has causal axioms (pathogens)"
    assert not [c for c in doid.claims if c.cause_id.startswith("geno:") or c.effect_id.startswith("geno:")]
    inh = [s for s in doid.statements if s.property_pid == "doid:inheritance_mode"]
    assert inh and all(s.object_qid and s.object_qid.startswith("geno:") for s in inh)
    assert {s.qualifiers["source_relation"] for s in inh} == {"IDO:0000664"}
    assert {s.qualifiers["source_relation_label"] for s in inh} == {"has material basis in"}
    assert any(c.cause_id.startswith("ncbitaxon:") for c in doid.claims)  # pathogens stay causal (allow-list)


def test_disease_driver_is_an_unmapped_statement(doid: ho.Parts) -> None:
    assert not [c for c in doid.claims if c.selector.get("relation") == "RO:0007001"]
    drv = [s for s in doid.statements if s.property_pid == "doid:has_disease_driver"]
    assert [s.object_qid for s in drv] == ["chebi:15377"]


# ------------------------------------------------------------------------------------------------ H1 (Mondo)
@pytest.fixture(scope="module")
def mondo() -> ho.Parts:
    parts, _ = ho.mondo_kg(_fx("atlas.mondo", "mondo.obo") + MONDO_EXTRA, raw_sha256="c" * 64, ri=_ro("atlas.mondo"))
    return parts


def test_disruption_is_the_cause_not_the_normal_process(mondo: ho.Parts) -> None:
    mine = {c.selector["relation"]: c for c in mondo.claims if c.selector["subject"] == "MONDO:9999901"}
    dis = mine["RO:0004021"]
    assert dis.cause_id == "reva_state:disruption_of:go:0003824" and dis.effect_id == "mondo:9999901"
    assert dis.cause_id != "go:0003824"
    assert dis.selector["filler"] == "GO:0003824"
    assert dis.selector["relation_label"] == "disease has basis in disruption of"
    assert dis.source_claim_id == "MONDO:9999901|RO:0004021|reva_state:disruption_of:go:0003824"
    alt = mine["RO:0004030"]
    assert alt.cause_id == "reva_state:alteration_in_structure_of:uberon:0000948"
    # subject-side: the disease causes the disruption of the process, not the process itself
    sub = mine["RO:0004024"]
    assert (sub.cause_id, sub.effect_id) == ("mondo:9999901", "reva_state:disruption_of:go:0007049")
    assert sub.relation == "has_effect"
    # an ordinary causal axiom is unchanged
    assert mine["RO:0014001"].cause_id == "ncbitaxon:1773"
    # no claim anywhere has a filler of these relations as a bare endpoint
    for c in mondo.claims:
        if ho.ALTERED_STATE.get(c.codebook_term):
            assert c.cause_id.startswith("reva_state:") or c.effect_id.startswith("reva_state:")


def test_altered_state_nodes_are_labelled_reva_made(mondo: ho.Parts) -> None:
    ents = {e.qid: e for e in mondo.entities}
    node = ents["reva_state:disruption_of:go:0003824"]
    assert node.kind == ho.STATE_KIND
    assert node.labels["en"] == "disruption of GO:0003824"
    assert node.descriptions["en"].startswith("REVA-made node, not a class of Mondo Disease Ontology")
    links = [s for s in mondo.statements if s.subject_qid == node.qid]
    assert [(s.property_pid, s.object_qid) for s in links] == [("mondo:altered_state_of", "go:0003824")]
    assert links[0].qualifiers["reva_made_node"] is True and links[0].qualifiers["source_relation"] == "RO:0004021"


def test_retraction_evidence_is_the_axiom_in_the_new_file(mondo: ho.Parts) -> None:
    present = ho.row_present(mondo.codebook["claims"])
    old = {"subject": "MONDO:9999901", "relation": "RO:0004021", "filler": "GO:0003824"}
    assert present({"selector": old}) is True  # still in the file: reclassified_by_reva
    assert present({"selector": {**old, "filler": "GO:0000001"}}) is False  # gone: removed_at_source
    assert present({"selector": {}}) is None


def test_new_predicates_registered_and_families_valid() -> None:
    from reva.kg.families import load_registry, validate

    preds = {
        p["pid"]: p for p in yaml.safe_load((REPO / "registry/kg/predicates_health.yaml").read_text())["predicates"]
    }
    for pid in ("doid:inheritance_mode", "doid:has_disease_driver", "mondo:altered_state_of", cg.PID_NEGATIVE):
        assert pid in preds, pid
    validate()
    reg = load_registry()
    assert reg.edge_family(cg.PID_NEGATIVE, "out") is None  # never a family hop
    assert reg.edge_family("doid:has_disease_driver", "out") is None  # neither causal nor traversed (M9)
    for pid in ("doid:inheritance_mode", "mondo:altered_state_of"):
        ef = reg.edge_family(pid, "out")
        assert ef is not None and ef.family == "context", pid


# ------------------------------------------------------------------------------------------------ H2 (ClinGen)
def _rows() -> list[dict[str, Any]]:
    _, rows = cg.parse(_fx("atlas.clingen", "gene_validity.csv"))
    return rows


def test_clingen_negative_classes_are_not_associations() -> None:
    rows = _rows()
    neg = [dict(rows[0], CLASSIFICATION=c) for c in sorted(cg.NEGATIVE)]
    for i, r in enumerate(neg):  # synthetic variants of a fixture row (test input): distinct assertion ids
        r["ONLINE REPORT"] = r["ONLINE REPORT"].replace("CGGV:assertion_", f"CGGV:assertion_neg{i}")
    sts, rep = cg.build(rows + neg, raw_sha256="e" * 64, revision="FILE CREATED test")
    by = {s.statement_id: s for s in sts}
    negs = [s for s in sts if s.property_pid == cg.PID_NEGATIVE]
    assert len(negs) == 3 and {s.qualifiers["classification"] for s in negs} == set(cg.NEGATIVE)
    assert all(s.statement_id.endswith("#curation_negative") for s in negs)
    assert all(s.qualifiers["classification"] in cg.SUPPORTING for s in sts if s.property_pid == cg.PID)
    assert by[next(iter(by))].subject_qid.startswith("hgnc:")
    assert rep["by_predicate"] == {cg.PID: len(rows), cg.PID_NEGATIVE: 3}


def test_clingen_unreviewed_classification_blocks_the_load() -> None:
    rows = [dict(_rows()[0], CLASSIFICATION="Supportive")]
    with pytest.raises(HealthSourceError):
        cg.build(rows, raw_sha256="e" * 64, revision="x")


# ------------------------------------------------------------------------------------------------ L8
def test_carbon_brief_event_ids_say_they_are_reva_made() -> None:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids
    from reva.evidence.runner import FixtureHttp

    http = FixtureHttp(at.NAME)
    rows = at.parse_csv(asyncio.run(http.get(at.CSV_URL)).body, min_rows=10)
    f = SimpleNamespace(sha256="c" * 64, retrieved_at=T)
    p = SimpleNamespace(sha256="d" * 64, retrieved_at=T)
    ents, sts, _ = at.kg_items(rows, f, p, {"last_updated": "19 March 2026"}, load_country_qids())
    events = {e.qid for e in ents if e.qid.startswith(("cbattr:extreme:", "cbattr:trend:"))}
    rules = {s.subject_qid: s for s in sts if s.property_pid == "cbattr:identity_rule"}
    assert events and set(rules) == events
    s = next(iter(rules.values()))
    assert s.value["value"] == at.EVENT_ID_RULE and s.value["value"].startswith("REVA-made identifier")
    assert s.qualifiers["reva_made_identifier"] is True
    assert "cbattr:identity_rule" in at.KG_PREDICATES


def test_resolve_realm_ids_say_they_are_reva_made() -> None:
    ecos = [
        eco.Ecoregion(1, "Adelie Land tundra", 11, "Tundra", "Antarctica", 1, "Half Protected", "AN11", "0.1"),
        eco.Ecoregion(2, "Admiralty Islands lowland rain forests", 1, "Tropical", "Australasia", 2, "x", "AU01", "0.2"),
    ]
    ents, sts = eco.kg_parts(ecos, raw_sha256="f" * 64, source_revision="etag:test")
    realms = {e.qid for e in ents if e.kind == "biogeographic_realm"}
    assert realms == {"resolve_realm:Antarctica", "resolve_realm:Australasia"}
    rules = [s for s in sts if s.property_pid == "resolve:identity_rule"]
    assert {s.subject_qid for s in rules} == realms
    assert all(s.value["value"] == eco.REALM_ID_RULE for s in rules)
    assert "resolve:identity_rule" in eco.PIDS
    assert eco.REALM_ID_RULE.startswith("REVA-made identifier")
