"""V6DRUGS medicines layer, offline: parsers and builders of ChEMBL, UniChem, WHO eEML, openFDA and ClinicalTrials.gov
on the captured fixtures (tests/fixtures/source/atlas.{chembl_drugs,unichem,who_eml,openfda,ctgov}); no network, no DB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_drugs_chembl as ch
from reva.adapters.connectors import atlas_drugs_ctgov as ct
from reva.adapters.connectors import atlas_drugs_eml as eml
from reva.adapters.connectors import atlas_drugs_openfda as fda
from reva.adapters.connectors import atlas_drugs_unichem as uc
from reva.adapters.connectors import drugs_common as dc

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"


def body(source: str, url_part: str, *, exact: bool = False) -> bytes:
    man = json.loads((FIX / source / "manifest.json").read_text())
    hits = [e for e in man if (e["url"] == url_part if exact else url_part in e["url"])]
    assert len(hits) == 1, (url_part, [e["url"] for e in hits])
    return (FIX / source / hits[0]["file"]).read_bytes()


def rows_of(source: str, url_part: str, key: str) -> list[dict[str, Any]]:
    rows, total, _ = ch.parse_page(body(source, url_part), key, key)
    assert len(rows) == total
    return rows


# ============================================================================================== ChEMBL
def _chembl_build() -> tuple[list[Any], list[Any], dict[str, Any]]:
    src = "atlas.chembl_drugs"
    release, mapping = ch.parse_mapping(body(src, "chembl_uniprot_mapping"))
    assert release == "chembl_37"
    return ch.build(
        release="ChEMBL_37",
        molecules=rows_of(src, "molecule.json", "molecules"),
        mechanisms=rows_of(src, "mechanism.json", "mechanisms"),
        targets=rows_of(src, "target.json", "targets"),
        indications=rows_of(src, "drug_indication.json", "drug_indications"),
        mapping=mapping,
        raws={k: "a" * 64 for k in ("molecules", "mechanisms", "targets", "indications", "mapping")},
    )


def test_chembl_drugs_mechanisms_targets_indications() -> None:
    ents, sts, rep = _chembl_build()
    kinds = {e.qid: e.kind for e in ents}
    assert kinds["chembl:CHEMBL941"] == "drug" and kinds["chembl:CHEMBL1862"] == "drug_target"
    assert {e.labels.get("en") for e in ents if e.qid == "chembl:CHEMBL941"} == {"IMATINIB"}
    by = {(s.subject_qid, s.property_pid, s.object_qid) for s in sts if s.object_qid}
    # imatinib mesylate (the approved form ChEMBL attaches mechanisms to) -> ABL1 -> UniProt P00519 (release mapping)
    assert ("chembl:CHEMBL1642", "chembl:mechanism_target", "chembl:CHEMBL1862") in by
    assert ("chembl:CHEMBL1862", "chembl:target_component", "uniprot:P00519") in by
    assert ("chembl:CHEMBL1862", "chembl:target_organism", "ncbitaxon:9606") in by
    mech = next(s for s in sts if s.property_pid == "chembl:mechanism_target" and s.object_qid == "chembl:CHEMBL1862")
    assert mech.qualifiers["action_type"] == "INHIBITOR" and mech.statement_id.startswith("mec:")
    assert mech.qualifiers["parent_molecule_chembl_id"] == "CHEMBL941"
    assert "not a causal claim" in mech.qualifiers["note"]
    # salt -> parent (ChEMBL hierarchy), never by name
    assert ("chembl:CHEMBL1642", "chembl:parent_molecule", "chembl:CHEMBL941") in by
    # indications: association with the phase, EFO/MONDO ids as published, MeSH alongside
    ind = [s for s in sts if s.property_pid == "chembl:indication"]
    assert ind and all(s.qualifiers["max_phase_for_ind"] in ("4.0", 4, "4") for s in ind)
    assert any(s.object_qid.startswith("mondo:") for s in ind) and any(s.object_qid.startswith("efo:") for s in ind)
    assert not any(e.qid.startswith(("mondo:", "doid:")) for e in ents)  # V4HEALTH's entities are not re-created
    assert rep["indication_mesh"] == rep["indications"]
    # ATC codes carry the WHOCC non-commercial flag
    atc = [s for s in sts if s.property_pid == "chembl:atc_code"]
    assert atc and all(s.qualifiers["licence_noncommercial"] is True for s in atc)
    assert all(s.source_revision == "ChEMBL_37" for s in sts)


def test_chembl_term_ids_and_mapping_guards() -> None:
    assert ch.term_qid("MONDO:0005148") == "mondo:0005148"
    assert ch.term_qid("EFO:0000685") == "efo:0000685"
    assert ch.term_qid("Orphanet:558") == "orphanet:558"
    assert ch.term_qid("diabetes") is None and ch.term_qid(None) is None
    with pytest.raises(dc.DrugsSourceError):
        ch.parse_mapping(b"P00519\tCHEMBL1862\tABL1\tSINGLE PROTEIN\n")
    rel, rows = ch.parse_mapping(
        b"# chembl_37 target list, 01/05/2026\nENSG00000207827\tCHEMBL1615321\tmiR\tNUCLEIC-ACID\n"
    )
    assert rel == "chembl_37" and rows[0][1].startswith("ENSG")
    _, sts, _ = ch.build(
        release="ChEMBL_37",
        molecules=[],
        mechanisms=[{"molecule_chembl_id": "CHEMBL1", "target_chembl_id": "CHEMBL1615321", "mec_id": 1}],
        targets=[],
        indications=[],
        mapping=rows,
        raws={k: "a" * 64 for k in ("molecules", "mechanisms", "targets", "indications", "mapping")},
    )
    assert not [s for s in sts if s.property_pid == "chembl:target_component"]  # Ensembl id is not a UniProt entry


# ============================================================================================== UniChem
def test_unichem_crosswalk_scope_and_entities() -> None:
    src = "atlas.unichem"
    rows = {s: uc.parse(body(src, f"src1src{s}.txt.gz"), s) for s in uc.PAIRS}
    drugs = {"CHEMBL25", "CHEMBL941", "CHEMBL1642", "CHEMBL1431", "CHEMBL1201823", "CHEMBL3"}
    ents, sts, rep = uc.build(rows, {s: "b" * 64 for s in uc.PAIRS}, drugs, "rev")
    by = {(s.subject_qid, s.property_pid, s.object_qid or s.value["value"]) for s in sts}
    assert ("chembl:CHEMBL941", "unichem:chebi", "chebi:45783") in by
    assert ("chembl:CHEMBL941", "unichem:drugbank_id", "DB00619") in by
    assert ("chembl:CHEMBL1642", "unichem:fda_unii", "unii:8A1O1M485B") in by
    assert not any(s.subject_qid == "chembl:CHEMBL3833402" for s in sts)  # not an approved drug in the KG
    assert rep["ChEBI"]["skipped"]["not_an_approved_drug_in_kg"] == 1
    assert {e.kind for e in ents} == {"drug_substance"}
    with pytest.raises(dc.DrugsSourceError):
        uc.parse(b"not gzip", 7)


# ============================================================================================== eEML
def test_eml_pages_and_build() -> None:
    src = "atlas.who_eml"
    listed, stated = eml.parse_list(body(src, "https://list.essentialmeds.org/", exact=True))
    assert dict(listed)["95"] == "Imatinib" and len(listed) == 5 and stated["medicines"] > 600
    atropine = eml.parse_medicine(body(src, "/medicines/10"), "10")
    assert atropine.atc == ["A03BA01", "S01FA01"] and atropine.drugbank == "DB00572"
    assert "298" in atropine.recommendations and atropine.history[0]["year"] == 1977
    assert atropine.history[0]["trs"] == "615"
    rec = eml.parse_recommendation(body(src, "/recommendations/298"), "298")
    assert rec.medicine_id == "10" and rec.icd_code == "9A96.Z"
    assert rec.icd_uri == "http://id.who.int/icd/entity/908233081/mms/unspecified"
    assert [s for s, _ in rec.sections] == ["439", "444"] and "EMLc" in rec.badges
    assert set(rec.alternatives) >= {"651", "512"}
    imat = eml.parse_medicine(body(src, "/medicines/95"), "95")
    recs = [eml.parse_recommendation(body(src, f"/recommendations/{r}"), r) for r in imat.recommendations]
    raws = {f"medicine:{imat.id}": "c" * 64, **{f"recommendation:{r.id}": "c" * 64 for r in recs}}
    ents, sts, rep = eml.build(
        [imat], recs, raws, revision="eEML test", drugbank_to_chembl={"DB00619": ["chembl:CHEMBL941"]}
    )
    by = {(s.subject_qid, s.property_pid, s.object_qid) for s in sts if s.object_qid}
    assert ("eml:medicine:95", "eml:chembl_molecule", "chembl:CHEMBL941") in by
    assert any(p == "eml:recommended_for" and o.startswith("icd11mms:") for _, p, o in by)
    assert ("eml:medicine:95", "eml:on_list", eml.LIST_EML) in by
    assert all(s.qualifiers.get("licence_noncommercial") for s in sts if s.property_pid == "eml:atc_code")
    # ambiguous DrugBank bridge -> no link (counted)
    _, sts2, rep2 = eml.build(
        [imat],
        recs,
        raws,
        revision="eEML test",
        drugbank_to_chembl={"DB00619": ["chembl:CHEMBL941", "chembl:CHEMBL1642"]},
    )
    assert not [s for s in sts2 if s.property_pid == "eml:chembl_molecule"]
    assert rep2["drugbank_maps_to_several_chembl_ids"] == 1


def test_eml_drift_is_refused() -> None:
    with pytest.raises(dc.DrugsSourceError):
        eml.parse_list(b"<html><body><ol></ol></body></html>")
    with pytest.raises(dc.DrugsSourceError):
        eml.parse_medicine(b"<html><div class='medicine-title'>X</div></html>", "1")


# ============================================================================================== openFDA
def test_openfda_counts_reports_not_causation() -> None:
    src = "atlas.openfda"
    res, meta = fda.parse_count(body(src, "receivedate:%5B20240101"), "country 2024")
    assert res and meta["last_updated"]
    rows = fda.country_rows(res, year=2024, last_updated=meta["last_updated"], raw="d" * 64, url="u")
    assert all(json.loads(r["flags"])["reports_not_causation"] for r in rows)
    assert all(int(r["value"]) > 0 for r in rows)
    part = fda.country_rows(res[:1], year=2026, last_updated="2026-07-30", raw="d" * 64, url="u")
    assert str(part[0]["period_end"]) == "2026-07-31" and "partial_period" in json.loads(part[0]["flags"])
    nf, _ = fda.parse_count(body(src, "BKJ8M8G5HI"), "imatinib serious")
    assert nf is None  # NOT_FOUND: no statement, never a zero
    top = fda.parse_count(body(src, "patient.drug.openfda.unii.exact"), "top")
    lab = fda.parse_count(body(src, "label.json"), "labels")
    rec = fda.parse_count(body(src, "enforcement.json?count=openfda.unii.exact"), "recalls")
    ser = fda.parse_count(body(src, "8A1O1M485B"), "imatinib mesylate serious")
    assert ser[0] is not None
    ents, sts, rep = fda.build_kg(
        top_events=(top[0] or [], top[1], "e" * 64),
        labels=(lab[0] or [], lab[1], "e" * 64),
        recalls=(rec[0] or [], rec[1], "e" * 64),
        serious={"8A1O1M485B": (ser[0], ser[1], "e" * 64)},
        revision="r",
    )
    faers = [s for s in sts if s.property_pid == "openfda:faers_reports"]
    assert faers and all(s.qualifiers["caveat"] == fda.FDA_CAVEAT for s in faers)
    sev = {s.qualifiers["seriousness"] for s in faers if s.subject_qid == "unii:8A1O1M485B"}
    assert {"serious", "non_serious"} <= sev
    assert not [s for s in sts if s.subject_qid == "unii:BKJ8M8G5HI" and s.qualifiers.get("seriousness") != "all"]


# ============================================================================================== ClinicalTrials.gov
def test_ctgov_stats_counts_and_aggregates() -> None:
    src = "atlas.ctgov"
    ts = ct.parse_version(body(src, "/version"))
    stats = ct.parse_stats(body(src, "/stats/field/values"))
    assert ts.startswith("2026-") and len(stats["LocationCountry"]["values"]) > 200
    fr3 = ct.parse_total(body(src, ct.count_url(ct.country_term("France", "PHASE3")), exact=True), "fr")
    assert fr3 > 0
    rows = ct.country_rows(stats, {("France", "PHASE3"): (fr3, "f" * 64)}, ts=ts, stats_raw="f" * 64)
    assert any(r["provider_geo"] == "France" and r["indicator"].endswith("PHASE3") for r in rows)
    g = ct.global_rows(stats, ts=ts, raw="f" * 64)
    assert any(r["indicator"] == "studies.registered|field=Phase|value=__field_missing__" for r in g)
    d3 = ct.parse_total(body(src, ct.count_url(ct.condition_term("D003924", "PHASE3")), exact=True), "d")
    ents, sts, rep = ct.build_kg(stats, {("D003924", "PHASE3"): (d3, "f" * 64)}, ts=ts, stats_raw="f" * 64)
    assert {e.kind for e in ents} == {"clinical_trial"}
    assert any(s.subject_qid == "ctgov:condition_mesh:D003924" and s.object_qid == "mesh:D003924" for s in sts)
    ph = [s for s in sts if s.subject_qid == "ctgov:condition_mesh:D003924" and s.qualifiers.get("phase") == "PHASE3"]
    assert ph and ph[0].value["amount"] == str(d3)
    assert 'COVERAGE[FullMatch]"Guinea"' in ct.country_term("Guinea", "PHASE1")


def test_ctgov_country_aliases_are_exact_names() -> None:
    from reva.atlas.tables import country_iso3

    assert country_iso3("France", "ctgov_country") == "FRA"
    assert country_iso3("Turkey (Türkiye)", "ctgov_country") is None  # not an exact crosswalk name: unmapped


# ============================================================================================== registry
def test_sources_families_and_predicates() -> None:
    from reva.atlas.sources import discover
    from reva.kg.families import validate

    names = [
        f"reva.adapters.connectors.{m}"
        for m in (
            "atlas_drugs_chembl",
            "atlas_drugs_unichem",
            "atlas_drugs_eml",
            "atlas_drugs_openfda",
            "atlas_drugs_ctgov",
        )
    ]
    found, errors = discover(names)
    assert not errors and set(found) == {
        "atlas.chembl_drugs",
        "atlas.unichem",
        "atlas.who_eml",
        "atlas.openfda",
        "atlas.ctgov",
    }
    probs = validate()
    assert not [p for p in probs if "v6drugs" in p], probs
    pids = {p["pid"] for p in dc.predicate_docs()}
    for p in pids:
        assert not any(m in p.split(":", 1)[1] for m in ("cause", "effect", "induce", "risk_factor")), p
    import yaml

    drop = yaml.safe_load((Path(dc.REPO_ROOT) / "registry/kg/relations_v4.d/v6drugs.yaml").read_text())
    assert {m["property"] for m in drop["mappings"]} <= pids
