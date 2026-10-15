"""PostgreSQL repositories. Only this package understands physical tables.

Functions take an explicit ``Connection`` so callers control transaction scope:
objects are written to the store first, then metadata, manifest references and
outbox rows commit in one transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import IntegrityFailure
from reva.kernel.ids import canonical_bytes, logical_hash, sha256_bytes
from reva.kernel.records import Activity
from reva.kernel.support import Support
from reva.kernel.time import utc_now
from reva.ports.storage import ArtifactStore, StoredObject

PUBLICATION_LOCK = 7262011


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, allow_nan=False)


# --------------------------------------------------------------------------- artifacts
def register_artifact(
    conn: Connection,
    obj: StoredObject,
    *,
    schema_version: str,
    rights: Mapping[str, Any],
    privacy_class: str = "public_aggregate",
    logical_sha256: str | None = None,
) -> str:
    """Record a verified object. Must run in the publication transaction.

    Takes the per-object advisory lock shared with orphan GC so a concurrent GC
    can never delete bytes that this transaction is about to reference.
    """
    from reva.adapters.objects.common import lock_object_xact

    lock_object_xact(conn, obj.sha256)
    conn.execute(
        text(
            """INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class,
                                         rights, logical_sha256)
               VALUES (:sha, :mt, :size, :sv, :pc, CAST(:rights AS jsonb), :lsha)
               ON CONFLICT (sha256) DO NOTHING"""
        ),
        {
            "sha": obj.sha256,
            "mt": obj.media_type,
            "size": obj.byte_size,
            "sv": schema_version,
            "pc": privacy_class,
            "rights": _j(dict(rights)),
            "lsha": logical_sha256,
        },
    )
    # (R5-05) Content-addressed reuse: an existing row describes the same bytes,
    # so its size must equal the size the store just verified. A mismatch means
    # the row was registered without the bytes and is reported as a typed
    # integrity failure (never retried into the location trigger). Labels
    # (schema_version, media type, rights) of an earlier registration of the
    # same bytes are kept: identical bytes legitimately recur across writers
    # (e.g. an empty provider response), and the simulation role -- the only
    # lower-trust registrant -- can no longer choose any metadata
    # (sim.register_artifact derives it from the bytes).
    stored = conn.execute(
        text("SELECT byte_size FROM meta.artifact WHERE sha256 = :sha"), {"sha": obj.sha256}
    ).scalar_one()
    if stored != obj.byte_size:
        raise IntegrityFailure(
            f"artifact {obj.sha256} is registered with byte_size {stored}, but the verified object has "
            f"{obj.byte_size} bytes",
            sha256=obj.sha256,
            registered_byte_size=stored,
            verified_byte_size=obj.byte_size,
        )
    conn.execute(
        text(
            """INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace,
                                                  verified_byte_size, checksum_verified_at)
               VALUES (:sha, :uri, :ns, :size, clock_timestamp())
               ON CONFLICT (artifact_sha256, storage_uri) DO NOTHING"""
        ),
        {"sha": obj.sha256, "uri": obj.storage_uri, "ns": obj.namespace, "size": obj.byte_size},
    )
    return obj.sha256


def store_json_artifact(
    conn: Connection,
    store: ArtifactStore,
    payload: Any,
    namespace: str,
    *,
    schema_version: str,
    rights: Mapping[str, Any] | None = None,
    privacy_class: str = "public_aggregate",
) -> str:
    data = canonical_bytes(payload)
    obj = store.put_bytes(data, namespace, "application/json")
    return register_artifact(
        conn,
        obj,
        schema_version=schema_version,
        rights=rights or {"licence": "internal-derived"},
        privacy_class=privacy_class,
        logical_sha256=logical_hash(payload),
    )


def artifact_locations(conn: Connection, sha: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT storage_uri, namespace, verified_byte_size, checksum_verified_at "
            "FROM meta.artifact_location WHERE artifact_sha256 = :s"
        ),
        {"s": sha},
    ).mappings()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- activities
def record_activity(
    conn: Connection,
    activity: Activity,
    inputs: Sequence[tuple[str, Mapping[str, Any]]] = (),
    outputs: Iterable[str] = (),
) -> UUID:
    conn.execute(
        text(
            """INSERT INTO meta.activity(id, kind, origin, code_digest, parameters, environment_digest,
                                         actor, started_at, ended_at)
               VALUES (:id, :kind, :origin, :code, CAST(:params AS jsonb), :env, :actor, :s, :e)"""
        ),
        {
            "id": activity.id,
            "kind": activity.kind,
            "origin": activity.origin.value,
            "code": activity.code_digest,
            "params": _j(activity.parameters),
            "env": activity.environment_digest,
            "actor": activity.actor,
            "s": activity.started_at,
            "e": activity.ended_at,
        },
    )
    for i, (sha, selector) in enumerate(inputs):
        conn.execute(
            text(
                "INSERT INTO meta.activity_input(activity_id, ordinal, artifact_sha256, selector) "
                "VALUES (:a, :o, :s, CAST(:sel AS jsonb))"
            ),
            {"a": activity.id, "o": i, "s": sha, "sel": _j(dict(selector))},
        )
    for sha in dict.fromkeys(outputs):
        conn.execute(
            text("INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)"),
            {"a": activity.id, "s": sha},
        )
    return activity.id


def simple_activity(
    kind: str,
    origin: str,
    actor: str,
    parameters: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
) -> Activity:
    from reva.kernel.ids import new_id
    from reva.kernel.records import ActivityOrigin
    from reva.operations.environment import code_digest, environment_digest

    now = utc_now()
    return Activity(
        id=new_id(),
        kind=kind,
        origin=ActivityOrigin(origin),
        code_digest=code_digest(),
        parameters=dict(parameters or {}),
        environment_digest=environment_digest(),
        actor=actor,
        started_at=started_at or now,
        ended_at=now if (started_at is None or now >= started_at) else started_at,
    )


# --------------------------------------------------------------------------- contexts
def create_context(
    conn: Connection,
    *,
    mode: str,
    origin: str,
    manifest_sha256: str,
    knowledge_cutoff: datetime | None,
    parent_context_id: UUID | None = None,
) -> UUID:
    return UUID(
        str(
            conn.execute(
                text(
                    """INSERT INTO meta.context(mode, origin, parent_context_id, manifest_sha256, knowledge_cutoff)
                       VALUES (:m, :o, :p, :sha, :c) RETURNING id"""
                ),
                {
                    "m": mode,
                    "o": origin,
                    "p": parent_context_id,
                    "sha": manifest_sha256,
                    "c": knowledge_cutoff,
                },
            ).scalar_one()
        )
    )


# --------------------------------------------------------------------------- ontology / world
def concept_version_id(conn: Connection, concept_id: str, version: str | None = None) -> UUID:
    q = "SELECT id FROM ontology.concept_version WHERE concept_id = :c"
    params: dict[str, Any] = {"c": concept_id}
    if version:
        q += " AND version = :v"
        params["v"] = version
    q += " ORDER BY recorded_at DESC LIMIT 1"
    row = conn.execute(text(q), params).scalar_one_or_none()
    if row is None:
        raise KeyError(f"concept {concept_id}@{version or 'latest'} is not registered")
    return UUID(str(row))


def concept_profile(conn: Connection, concept_uuid: UUID) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT concept_id, version, kind, semantic_profile, definition "
                "FROM ontology.concept_version WHERE id=:i"
            ),
            {"i": concept_uuid},
        )
        .mappings()
        .one()
    )
    return dict(row)


def resolve_alias(conn: Connection, provider: str, external_key: str) -> UUID | None:
    row = conn.execute(
        text(
            "SELECT entity_id FROM world.alias WHERE provider = :p AND external_key = :k "
            "AND confidence_kind IN ('reviewed_exact','provider_declared') "
            "ORDER BY recorded_at DESC LIMIT 1"
        ),
        {"p": provider, "k": external_key},
    ).scalar_one_or_none()
    return UUID(str(row)) if row else None


def ensure_support(conn: Connection, support: Support, activity_id: UUID) -> UUID:
    digest = support.digest()
    row = conn.execute(
        text("SELECT id FROM world.support WHERE support_digest = :d"), {"d": digest}
    ).scalar_one_or_none()
    if row:
        return UUID(str(row))
    t = support.temporal
    valid_range = None
    if t.start is not None:
        upper = t.end.isoformat() if t.end else None
        if t.kind == "instant":
            valid_range = f"[{t.start.isoformat()} 00:00:00+00,{t.start.isoformat()} 00:00:01+00)"
        elif upper:
            valid_range = f"[{t.start.isoformat()} 00:00:00+00,{upper} 00:00:00+00)"
    return UUID(
        str(
            conn.execute(
                text(
                    """INSERT INTO world.support(temporal_extent, valid_range, spatial_spec, population_spec,
                                                 aggregation, resolution, provenance_activity_id, support_digest)
                       VALUES (CAST(:t AS jsonb), CAST(:vr AS tstzrange), CAST(:sp AS jsonb), CAST(:pop AS jsonb),
                               :agg, CAST(:res AS jsonb), :act, :d)
                       ON CONFLICT (support_digest) DO NOTHING
                       RETURNING id"""
                ),
                {
                    "t": support.temporal.model_dump_json(),
                    "vr": valid_range,
                    "sp": support.spatial.model_dump_json(),
                    "pop": support.population.model_dump_json(),
                    "agg": support.aggregation,
                    "res": _j(support.resolution),
                    "act": activity_id,
                    "d": digest,
                },
            ).scalar_one_or_none()
            or conn.execute(text("SELECT id FROM world.support WHERE support_digest = :d"), {"d": digest}).scalar_one()
        )
    )


def support_record(conn: Connection, support_id: UUID) -> dict[str, Any]:
    return dict(
        conn.execute(
            text(
                "SELECT id, temporal_extent, spatial_spec, population_spec, aggregation, resolution "
                "FROM world.support WHERE id = :i"
            ),
            {"i": support_id},
        )
        .mappings()
        .one()
    )


# --------------------------------------------------------------------------- sources
def source_by_key(conn: Connection, source_key: str) -> dict[str, Any] | None:
    row = (
        conn.execute(text("SELECT * FROM evidence.source WHERE source_key = :k"), {"k": source_key})
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


def latest_source_metadata_version(conn: Connection, source_id: UUID) -> dict[str, Any]:
    return dict(
        conn.execute(
            text("SELECT * FROM evidence.source_metadata_version WHERE source_id = :s ORDER BY version DESC LIMIT 1"),
            {"s": source_id},
        )
        .mappings()
        .one()
    )


def append_source_metadata_if_changed(
    conn: Connection, source_id: UUID, metadata: Mapping[str, Any], rights: Mapping[str, Any], activity_id: UUID
) -> UUID:
    latest = latest_source_metadata_version(conn, source_id)
    if logical_hash(latest["metadata"]) == logical_hash(dict(metadata)) and logical_hash(
        latest["rights"]
    ) == logical_hash(dict(rights)):
        return UUID(str(latest["id"]))
    return UUID(
        str(
            conn.execute(
                text(
                    """INSERT INTO evidence.source_metadata_version(source_id, version, metadata, rights,
                                                                    provenance_activity_id)
                       VALUES (:s, :v, CAST(:m AS jsonb), CAST(:r AS jsonb), :a) RETURNING id"""
                ),
                {
                    "s": source_id,
                    "v": latest["version"] + 1,
                    "m": _j(dict(metadata)),
                    "r": _j(dict(rights)),
                    "a": activity_id,
                },
            ).scalar_one()
        )
    )


def connector_version_row(conn: Connection, name: str, version: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text("SELECT * FROM evidence.connector_version WHERE name = :n AND version = :v"),
            {"n": name, "v": version},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


# --------------------------------------------------------------------------- publication
def take_publication_lock(conn: Connection) -> None:
    """Transaction-scoped publication lock (held until commit, so sequence order = commit order).

    ``ops.check_publication`` requires it to be held before the ``ops.publication`` INSERT and
    re-acquires it at transaction scope itself (re-entrant; R5-08): it never probes with
    ``pg_advisory_unlock``, which would release a caller's session-level hold.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": PUBLICATION_LOCK})


def emit_outbox(conn: Connection, event_type: str, payload: Mapping[str, Any]) -> None:
    conn.execute(
        text("INSERT INTO ops.outbox(event_type, payload) VALUES (:t, CAST(:p AS jsonb))"),
        {"t": event_type, "p": _j(dict(payload))},
    )


def audit(conn: Connection, actor: str, action: str, target: Mapping[str, Any], details: Mapping[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO ops.audit(actor, action, target, details) "
            "VALUES (:a, :ac, CAST(:t AS jsonb), CAST(:d AS jsonb))"
        ),
        {"a": actor, "ac": action, "t": _j(dict(target)), "d": _j(dict(details))},
    )


def raw_bytes_hash(data: bytes) -> str:
    return sha256_bytes(data)
