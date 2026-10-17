"""V4HEALTH offline tests on the captured fixtures: OBO / OBO-Graphs parsing, the KG builds of UBERON, DOID and Mondo,
the causal decision rule (ontology causal axioms become T1 claims, never KG links), ClinGen associations, the WHO GHE
atlas rows, licences and family mappings. No database, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_clingen as cg
from reva.adapters.connectors import atlas_health_onto as ho
from reva.adapters.connectors import atlas_who_ghe as gh
from reva.adapters.connectors.health_obo_common import (
    HealthSourceError,
    RelationIndex,
    curie,
    kg_id,
    load_predicates,
    parse_obo,
)
from reva.kernel.errors import ValidationProblem
from reva.ports.connectors import FetchItem

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
REPO = Path(__file__).resolve().parents[2]


def _fx(source: str, name: str) -> bytes:
    return (FIX / source / name).read_bytes()


@pytest.fixture(scope="module")
def ro() -> RelationIndex:
    ri, rev = ho.ro_index(_fx("atlas.doid", "ro.obo"), raw_sha256="a" * 64)
    assert rev.startswith("releases/")
    return ri


@pytest.fixture(scope="module")
def doid(ro: RelationIndex) -> ho.Parts:
    import copy

    parts, rev = ho.doid_kg(_fx("atlas.doid", "doid.json"), raw_sha256="b" * 64, ri=copy.deepcopy(ro))
    assert rev.endswith("doid/releases/2026-09-30/doid.owl")
    return parts


@pytest.fixture(scope="module")
def mondo() -> ho.Parts:
    ri, _ = ho.ro_index(_fx("atlas.mondo", "ro.obo"), raw_sha256="a" * 64)
    parts, rev = ho.mondo_kg(_fx("atlas.mondo", "mondo.obo"), raw_sha256="c" * 64, ri=ri)
    assert rev == "releases/2026-10-06"
    return parts


def _st(parts: ho.Parts, subject: str, pid: str) -> list[Any]:
    return [s for s in parts.statements if s.subject_qid == subject and s.property_pid == pid]


# ------------------------------------------------------------------------------------------- identifiers / OBO
def test_curie_and_kg_ids_are_the_published_identifiers() -> None:
    assert curie("http://purl.obolibrary.org/obo/NCBITaxon_1773") == "NCBITaxon:1773"
    assert curie("http://identifiers.org/hgnc/4851") == "HGNC:4851"
    assert curie("http://identifiers.org/ncbigene/3064") == "NCBIGene:3064"
    assert curie("UBERON:0000948") == "UBERON:0000948"
    assert curie("heart") is None and curie("http://example.org/x") is None
    assert kg_id("NCBITaxon:1773") == "ncbitaxon:1773" and kg_id("DOID:399") == "doid:399"


def test_obo_parser_reads_quoted_values_qualifiers_and_comments() -> None:
    body = (
        b"format-version: 1.2\ndata-version: x/1\n\n[Term]\nid: X:1\nname: a ! not a comment\n"
        b'def: "say \\"hi\\" ! here" [PMID:1]\nsynonym: "b" EXACT []\nsynonym: "c" RELATED []\n'
        b'relationship: part_of X:2 {source="MA"} ! label\n'
    )
    header, sts = parse_obo(body, what="t")
    assert header["data-version"] == ["x/1"]
    t = sts[0]
    assert t.first("def") == 'say "hi" ! here'
    assert t.tagged("relationship") == [("part_of X:2", 'source="MA"')]
    from reva.adapters.connectors.health_obo_common import exact_synonyms

    assert exact_synonyms(t) == ["b"]
    with pytest.raises(HealthSourceError):
        parse_obo(b"[Term]\nid: X:1\n", what="no header")


# ------------------------------------------------------------------------------------------- relations / causal rule
def test_causal_rule_follows_the_relation_ontology(ro: RelationIndex) -> None:
    ro_m, _ = ho.ro_index(_fx("atlas.mondo", "ro.obo"), raw_sha256="a" * 64)
    assert ro_m.is_causal("RO:0014001")  # disease has infectious agent
    assert ro.is_causal("RO:0004019")  # disease has basis in
    assert ro.is_causal("RO:0004005")  # has major susceptibility factor
    for plain in ("RO:0004026", "RO:0002452", "RO:0002200", "RO:0002451", "RO:0002488", "RO:0004029"):
        assert not ro.is_causal(plain), plain
    own = ro_m.definition("RO:0014001")
    assert own is not None and own[0].startswith('"A relationship between a disease and an infectious agent')
    assert own[1]["document_key"] == "ro.obo" and own[1]["paragraph"] == "[Typedef] id: RO:0014001"
    inherited = ro.definition("RO:0004019")
    assert inherited is not None and inherited[0].startswith("[no definition is given for RO:0004019")
    assert '"relation that links two events, processes, states, or objects' in inherited[0]


def test_no_kg_predicate_is_causal_and_every_family_mapping_is_registered() -> None:
    from reva.kg.families import load_registry, validate

    preds = load_predicates()
    markers = ("cause", "causal", "effect", "material_basis", "risk_factor", "induces", "leads_to")
    assert not [p.pid for p in preds if any(m in p.pid or m in p.reva_predicate for m in markers)]
    doc = yaml.safe_load((REPO / "registry/kg/relations_v4.d/v4health.yaml").read_text())
    pids = {p.pid for p in preds}
    mapped = {m["property"] for m in doc["mappings"]}
    assert {p for p in mapped if not p.startswith("P")} <= pids
    requested = {
        r["property"] for r in yaml.safe_load((REPO / "registry/kg/property_requests/v4health.yaml").read_text())
    }
    assert {p for p in mapped if p.startswith("P")} <= requested
    validate()
    reg = load_registry()
    for pid, fam in (
        ("doid:has_location", "located_in"),
        ("clingen:gene_disease_validity", "associated_with"),
        ("uberon:part_of", "part_of"),
    ):
        ef = reg.edge_family(pid, "out")
        assert ef is not None and ef.family == fam, pid


# ------------------------------------------------------------------------------------------- UBERON
def test_uberon_human_view_anatomy() -> None:
    parts = ho.uberon_kg(_fx("atlas.uberon", "human-view.obo"), raw_sha256="d" * 64)
    ents = {e.qid: e for e in parts.entities}
    heart = ents["uberon:0000948"]
    assert heart.kind == "anatomical_structure" and heart.labels == {"en": "heart"}
    assert "chambered heart" in heart.aliases["en"]
    assert ents["cl:0000540"].kind == "cell_type"
    part_of = {s.object_qid for s in _st(parts, "uberon:0002048", "uberon:part_of")}  # lung
    assert "uberon:0000170" in part_of  # pair of lungs
    defs = _st(parts, "uberon:0000948", "uberon:definition")
    assert defs and defs[0].value["value"].startswith("A myogenic muscular circulatory organ")
    xr = {s.value["value"] for s in _st(parts, "uberon:0000948", "uberon:xref")}
    assert "FMA:7088" in xr and not any(x.startswith("ZFA:") for x in xr)
    s = _st(parts, "uberon:0000948", "uberon:subclass_of")[0]
    assert s.references[0]["selector"]["term"] == "UBERON:0000948" and s.raw_sha256 == "d" * 64
    assert parts.report["revision"] == "uberon/releases/2026-10-01/subsets/human-view.owl"


def test_uberon_licence_drift_blocks() -> None:
    body = _fx("atlas.uberon", "human-view.obo").replace(b"licenses/by/3.0/", b"licenses/by-nc/3.0/")
    with pytest.raises(HealthSourceError) as ei:
        ho.uberon_kg(body, raw_sha256="d" * 64)
    assert ei.value.details["kind"] == "licence"


# ------------------------------------------------------------------------------------------- DOID
def test_doid_disease_location_symptoms_and_xrefs(doid: ho.Parts) -> None:
    ents = {e.qid: e for e in doid.entities}
    assert ents["doid:399"].labels["en"] == "tuberculosis" and ents["doid:399"].kind == "disease"
    assert ents["ncbitaxon:1773"].labels["en"] == "Mycobacterium tuberculosis"
    assert ents["ncbitaxon:1773"].kind == "taxon"
    assert "uberon:0002048" not in ents  # UBERON entities are created by atlas.uberon only
    loc = _st(doid, "doid:0060570", "doid:has_location")  # cardiac tuberculosis -> heart
    assert [s.object_qid for s in loc] == ["uberon:0000948"]
    assert loc[0].qualifiers["axioms"] == ["equivalent_class"]
    malaria_symptoms = {s.object_qid for s in _st(doid, "doid:12365", "doid:has_symptom")}
    assert "symp:0019174" in malaria_symptoms  # chills
    assert ents["symp:0019174"].labels["en"] == "chills"
    xr = {s.value["value"] for s in _st(doid, "doid:12365", "doid:xref")}
    assert any(x.startswith("ICD10CM:") for x in xr)
    up = ho.chain_up(doid, "doid:399", "doid:subclass_of")
    assert up[0] == "doid:399" and up[-1] == "doid:4"  # ... -> disease


def test_doid_causal_axioms_are_claims_never_statements(doid: ho.Parts) -> None:
    pids = {s.property_pid for s in doid.statements}
    assert not any("material" in p or "basis" in p or "trigger" in p for p in pids)
    tb = [c for c in doid.claims if c.effect_id == "doid:399"]
    assert [(c.cause_id, c.relation, c.codebook_term, c.tier) for c in tb] == [
        ("ncbitaxon:1773", "has_cause", "IDO:0000664", "T1")
    ]
    assert tb[0].selector["relation_label"] == "has material basis in"
    assert tb[0].source_claim_id == "DOID:399|IDO:0000664|NCBITaxon:1773"
    pf = [c for c in doid.claims if c.effect_id == "doid:14067"]  # Plasmodium falciparum malaria
    assert any(c.cause_id == "ncbitaxon:5833" for c in pf)
    cb = ho.codebook("Human Disease Ontology", "rev", "ro-rev", doid.codebook["claims"], ho.DOID_URL)
    assert cb["definitions"]["IDO:0000664"] == '"Relating the disease to it’s causation (disease and disorder)."'
    assert cb["sources"]["IDO:0000664"]["document_key"] == "doid.json"
    assert set(cb["definitions"]) == {c.codebook_term for c in doid.claims}
    assert len({c.source_claim_id for c in doid.claims}) == len(doid.claims)


# ------------------------------------------------------------------------------------------- MONDO
def test_mondo_equivalences_xrefs_and_claims(mondo: ho.Parts) -> None:
    eq = _st(mondo, "mondo:0018076", "mondo:equivalent_doid")
    assert [s.object_qid for s in eq] == ["doid:399"]
    xr = {s.value["value"] for s in _st(mondo, "mondo:0018076", "mondo:xref")}
    assert {"ICD10WHO:A15-A19", "icd11.foundation:2072728114"} <= xr
    assert not any(x.startswith(("MEDGEN:", "GARD:")) for x in xr)
    tb = [c for c in mondo.claims if c.effect_id == "mondo:0018076"]
    assert [(c.cause_id, c.relation, c.codebook_term) for c in tb] == [("ncbitaxon:1773", "has_cause", "RO:0014001")]
    hd = [c for c in mondo.claims if c.effect_id == "mondo:0007739"]  # Huntington disease
    assert [(c.cause_id, c.codebook_term) for c in hd] == [("hgnc:4851", "RO:0004003")]
    assert hd[0].selector["annotations"].startswith('source="OMIM:')
    assert not {s.property_pid for s in mondo.statements} & {"mondo:has_material_basis_in_germline_mutation_in"}
    assert mondo.report["causal_without_definition_not_loaded"] is not None


# ------------------------------------------------------------------------------------------- ClinGen
def test_clingen_associations_by_identifier_only() -> None:
    created, rows = cg.parse(_fx("atlas.clingen", "gene_validity.csv"))
    assert created == "2026-10-17"
    sts, rep = cg.build(rows, raw_sha256="e" * 64, revision="FILE CREATED 2026-10-17")
    htt = [s for s in sts if s.subject_qid == "hgnc:4851"]
    assert htt and all(s.object_qid.startswith("mondo:") for s in htt)
    s = htt[0]
    assert s.statement_id.startswith("CGGV:assertion_") and s.qualifiers["classification"]
    assert s.qualifiers["gene_symbol_as_written"] == "HTT" and s.property_pid == cg.PID
    assert rep["statements"] == len(sts) == len(rows)
    legacy = dict(
        rows[0], **{"ONLINE REPORT": "https://search.clinicalgenome.org/kb/gene-validity/CGGCIEX:assertion_6238"}
    )
    (st,), _ = cg.build([legacy], raw_sha256="e" * 64, revision="r")
    assert st.statement_id == "CGGCIEX:assertion_6238"
    bad = _fx("atlas.clingen", "gene_validity.csv").replace(b'"MOI"', b'"MODE"')
    with pytest.raises(HealthSourceError):
        cg.parse(bad)


# ------------------------------------------------------------------------------------------- WHO GHE
def test_ghe_rows_flags_and_missing() -> None:
    causes = gh.parse_causes(_fx("atlas.who_ghe", "ghe_causes.json"))
    assert causes[0] == ("All Causes", 0) and causes[1100] == ("Cardiovascular diseases", 2)
    rows = gh.parse_country(_fx("atlas.who_ghe", "ghe_FRA.json"), "FRA")
    out = gh.atlas_rows(rows, causes, iso3="FRA", raw_sha256="f" * 64, item_key="ghe.country:FRA")
    tot = [
        r for r in out if r["indicator"] == "ghe.deaths|sex=TOTAL|age=TOTAL|cause=0" and r["period_start"].year == 2023
    ]
    assert len(tot) == 1 and tot[0]["value"] == "634229.33" and tot[0]["unit"] == "deaths"
    flags = json.loads(tot[0]["flags"])
    assert flags["cause_title"] == "All Causes" and flags["low"] and flags["high"]
    assert flags["estimate_class"] == "who_comparable_estimate"
    rate = [r for r in out if r["indicator"].startswith("ghe.deaths_rate100k|") and r["period_start"].year == 2023]
    assert rate and "low" not in json.loads(rate[0]["flags"])
    assert tot[0]["concept_id"] == "health_body.ghe.deaths.all_causes"
    nulled = [dict(rows[0], VAL_DTHS_COUNT_NUMERIC=None)]
    miss = gh.atlas_rows(nulled, causes, iso3="FRA", raw_sha256="f" * 64, item_key="k")[0]
    assert miss["value"] is None and json.loads(miss["flags"])["missing"] == "not_reported"


def test_ghe_follow_ups_and_drift() -> None:
    conn = gh.WhoGheConnector(("FRA",))
    (years,) = conn.items()
    nxt = conn.follow_ups(years, _fx("atlas.who_ghe", "ghe_years.json"))
    assert conn.latest == 2023 and [i.key for i in nxt] == ["ghe.countries", "ghe.causes"]
    per = conn.follow_ups(nxt[0], _fx("atlas.who_ghe", "ghe_countries.json"))
    assert [i.key for i in per] == ["ghe.country:FRA"] and "DIM_YEAR_CODE%20eq%202023" in per[0].url
    with pytest.raises(HealthSourceError.__mro__[1]):
        conn.check(
            FetchItem(key="ghe.country:FRA", url="u", max_bytes=10, params={"kind": "country", "iso3": "FRA"}),
            json.dumps({"value": [{"DIM_COUNTRY_CODE": "FRA"}]}).encode(),
        )
    short = gh.WhoGheConnector(("FRA",), min_countries=150)
    short.countries = ["FRA"]
    with pytest.raises(ValidationProblem):
        short.atlas_frame({"ghe.causes": _fx("atlas.who_ghe", "ghe_causes.json")}, {})


def test_atlas_sources_are_declared() -> None:
    names = {s.name for s in ho.atlas_source()} | {cg.atlas_source().name, gh.atlas_source().name}
    assert names == {"atlas.uberon", "atlas.doid", "atlas.mondo", "atlas.clingen", "atlas.who_ghe"}
    for s in ho.atlas_source():
        assert "raw.githubusercontent.com" in s.allowed_hosts and s.planned_requests == 2
