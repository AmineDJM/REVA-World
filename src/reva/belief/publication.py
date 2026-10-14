"""Belief release publication: immutable, release-pinned, atomically visible.

Objects (estimate table Parquet, manifests) are written first; the release,
estimates, inputs, artifact references, outbox events and the publication
sequence commit in one transaction under the publication advisory lock.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, text

from reva.adapters.postgres import repositories as repo
from reva.belief.selection import METHOD_KEY, METHOD_VERSION, eligible_latest, select
from reva.kernel.context import Origin, assert_publishable
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.ontology.registry import load_registry
from reva.ports.storage import ArtifactStore

ESTIMATE_SCHEMA = pa.schema(
    [
        ("subject_id", pa.string()),
        ("concept_id", pa.string()),
        ("support_id", pa.string()),
        ("value_kind", pa.string()),
        ("value", pa.string()),
        ("unit_ref", pa.string()),
        ("uncertainty_status", pa.string()),
        ("observation_id", pa.string()),
        ("source_key", pa.string()),
    ]
)


@dataclass(frozen=True)
class ReleaseResult:
    release_id: UUID
    context_id: UUID
    sequence: int
    estimates: int
    manifest_sha256: str
    knowledge_cutoff: datetime


def _estimate_table(rows: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    rows = sorted(rows, key=lambda r: (r["concept_id"], r["subject_id"], r["support_id"]))
    table = pa.Table.from_pylist(rows, schema=ESTIMATE_SCHEMA)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", write_statistics=False)
    meta = {
        "arrow_schema_fingerprint": logical_hash(str(ESTIMATE_SCHEMA)),
        "sorted_by": ["concept_id", "subject_id", "support_id"],
        "row_count": table.num_rows,
        "logical_sha256": logical_hash(rows),
        "byte_hash_note": "Parquet bytes are pinned separately and are not promised identical across library versions",
    }
    return buf.getvalue(), meta


def publish_release(
    engine: Engine,
    store: ArtifactStore,
    *,
    cutoff: datetime,
    origin: Origin,
    environment: str,
    actor: str = "worker:belief",
    concepts: list[str] | None = None,
) -> ReleaseResult:
    assert_publishable(origin, environment)
    reg = load_registry()
    preferences = reg.policies["selection"]["preferences"]
    origins = [origin.value]
    with engine.connect() as conn:
        rows = eligible_latest(conn, cutoff, origins, concepts)
    selections = select(rows, preferences)
    if not selections:
        raise ValidationProblem("no eligible observations at this cutoff; nothing to publish")

    table_rows = []
    for s in selections:
        table_rows.append(
            {
                "subject_id": str(s.subject_id),
                "concept_id": s.concept_id,
                "support_id": str(s.support_id),
                "value_kind": s.value.get("kind"),
                "value": s.value.get("value") if s.value.get("kind") == "decimal" else None,
                "unit_ref": s.value.get("unit_ref"),
                "uncertainty_status": s.uncertainty.get("status"),
                "observation_id": str(s.observation_id),
                "source_key": s.source_key,
            }
        )
    parquet_bytes, table_meta = _estimate_table(table_rows)
    pq_obj = store.put_bytes(parquet_bytes, "belief", "application/vnd.apache.parquet")  # objects first

    with engine.begin() as conn:
        act = repo.simple_activity(
            "belief.publication", "belief", actor,
            {"method": f"{METHOD_KEY}@{METHOD_VERSION}", "cutoff": cutoff.isoformat(), "origin": origin.value,
             "policy": reg.policies["selection"]["policy_version"]},
        )
        repo.register_artifact(conn, pq_obj, schema_version="belief-estimates/1",
                               rights={"licence": "derived from source licences; see inputs"},
                               logical_sha256=table_meta["logical_sha256"])
        onto_sha = repo.store_json_artifact(conn, store, reg.manifest(), "belief", schema_version="ontology-manifest/1")
        method = conn.execute(
            text("SELECT id, spec_sha256 FROM model.version WHERE model_key=:k AND version=:v"),
            {"k": METHOD_KEY, "v": METHOD_VERSION},
        ).mappings().one()
        model_manifest = {"schema": "reva-model-manifest/1",
                          "models": [{"model_key": METHOD_KEY, "version": METHOD_VERSION,
                                      "spec_sha256": method["spec_sha256"]}]}
        model_sha = repo.store_json_artifact(conn, store, model_manifest, "belief", schema_version="model-manifest/1")
        input_obs = sorted({str(s.observation_id) for s in selections})
        temporal = conn.execute(
            text("SELECT min(lower(valid_range)), max(upper(valid_range)) FROM world.support WHERE id = ANY(:ids)"),
            {"ids": list({s.support_id for s in selections})},
        ).one()
        manifest = {
            "schema": "reva-belief-release/1",
            "method": f"{METHOD_KEY}@{METHOD_VERSION}",
            "knowledge_cutoff": cutoff,
            "origin": origin.value,
            "selection_policy": reg.policies["selection"]["policy_version"],
            "estimates_artifact": {"sha256": pq_obj.sha256, **table_meta},
            "ontology_manifest_sha256": onto_sha,
            "model_manifest_sha256": model_sha,
            "input_observation_ids": input_obs,
            "temporal_coverage": [str(temporal[0]), str(temporal[1])],
            "source_logical_keys_count": len(input_obs),
        }
        manifest_sha = repo.store_json_artifact(conn, store, manifest, "belief", schema_version="belief-release/1")
        repo.record_activity(conn, act, inputs=[], outputs=[pq_obj.sha256, onto_sha, model_sha, manifest_sha])
        ctx = repo.create_context(conn, mode="belief", origin=origin.value, manifest_sha256=manifest_sha,
                                  knowledge_cutoff=cutoff)
        rid = conn.execute(
            text(
                """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                     model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
                   VALUES (:c, :o, :m, :om, :mm, :cut, :meth, :a) RETURNING id"""
            ),
            {"c": ctx, "o": origin.value, "m": manifest_sha, "om": onto_sha, "mm": model_sha, "cut": cutoff,
             "meth": f"{METHOD_KEY}@{METHOD_VERSION}", "a": act.id},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO belief.release_artifact(release_id, artifact_sha256, role) VALUES (:r, :s, 'estimates')"),
            {"r": rid, "s": pq_obj.sha256},
        )
        for s in selections:
            eid = conn.execute(
                text(
                    """INSERT INTO belief.estimate(release_id, subject_id, concept_version_id, support_id,
                         method_version_id, modality, value, uncertainty, alternatives, discrepancy_flags,
                         provenance_activity_id)
                       VALUES (:r, :s, :c, :sp, :m, 'selected_report', CAST(:v AS jsonb), CAST(:u AS jsonb),
                               CAST(:alt AS jsonb), CAST(:df AS jsonb), :a) RETURNING id"""
                ),
                {"r": rid, "s": s.subject_id, "c": s.concept_version_id, "sp": s.support_id, "m": method["id"],
                 "v": json.dumps(s.value), "u": json.dumps(s.uncertainty), "alt": json.dumps(s.alternatives,
                                                                                             default=str),
                 "df": json.dumps(s.discrepancy_flags), "a": act.id},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO belief.input(estimate_id, observation_id) VALUES (:e, :o)"),
                {"e": eid, "o": s.observation_id},
            )
        repo.take_publication_lock(conn)
        seq = conn.execute(
            text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a) RETURNING sequence"),
            {"r": rid, "a": act.id},
        ).scalar_one()
        repo.emit_outbox(conn, "belief.release_published",
                         {"release_id": str(rid), "sequence": seq, "cutoff": cutoff.isoformat()})
        conn.execute(text("DELETE FROM ops.dirty_node WHERE node_key LIKE 'belief:%' AND since_sequence <= "
                          "(SELECT COALESCE(max(id),0) FROM ops.outbox)"))
    return ReleaseResult(UUID(str(rid)), ctx, int(seq), len(selections), manifest_sha, cutoff)
