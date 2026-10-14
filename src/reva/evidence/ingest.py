"""Evidence ingestion: the controlled write path for source observations.

Publication protocol: bytes are written to the object store (content-addressed,
verified) *before* the transaction; metadata, observations, relations, outbox
and invalidation rows then commit atomically. A crash before commit leaves an
unreferenced object (an orphan for GC), never partially visible data.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.adapters.postgres import repositories as repo
from reva.evidence.revisions import LineageHead, decide_revision, semantic_hash
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import canonical_bytes
from reva.ports.connectors import (
    ConnectorSpec,
    ObservationBatch,
    ParsedObservation,
    QualityReport,
    RawAcquisition,
)
from reva.ports.storage import ArtifactStore, StoredObject

log = logging.getLogger("reva.ingest")


@dataclass
class PublishSummary:
    acquisition_id: UUID | None
    raw_sha256: str
    namespace: str
    inserted: int = 0
    unchanged: int = 0
    relations: int = 0
    duplicate_acquisition: bool = False
    changed_keys: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ConnectorRegistration:
    source_id: UUID
    connector_version_id: UUID
    source_metadata_version_id: UUID
    rights: dict[str, Any]


def connector_registration(conn: Connection, spec: ConnectorSpec) -> ConnectorRegistration:
    cv = repo.connector_version_row(conn, spec.name, spec.version)
    if cv is None:
        raise ValidationProblem(f"connector {spec.name}@{spec.version} is not registered (run seed)")
    src = conn.execute(
        text("SELECT id, rights FROM evidence.source WHERE id = :i"), {"i": cv["source_id"]}
    ).mappings().one()
    smv = repo.latest_source_metadata_version(conn, src["id"])
    return ConnectorRegistration(src["id"], cv["id"], smv["id"], dict(smv["rights"]))


def acquisition_idempotency_key(spec: ConnectorSpec, raw: RawAcquisition, raw_sha: str) -> str:
    basis = f"{spec.name}|{spec.version}|{raw.request_fingerprint}|{raw_sha}|{raw.origin}"
    return hashlib.sha256(basis.encode()).hexdigest()


def write_raw_object(store: ArtifactStore, raw: RawAcquisition, quarantined: bool) -> StoredObject:
    ns = "quarantine" if quarantined else "raw"
    return store.put_bytes(raw.body, ns, raw.media_type or "application/octet-stream")


def _lineage_head(conn: Connection, source_id: UUID, logical_key: str) -> LineageHead | None:
    # Serialise writers of the same lineage for the duration of the transaction.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"{source_id}|{logical_key}"},
    )
    row = conn.execute(
        text(
            "SELECT id, revision_no, semantic_hash FROM evidence.observation "
            "WHERE source_id = :s AND logical_key = :k ORDER BY revision_no DESC LIMIT 1"
        ),
        {"s": source_id, "k": logical_key},
    ).one_or_none()
    return LineageHead(row[0], row[1], row[2]) if row else None


def _insert_observation(
    conn: Connection,
    reg: ConnectorRegistration,
    acquisition_id: UUID,
    raw_sha: str,
    obs: ParsedObservation,
    parser_version: str,
    activity_id: UUID,
) -> tuple[str, UUID | None]:
    subject = repo.resolve_alias(conn, obs.subject_provider, obs.subject_key)
    if subject is None:
        raise ValidationProblem(
            f"no reviewed alias for {obs.subject_provider}:{obs.subject_key}",
            provider=obs.subject_provider,
            key=obs.subject_key,
        )
    concept = repo.concept_version_id(conn, obs.concept_id, obs.concept_version)
    support_id = repo.ensure_support(conn, obs.support, activity_id)
    shash = semantic_hash(obs)
    decision = decide_revision(_lineage_head(conn, reg.source_id, obs.logical_key), shash)
    if decision.action == "unchanged":
        return "unchanged", None
    selector = {**obs.raw_selector, "artifact_sha256": raw_sha}
    record = {
        "logical_key": obs.logical_key,
        "revision_no": decision.revision_no,
        "value": obs.value,
        "semantic_hash": shash,
        "acquisition_id": str(acquisition_id),
    }
    oid = conn.execute(
        text(
            """INSERT INTO evidence.observation(
                 source_id, connector_version_id, acquisition_id, logical_key, revision_no,
                 source_revision_id, supersedes_id, subject_id, concept_version_id, support_id, value,
                 uncertainty, source_flags, observed_at, source_published_at, source_available_at,
                 availability_basis, raw_selector, parser_version, provenance_activity_id, semantic_hash,
                 record_hash)
               VALUES (:src, :cv, :acq, :lk, :rev, :srid, :sup, :subj, :concept, :support,
                 CAST(:value AS jsonb), CAST(:unc AS jsonb), CAST(:flags AS jsonb), :obs_at, :pub_at,
                 :avail_at, CAST(:avail_basis AS jsonb), CAST(:sel AS jsonb), :pv, :act, :shash, :rhash)
               RETURNING id"""
        ),
        {
            "src": reg.source_id,
            "cv": reg.connector_version_id,
            "acq": acquisition_id,
            "lk": obs.logical_key,
            "rev": decision.revision_no,
            "srid": obs.source_revision_id,
            "sup": decision.supersedes_id,
            "subj": subject,
            "concept": concept,
            "support": support_id,
            "value": canonical_json(obs.value),
            "unc": obs.uncertainty.model_dump_json(),
            "flags": canonical_json(obs.source_flags),
            "obs_at": obs.observed_at,
            "pub_at": obs.source_published_at,
            "avail_at": obs.source_available_at,
            "avail_basis": canonical_json(obs.availability_basis) if obs.availability_basis else None,
            "sel": canonical_json(selector),
            "pv": parser_version,
            "act": activity_id,
            "shash": shash,
            "rhash": hashlib.sha256(canonical_bytes(record)).hexdigest(),
        },
    ).scalar_one()
    return "inserted", UUID(str(oid))


def canonical_json(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, default=str, allow_nan=False, separators=(",", ":"))


def _insert_relation(
    conn: Connection, obs: ParsedObservation, observation_id: UUID, support_id: UUID, activity_id: UUID
) -> None:
    rel = obs.relation
    assert rel is not None
    predicate = repo.concept_version_id(conn, rel.predicate)
    rid = conn.execute(
        text(
            """INSERT INTO world.relation(predicate_id, layer, support_id, evidence_observation_id,
                 existence_uncertainty, strength, component, provenance_activity_id)
               VALUES (:p, :layer, :s, :o, CAST(:eu AS jsonb), NULL, CAST(:c AS jsonb), :a) RETURNING id"""
        ),
        {
            "p": predicate,
            "layer": rel.layer,
            "s": support_id,
            "o": observation_id,
            "eu": canonical_json({"status": "unquantified", "kind": "unknown",
                                  "description": "reported flow; existence as reported by source"}),
            "c": canonical_json(rel.component),
            "a": activity_id,
        },
    ).scalar_one()
    for i, p in enumerate(rel.participants):
        entity = repo.resolve_alias(conn, p["provider"], p["key"])
        if entity is None:
            raise ValidationProblem(f"unmatched relation participant {p['provider']}:{p['key']}")
        conn.execute(
            text(
                "INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id, direction) "
                "VALUES (:r, :o, :role, :e, :d)"
            ),
            {
                "r": rid,
                "o": i,
                "role": repo.concept_version_id(conn, p["role"]),
                "e": entity,
                "d": p.get("direction", "undirected"),
            },
        )


def publish_acquisition(
    engine: Engine,
    store: ArtifactStore,
    spec: ConnectorSpec,
    raw: RawAcquisition,
    batches: Sequence[ObservationBatch],
    reports: Sequence[QualityReport],
    *,
    actor: str = "worker:ingest",
    fault_after_object_write: bool = False,
) -> PublishSummary:
    quarantined = any(r.blocking for r in reports)
    obj = write_raw_object(store, raw, quarantined)  # 1-2: bytes first, verified
    if fault_after_object_write:  # used by fault-injection tests
        raise RuntimeError("injected fault after object upload, before commit")
    summary = PublishSummary(None, obj.sha256, obj.namespace)
    with engine.begin() as conn:  # 4: one transaction
        reg = connector_registration(conn, spec)
        idem = acquisition_idempotency_key(spec, raw, obj.sha256)
        existing = conn.execute(
            text("SELECT id FROM evidence.acquisition WHERE idempotency_key = :k"), {"k": idem}
        ).scalar_one_or_none()
        if existing is not None:
            summary.acquisition_id = UUID(str(existing))
            summary.duplicate_acquisition = True
            return summary
        repo.register_artifact(
            conn,
            obj,
            schema_version=f"{spec.name}/{spec.parser_version}/raw",
            rights={**reg.rights, "source_key": spec.source_key},
        )
        acq_act = repo.simple_activity(
            "source.acquisition",
            "source",
            actor,
            {
                "connector": spec.name,
                "connector_version": spec.version,
                "item": raw.item.key,
                "request_fingerprint": raw.request_fingerprint,
                "origin": raw.origin,
            },
            started_at=raw.retrieved_at,
        )
        repo.record_activity(conn, acq_act, outputs=[obj.sha256])
        response_metadata = {
            **raw.response_metadata,
            "item_key": raw.item.key,
            "quality": [r.model_dump(mode="json") for r in reports],
            "batch_metadata": [b.metadata for b in batches],
            "schema_fingerprints": sorted({b.schema_fingerprint for b in batches}),
        }
        if raw.replay_of_acquisition_id:
            response_metadata["replay_of_acquisition_id"] = raw.replay_of_acquisition_id
        acq_id = UUID(
            str(
                conn.execute(
                    text(
                        """INSERT INTO evidence.acquisition(source_id, connector_version_id, source_metadata_version_id,
                             origin, request_fingerprint, raw_sha256, retrieved_at, response_metadata,
                             provenance_activity_id, idempotency_key)
                           VALUES (:s, :cv, :smv, :o, :fp, :sha, :rt, CAST(:rm AS jsonb), :a, :k) RETURNING id"""
                    ),
                    {
                        "s": reg.source_id,
                        "cv": reg.connector_version_id,
                        "smv": reg.source_metadata_version_id,
                        "o": raw.origin,
                        "fp": raw.request_fingerprint,
                        "sha": obj.sha256,
                        "rt": raw.retrieved_at,
                        "rm": canonical_json(response_metadata),
                        "a": acq_act.id,
                        "k": idem,
                    },
                ).scalar_one()
            )
        )
        summary.acquisition_id = acq_id
        if quarantined:
            repo.emit_outbox(conn, "evidence.acquisition_quarantined",
                             {"acquisition_id": str(acq_id), "connector": spec.name})
            return summary
        norm_act = repo.simple_activity(
            "source.normalisation",
            "normalisation",
            actor,
            {"connector": spec.name, "parser_version": spec.parser_version, "acquisition_id": str(acq_id)},
        )
        repo.record_activity(conn, norm_act, inputs=[(obj.sha256, {"item": raw.item.key})])
        for batch in batches:
            for obs in batch.observations:
                status, oid = _insert_observation(
                    conn, reg, acq_id, obj.sha256, obs, spec.parser_version, norm_act.id
                )
                if status == "unchanged":
                    summary.unchanged += 1
                    continue
                summary.inserted += 1
                summary.changed_keys.append((obs.concept_id, obs.subject_key))
                if obs.relation is not None and oid is not None:
                    support_id = repo.ensure_support(conn, obs.support, norm_act.id)
                    _insert_relation(conn, obs, oid, support_id, norm_act.id)
                    summary.relations += 1
        if summary.inserted:
            seq = conn.execute(text("SELECT COALESCE(max(id), 0) FROM ops.outbox")).scalar_one()
            repo.emit_outbox(
                conn,
                "evidence.observations_changed",
                {"acquisition_id": str(acq_id), "keys": sorted(map(list, set(summary.changed_keys)))},
            )
            for concept_id, _subject in set(summary.changed_keys):
                conn.execute(
                    text(
                        """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
                           VALUES (:k, :s, '{"kind":"none","value":"0"}'::jsonb, 'upstream_changed')
                           ON CONFLICT (node_key) DO UPDATE SET updated_at = clock_timestamp()"""
                    ),
                    {"k": f"belief:{concept_id}", "s": seq},
                )
    log.info(
        "published acquisition %s: %d inserted, %d unchanged",
        summary.acquisition_id,
        summary.inserted,
        summary.unchanged,
    )
    return summary
