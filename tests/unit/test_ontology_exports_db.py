"""PROV export loaded from the provenance tables (read-only walk)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from reva.adapters.postgres import repositories as repo
from reva.ontology.exports import activity_iri, artifact_iri, export_artifact_lineage

pytestmark = pytest.mark.db


def test_export_lineage_from_database(db) -> None:  # type: ignore[no-untyped-def]
    with db.worker.begin() as conn:
        raw = repo.store_json_artifact(conn, db.store, {"test": "ontology-export-raw"}, "raw", schema_version="t/1")
        out = repo.store_json_artifact(
            conn, db.store, {"test": "ontology-export-norm"}, "normalised", schema_version="t/1"
        )
        a1 = repo.simple_activity("test.acquire", "source", "test:agent-k")
        repo.record_activity(conn, a1, outputs=[raw])
        a2 = repo.simple_activity("test.normalise", "normalisation", "test:agent-k")
        repo.record_activity(conn, a2, inputs=[(raw, {"json_pointer": "/test"})], outputs=[out])
    with db.api.connect() as conn:
        doc = export_artifact_lineage(conn, out)
        n_rows = conn.execute(
            text("SELECT count(*) FROM meta.activity WHERE id IN (:a, :b)"), {"a": a1.id, "b": a2.id}
        ).scalar_one()
    assert n_rows == 2
    nodes = {n["@id"]: n for n in doc["@graph"]}
    assert {activity_iri(a1.id), activity_iri(a2.id), artifact_iri(raw), artifact_iri(out)} <= set(nodes)
    assert nodes[artifact_iri(out)]["prov:wasGeneratedBy"] == [{"@id": activity_iri(a2.id)}]
    assert nodes[activity_iri(a2.id)]["prov:qualifiedUsage"][0]["reva:selector"]["@value"] == {"json_pointer": "/test"}
    assert doc["reva:focus"] == {"@id": artifact_iri(out)} and doc["reva:truncated"] is False
