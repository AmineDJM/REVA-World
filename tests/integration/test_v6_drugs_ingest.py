"""V6DRUGS end to end on a throwaway database: ChEMBL -> UniChem -> WHO eEML -> openFDA -> ClinicalTrials.gov in
fixture mode through the atlas platform (``run_atlas_ingest``; no network). The medicine path the layer exists for:
eEML imatinib -> (DrugBank, UniChem) -> ChEMBL imatinib <- (parent) imatinib mesylate -> ABL1 target -> UniProt
P00519; approved indications as associations; FAERS report counts with FDA's caveat; registry counts; idempotence.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db
SOURCES = ("atlas.chembl_drugs", "atlas.unichem", "atlas.who_eml", "atlas.openfda", "atlas.ctgov")


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


def _q(db: Any, sql: str, **kw: Any) -> Any:
    with db.owner.connect() as c:
        return c.execute(text(sql), kw).all()


def _edge(db: Any, s: str, p: str, o: str) -> bool:
    return bool(
        _q(
            db,
            "SELECT 1 FROM kg.statement_current WHERE subject_qid=:s AND property_pid=:p AND object_qid=:o",
            s=s,
            p=p,
            o=o,
        )
    )


async def test_drugs_layer_fixture_ingest(db: Any) -> None:
    out = {n: await _run(db, n) for n in SOURCES}
    for n in SOURCES:
        kg = out[n]["summary"]["kg"]
        assert not kg["rejected_counts"], (n, kg["rejected_sample"])
        assert kg["statements_inserted"] > 0, n
    # the medicine path, every hop by a published identifier
    assert _edge(db, "eml:medicine:95", "eml:chembl_molecule", "chembl:CHEMBL941")
    assert _edge(db, "chembl:CHEMBL1642", "chembl:parent_molecule", "chembl:CHEMBL941")
    assert _edge(db, "chembl:CHEMBL1642", "chembl:mechanism_target", "chembl:CHEMBL1862")
    assert _edge(db, "chembl:CHEMBL1862", "chembl:target_component", "uniprot:P00519")
    assert _edge(db, "chembl:CHEMBL941", "unichem:chebi", "chebi:45783")
    assert _edge(db, "chembl:CHEMBL1642", "unichem:fda_unii", "unii:8A1O1M485B")
    # kinds
    kinds = dict(
        _q(
            db,
            "SELECT qid, kind FROM kg.entity_current WHERE qid IN "
            "('chembl:CHEMBL941','chembl:CHEMBL1862','eml:medicine:95','ctgov:condition_mesh:D003924')",
        )
    )
    assert kinds == {
        "chembl:CHEMBL941": "drug",
        "chembl:CHEMBL1862": "drug_target",
        "eml:medicine:95": "drug",
        "ctgov:condition_mesh:D003924": "clinical_trial",
    }
    # indications are associations with ChEMBL's phase, provenance to the raw page
    ind = _q(
        db,
        """SELECT object_qid, qualifiers, raw_sha256, source_revision FROM kg.statement_current
           WHERE property_pid = 'chembl:indication' AND subject_qid = 'chembl:CHEMBL1642'""",
    )
    assert ind and all(r.source_revision == "ChEMBL_37" and r.raw_sha256 for r in ind)
    assert all("not an efficacy estimate" in r.qualifiers["note"] for r in ind)
    # eEML recommendation for imatinib -> ICD-11 category; atropine EMLc badge
    assert _q(
        db,
        "SELECT 1 FROM kg.statement_current WHERE subject_qid='eml:medicine:95' AND property_pid='eml:recommended_for' "
        "AND object_qid LIKE 'icd11mms:%'",
    )
    assert _edge(db, "eml:medicine:10", "eml:on_list", "eml:list:emlc")
    # FAERS: reports with FDA's caveat; a substance without a match has no count (never zero)
    faers = _q(
        db,
        """SELECT value, qualifiers FROM kg.statement_current WHERE subject_qid = 'unii:8A1O1M485B'
           AND property_pid = 'openfda:faers_reports'""",
    )
    assert {r.qualifiers["seriousness"] for r in faers} >= {"serious", "non_serious"}
    assert all(r.qualifiers["reports_not_causation"] is True for r in faers)
    assert not _q(
        db,
        """SELECT 1 FROM kg.statement_current WHERE subject_qid = 'unii:BKJ8M8G5HI'
           AND property_pid = 'openfda:faers_reports' AND qualifiers ->> 'seriousness' <> 'all'""",
    )
    # atlas tables (registered from the fixture responses; ISO2 -> ISO3 and exact ClinicalTrials.gov names)
    fc = out["atlas.openfda"]["summary"]["faers_country"]
    assert fc["dataset_key"] == "atlas.openfda.faers_country" and fc["row_count"] > 100
    frame = read_atlas_table(db.store, fc["artifact_sha256"])
    assert "USA" in set(frame["iso3"].drop_nulls().to_list())
    cc = out["atlas.ctgov"]["summary"]["studies_by_country"]
    frame = read_atlas_table(db.store, cc["artifact_sha256"])
    fr = frame.filter((frame["iso3"] == "FRA") & (frame["indicator"] == "studies.registered|phase=PHASE3"))
    assert fr.height == 1 and int(fr["value"][0]) > 0
    assert out["atlas.ctgov"]["summary"]["registry_counts"]["row_count"] > 10
    # idempotence: a second fixture run stores nothing new
    again = await _run(db, "atlas.chembl_drugs")
    assert again["summary"]["kg"]["statements_inserted"] == 0
