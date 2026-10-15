"""Belief release publication: immutable, release-pinned, atomically visible.

Objects (estimate table Parquet, manifests) are written first; the release,
estimates, inputs, release-pinned relations, artifact references, outbox events
and the publication sequence commit in one transaction under the publication
advisory lock.

Publication pauses (connector self-healing, :mod:`reva.autonomy.repair`): the
active ``publication_pause:belief:<concept>`` markers are read inside the
publication transaction, after the locks are granted (a pause takes the same
publication lock, so it is either visible to this release or waits for it).
Estimands of paused concepts get no new estimate: they are listed in the
manifest's ``withheld_estimands`` with reason ``source_schema_quarantine`` (and
in the publication activity parameters, ``withheld_paused``), the last accepted
release stays readable and is served as stale by the read models
(:func:`reva.world.queries.release_estimates`), and the concept's belief node is
kept dirty so the release after the pause is lifted publishes it again.
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
from sqlalchemy import Connection, Engine, text

from reva.adapters.postgres import repositories as repo
from reva.belief.selection import METHOD_KEY, METHOD_VERSION, Selection, Withheld, eligible_latest, select_report
from reva.evidence.locks import take_evidence_snapshot_lock
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
    cutoff: datetime | None = None,
    origin: Origin,
    environment: str,
    actor: str = "worker:belief",
    concepts: list[str] | None = None,
    fence: Any = None,
    lock_timeout: str = "120s",
) -> ReleaseResult:
    """Publish a source_select_v1 release.

    Snapshot consistency (R-03): the transaction first takes the evidence lock
    exclusively (every evidence writer holds it shared), so no ingest, retraction
    or eligibility decision is in flight. ``cutoff=None`` means "now": the cutoff is
    then read from the database clock *after* the lock is granted, and selection
    runs in the same transaction. An explicit cutoff must not be in the future
    (rows recorded later could still fall at/before it); past cutoffs are honoured.
    """
    assert_publishable(origin, environment)
    reg = load_registry()
    preferences = reg.policies["selection"]["preferences"]
    origins = [origin.value]

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": lock_timeout})
        take_evidence_snapshot_lock(conn)  # waits for in-flight evidence writers to commit
        repo.take_publication_lock(conn)  # serialises publication sequence allocation
        db_now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        if cutoff is None:
            cutoff = db_now
        elif cutoff > db_now:
            raise ValidationProblem(
                "knowledge cutoff is in the future; pass cutoff=None to publish as of now",
                cutoff=cutoff.isoformat(),
                now=db_now.isoformat(),
            )
        if fence is not None:
            fence(conn)  # stale lease holders cannot publish
        pauses = _active_pauses(conn)  # under the locks: a concurrent pause waits for this release
        rows = eligible_latest(conn, cutoff, origins, concepts, include_retracted=True)  # fresh snapshot
        result = select_report(rows, preferences)
        selections, paused_withheld = _withhold_paused(conn, result.selections, pauses, origin.value)
        if not selections:
            if paused_withheld:
                raise ValidationProblem(
                    "every eligible estimand belongs to a concept whose publication is paused; nothing to publish",
                    paused_concepts=sorted({w.concept_id for w in paused_withheld}),
                )
            raise ValidationProblem("no eligible observations at this cutoff; nothing to publish")
        paused_concepts = sorted({w.concept_id for w in paused_withheld})
        excluded_retracted = sorted(str(r["id"]) for r in rows if r.get("selection_status") == "retracted")

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
        # Objects before the metadata that references them; an object orphaned by a
        # rollback is harmless (GC).
        pq_obj = store.put_bytes(parquet_bytes, "belief", "application/vnd.apache.parquet")

        act_params: dict[str, Any] = {
            "method": f"{METHOD_KEY}@{METHOD_VERSION}",
            "cutoff": cutoff.isoformat(),
            "origin": origin.value,
            "policy": reg.policies["selection"]["policy_version"],
        }
        if paused_concepts:
            # Durable, role-readable record of what this release withheld (meta.activity is
            # readable by reva_api; the manifest artifact is not reachable from SQL).
            act_params["withheld_paused"] = [_pause_record(pauses[c]) for c in paused_concepts]
        act = repo.simple_activity("belief.publication", "belief", actor, act_params)
        repo.register_artifact(
            conn,
            pq_obj,
            schema_version="belief-estimates/1",
            rights={"licence": "derived from source licences; see inputs"},
            logical_sha256=table_meta["logical_sha256"],
        )
        onto_sha = repo.store_json_artifact(conn, store, reg.manifest(), "belief", schema_version="ontology-manifest/1")
        method = (
            conn.execute(
                text("SELECT id, spec_sha256 FROM model.version WHERE model_key=:k AND version=:v"),
                {"k": METHOD_KEY, "v": METHOD_VERSION},
            )
            .mappings()
            .one()
        )
        model_manifest = {
            "schema": "reva-model-manifest/1",
            "models": [{"model_key": METHOD_KEY, "version": METHOD_VERSION, "spec_sha256": method["spec_sha256"]}],
        }
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
            "excluded_retracted_lineage_heads": excluded_retracted,
            "withheld_estimands": [w.as_json() for w in result.withheld + paused_withheld],
            "temporal_coverage": [str(temporal[0]), str(temporal[1])],
            "source_logical_keys_count": len(input_obs),
        }
        if paused_concepts:
            manifest["publication_pauses"] = [_pause_record(pauses[c]) for c in paused_concepts]
        # --- V2 dataset inputs (reva.simulation.datasets, owner ENG): pinned by digest ---
        from reva.simulation.datasets import publication_hook

        dataset_inputs, pin_datasets = publication_hook(conn, cutoff=cutoff, origin=origin)
        if dataset_inputs:  # releases without datasets keep the V1 manifest unchanged
            manifest["dataset_inputs"] = dataset_inputs
        # --- end V2 dataset inputs ---
        manifest_sha = repo.store_json_artifact(conn, store, manifest, "belief", schema_version="belief-release/1")
        repo.record_activity(conn, act, inputs=[], outputs=[pq_obj.sha256, onto_sha, model_sha, manifest_sha])
        ctx = repo.create_context(
            conn, mode="belief", origin=origin.value, manifest_sha256=manifest_sha, knowledge_cutoff=cutoff
        )
        rid = conn.execute(
            text(
                """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                     model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
                   VALUES (:c, :o, :m, :om, :mm, :cut, :meth, :a) RETURNING id"""
            ),
            {
                "c": ctx,
                "o": origin.value,
                "m": manifest_sha,
                "om": onto_sha,
                "mm": model_sha,
                "cut": cutoff,
                "meth": f"{METHOD_KEY}@{METHOD_VERSION}",
                "a": act.id,
            },
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
                {
                    "r": rid,
                    "s": s.subject_id,
                    "c": s.concept_version_id,
                    "sp": s.support_id,
                    "m": method["id"],
                    "v": json.dumps(s.value),
                    "u": json.dumps(s.uncertainty),
                    "alt": json.dumps(s.alternatives, default=str),
                    "df": json.dumps(s.discrepancy_flags),
                    "a": act.id,
                },
            ).scalar_one()
            conn.execute(
                text("INSERT INTO belief.input(estimate_id, observation_id) VALUES (:e, :o)"),
                {"e": eid, "o": s.observation_id},
            )
        pin_datasets(rid)  # V2 dataset inputs (owner ENG): sealed with the release, like estimates
        pin_release_relations(conn, rid, act.id)  # before the publication row seals the release
        seq = conn.execute(
            text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a) RETURNING sequence"),
            {"r": rid, "a": act.id},
        ).scalar_one()
        repo.emit_outbox(
            conn, "belief.release_published", {"release_id": str(rid), "sequence": seq, "cutoff": cutoff.isoformat()}
        )
        _clear_dirty_nodes(conn, origin.value, concepts, keep=paused_concepts)
        _keep_paused_dirty(conn, origin.value, paused_concepts)
    return ReleaseResult(UUID(str(rid)), ctx, int(seq), len(selections), manifest_sha, cutoff)


# --------------------------------------------------------------------------- release-pinned relations
PIN_RELATIONS_SQL = """
INSERT INTO world.relation(predicate_id, layer, support_id, release_id, existence_uncertainty, strength, component,
                           provenance_activity_id)
SELECT r.predicate_id, r.layer, r.support_id, e.release_id, r.existence_uncertainty, r.strength,
       r.component || jsonb_build_object(
         'flow_magnitude', jsonb_build_object(
            'value', e.value, 'concept_id', c.concept_id, 'concept_version', c.version,
            'basis', 'selected estimate of this release (' || :method || ')'),
         'derived_from', jsonb_build_object(
            'relation_id', r.id, 'evidence_observation_id', r.evidence_observation_id, 'estimate_id', e.id)),
       :act
FROM belief.estimate e
JOIN belief.input i ON i.estimate_id = e.id AND i.observation_id IS NOT NULL
JOIN world.relation r ON r.evidence_observation_id = i.observation_id
JOIN ontology.concept_version c ON c.id = e.concept_version_id
WHERE e.release_id = :rid
ORDER BY r.id
"""

PIN_PARTICIPANTS_SQL = """
INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id, direction)
SELECT nr.id, p.ordinal, p.role_concept_id, p.entity_id, p.direction
FROM world.relation nr
JOIN world.relation_participant p ON p.relation_id = CAST(nr.component->'derived_from'->>'relation_id' AS uuid)
WHERE nr.release_id = :rid
ORDER BY nr.id, p.ordinal
"""


def pin_release_relations(conn: Connection, release_id: Any, activity_id: Any) -> int:
    """Pin the release's world view: one ``world.relation`` row with ``release_id`` per
    evidence relation derived from an input observation of the release's estimates.

    A relation holds exactly one of ``release_id`` / ``evidence_observation_id`` and is
    sealed with its creating transaction, so evidence relations are not re-pointed:
    the release gets its own rows (same predicate, layer, support, uncertainty,
    strength and participants), whose component adds the selected ``flow_magnitude``
    and ``derived_from`` (source relation, observation, estimate). Must run in the
    publication transaction before ``ops.publication`` is inserted (the release is
    sealed afterwards). Returns the number of pinned relations.
    """
    n = conn.execute(
        text(PIN_RELATIONS_SQL), {"rid": release_id, "act": activity_id, "method": f"{METHOD_KEY}@{METHOD_VERSION}"}
    ).rowcount
    if n:
        conn.execute(text(PIN_PARTICIPANTS_SQL), {"rid": release_id})
    return int(n or 0)


# --------------------------------------------------------------------------- publication pauses
def _active_pauses(conn: Connection) -> dict[str, dict[str, Any]]:
    from reva.autonomy.repair import active_publication_pauses

    return {p["concept"]: p for p in active_publication_pauses(conn)}


def _pause_record(p: dict[str, Any]) -> dict[str, Any]:
    from reva.autonomy.repair import PAUSE_WITHHOLD_REASON

    return {
        "concept_id": p["concept"],
        "reason": PAUSE_WITHHOLD_REASON,
        "pause_reason": p.get("reason"),
        "proposal_id": p.get("proposal_id"),
        "paused_since": p.get("since"),
    }


def _withhold_paused(
    conn: Connection, selections: list[Selection], pauses: dict[str, dict[str, Any]], origin: str
) -> tuple[list[Selection], list[Withheld]]:
    """Split selections into publishable ones and ``source_schema_quarantine`` withholdings."""
    if not pauses:
        return selections, []
    from reva.autonomy.repair import PAUSE_WITHHOLD_REASON, last_accepted_release

    keep: list[Selection] = []
    withheld: list[Withheld] = []
    last: dict[str, dict[str, Any] | None] = {}
    for s in selections:
        p = pauses.get(s.concept_id)
        if p is None:
            keep.append(s)
            continue
        if s.concept_id not in last:
            last[s.concept_id] = last_accepted_release(conn, origin, s.concept_id)
        withheld.append(
            Withheld(
                subject_id=s.subject_id,
                concept_version_id=s.concept_version_id,
                concept_id=s.concept_id,
                support_id=s.support_id,
                reason=PAUSE_WITHHOLD_REASON,
                discrepancy_flags=[],
                alternatives=[],
                details={
                    **_pause_record(p),
                    "selected_observation_id": str(s.observation_id),
                    "last_accepted_release": last[s.concept_id],
                    "visibility": "the last accepted release stays readable and is served as stale",
                },
            )
        )
    return keep, withheld


def _keep_paused_dirty(conn: Connection, origin: str, concepts: list[str]) -> None:
    """A withheld concept stays dirty for this origin, so lifting the pause republishes it."""
    if not concepts:
        return
    seq = int(conn.execute(text("SELECT COALESCE(max(id), 0) FROM ops.outbox")).scalar_one())
    for c in concepts:
        conn.execute(
            text("""INSERT INTO ops.dirty_node AS d (node_key, since_sequence, accumulated_error_bound, reason)
                    VALUES (:k, :s, '{"kind":"none","value":"0"}'::jsonb, :r)
                    ON CONFLICT (node_key) DO UPDATE SET updated_at = clock_timestamp(),
                      reason = CASE WHEN d.reason = EXCLUDED.reason OR d.reason = 'upstream_changed' THEN d.reason
                                    WHEN d.reason LIKE 'upstream_changed:%' THEN 'upstream_changed:mixed'
                                    ELSE EXCLUDED.reason END"""),
            {"k": f"belief:{c}", "s": seq, "r": f"upstream_changed:{origin}"},
        )


def _clear_dirty_nodes(
    conn: Connection, origin: str, concepts: list[str] | None, keep: list[str] | None = None
) -> None:
    """Clear belief dirty markers this release actually covers (R-06).

    Only nodes of the published concepts (all belief nodes for a full
    publication), and only markers caused by this origin's changes (or by
    origin-less invalidations); a mixed marker is narrowed to the other origin so
    its pending publication is not lost. Under the evidence lock no ingest is in
    flight, so every marker present was caused by evidence visible to selection.
    """
    nodes = None if concepts is None else sorted({f"belief:{c}" for c in concepts})
    kept = sorted({f"belief:{c}" for c in keep or []})  # withheld (paused) concepts stay dirty
    other = "test_fixture" if origin == "live" else "live"
    scope = (
        "node_key LIKE 'belief:%' AND (CAST(:n AS text[]) IS NULL OR node_key = ANY(CAST(:n AS text[]))) "
        "AND NOT (node_key = ANY(CAST(:kept AS text[])))"
    )
    conn.execute(
        text(
            f"DELETE FROM ops.dirty_node WHERE {scope} AND (reason = :mine "
            "OR reason NOT LIKE 'upstream_changed%' OR reason = 'upstream_changed')"
        ),
        {"n": nodes, "kept": kept, "mine": f"upstream_changed:{origin}"},
    )
    conn.execute(
        text(f"UPDATE ops.dirty_node SET reason = :other WHERE {scope} AND reason = 'upstream_changed:mixed'"),
        {"n": nodes, "kept": kept, "other": f"upstream_changed:{other}"},
    )


def add_prior_estimate_input(conn: Connection, estimate_id: UUID, prior_estimate_id: UUID) -> None:
    """Record a prior belief estimate as an input of another estimate (R-27).

    The prior must come from a *published* release whose knowledge cutoff is not
    later than the consuming release's, and a live release may only cite live
    priors (a fixture or test estimate can never inform the live world).
    """
    rows = (
        conn.execute(
            text(
                """SELECT e.id, r.origin, r.knowledge_cutoff, p.sequence
               FROM belief.estimate e JOIN belief.release r ON r.id = e.release_id
               LEFT JOIN ops.publication p ON p.release_id = r.id
               WHERE e.id = ANY(:ids)"""
            ),
            {"ids": [estimate_id, prior_estimate_id]},
        )
        .mappings()
        .all()
    )
    by_id = {UUID(str(r["id"])): r for r in rows}
    if estimate_id not in by_id or prior_estimate_id not in by_id:
        raise ValidationProblem("unknown estimate")
    cur, prior = by_id[estimate_id], by_id[prior_estimate_id]
    if prior["sequence"] is None:
        raise ValidationProblem("prior estimate belongs to an unpublished release")
    if prior["knowledge_cutoff"] > cur["knowledge_cutoff"]:
        raise ValidationProblem("prior estimate comes from a later knowledge cutoff")
    if cur["origin"] == "live" and prior["origin"] != "live":
        raise ValidationProblem("a live release cannot cite a non-live prior estimate", prior_origin=prior["origin"])
    conn.execute(
        text("INSERT INTO belief.input(estimate_id, prior_estimate_id) VALUES (:e, :p)"),
        {"e": estimate_id, "p": prior_estimate_id},
    )
