"""V4HEALTH end to end on a throwaway database: UBERON, DOID, Mondo, ClinGen and WHO GHE in fixture mode through the
atlas platform (``run_atlas_ingest``; no network). KG loads with provenance, causal ontology axioms as T1 claims
(never KG links), ClinGen associations, the GHE atlas table, idempotence, and the paths the layer exists for:
tuberculosis -> Mycobacterium tuberculosis (claim), Mondo tuberculosis -> DOID tuberculosis (equivalence), cardiac
tuberculosis -> heart (located_in) -> ... UBERON anatomy, Huntington disease <- HTT (claim + ClinGen association).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import causdata_common as cd
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db
SOURCES = ("atlas.uberon", "atlas.doid", "atlas.mondo", "atlas.clingen", "atlas.who_ghe")


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


def _one(db: Any, sql: str, **kw: Any) -> Any:
    with db.owner.connect() as c:
        return c.execute(text(sql), kw).all()


async def test_health_layer_fixture_ingest(db: Any) -> None:
    out = {n: await _run(db, n) for n in SOURCES}
    for n in ("atlas.uberon", "atlas.doid", "atlas.mondo", "atlas.clingen"):
        kg = out[n]["summary"]["kg"]
        assert not kg["rejected_counts"], (n, kg["rejected_sample"])
        assert kg["statements_inserted"] > 0, n
    # anatomy: lung part_of pair of lungs, with the UBERON file as raw artifact
    rows = _one(
        db,
        """SELECT object_qid, raw_sha256, source_key, source_revision FROM kg.statement_current
           WHERE subject_qid = 'uberon:0002048' AND property_pid = 'uberon:part_of'""",
    )
    assert {r.object_qid for r in rows} >= {"uberon:0000170"}
    assert rows[0].source_key == "uberon.obo" and rows[0].raw_sha256 == out["atlas.uberon"]["summary"]["raw"]["file"]
    assert rows[0].source_revision == "uberon/releases/2026-10-01/subsets/human-view.owl"
    # disease -> anatomy, disease -> disease (Mondo equivalence by Mondo's own xref)
    assert _one(
        db,
        "SELECT 1 FROM kg.statement_current WHERE subject_qid='doid:0060570' AND property_pid='doid:has_location' "
        "AND object_qid='uberon:0000948'",
    )
    assert _one(
        db,
        "SELECT 1 FROM kg.statement_current WHERE subject_qid='mondo:0018076' "
        "AND property_pid='mondo:equivalent_doid' AND object_qid='doid:399'",
    )
    # no causal axiom is a KG statement
    assert not _one(
        db,
        "SELECT 1 FROM kg.statement_current WHERE source_key IN ('doid.obo','mondo.obo') "
        "AND object_qid LIKE 'ncbitaxon:%' AND property_pid NOT IN ('doid:subclass_of','doid:transmitted_by',"
        "'mondo:in_taxon','mondo:transmitted_by')",
    )
    # ClinGen: HTT -> Huntington disease, association with its classification
    cl = _one(
        db,
        """SELECT object_qid, qualifiers FROM kg.statement_current
           WHERE subject_qid = 'hgnc:4851' AND property_pid = 'clingen:gene_disease_validity'""",
    )
    assert cl and all(r.object_qid.startswith("mondo:") for r in cl) and cl[0].qualifiers["classification"]
    # causal claims (T1, codebook quoted) when CAUSCORE is present
    for n, effect, cause in (("atlas.doid", "doid:399", "ncbitaxon:1773"), ("atlas.mondo", "mondo:0007739", "hgnc:4851")):
        res = out[n]["summary"]["causal"]
        ds = "doid" if n == "atlas.doid" else "mondo"
        if not cd.causal_available(db.worker):
            assert res[ds]["status"] == "causal_unavailable"
            continue
        assert res[ds]["status"] == "loaded", res
        claim = _one(
            db,
            """SELECT relation, source_tier, codebook, source_key FROM kg.causal_claim_current
               WHERE effect_id = :e AND cause_id = :c""",
            e=effect,
            c=cause,
        )
        assert len(claim) == 1 and claim[0].source_tier == "T1", claim
        assert claim[0].codebook["definition"].startswith(("\"", "[no definition"))
        assert claim[0].codebook.get("quote_source", {}).get("sha256")
    # GHE atlas table
    ghe = out["atlas.who_ghe"]["summary"]["atlas"]
    assert ghe["dataset_key"] == "atlas.who.ghe" and ghe["unmapped_provider_geo"] == []
    frame = read_atlas_table(db.store, ghe["artifact_sha256"])
    assert frame.height == ghe["row_count"] > 0
    assert set(frame["provider_geo"].unique().to_list()) == {"FRA", "NGA"}
    # idempotent re-runs
    again = await _run(db, "atlas.doid")
    assert again["summary"]["kg"]["statements_inserted"] == 0
    assert again["summary"]["kg"]["retractions_inserted"] == 0
    g2 = await _run(db, "atlas.who_ghe")
    assert g2["summary"]["atlas"]["created"] is False
