"""Release-pinned reads. World read APIs are projections of a pinned release;
there is no unversioned current_facts table."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def release_as_of(conn: Connection, as_of: datetime | None, origin: str | None = None) -> dict[str, Any] | None:
    """The release REVA actually had published at `as_of` (operational_as_known).

    Definition of the *current release* as of time T (``as_of=None`` means the
    database's ``clock_timestamp()``): among published releases (optionally of one
    origin) with ``published_at <= T`` **and** ``knowledge_cutoff <= T``, the one
    with the highest publication sequence. A release whose knowledge cutoff lies
    after T is never returned, whatever its sequence (R-20).
    """
    row = conn.execute(
        text(
            """WITH t AS (SELECT COALESCE(CAST(:t AS timestamptz), clock_timestamp()) AS at)
               SELECT r.*, p.sequence, p.published_at FROM belief.release r
               JOIN ops.publication p ON p.release_id = r.id
               CROSS JOIN t
               WHERE p.published_at <= t.at
                 AND r.knowledge_cutoff <= t.at
                 AND (CAST(:o AS text) IS NULL OR r.origin = :o)
               ORDER BY p.sequence DESC LIMIT 1"""
        ),
        {"t": as_of, "o": origin},
    ).mappings().one_or_none()
    return dict(row) if row else None


def release_by_id(conn: Connection, release_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """SELECT r.*, p.sequence, p.published_at FROM belief.release r
               LEFT JOIN ops.publication p ON p.release_id = r.id WHERE r.id = :i"""
        ),
        {"i": release_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


ESTIMATES_SQL = """
SELECT e.id, e.subject_id, ent.label AS subject_label, c.concept_id, c.version AS concept_version,
       c.semantic_profile, e.support_id, s.temporal_extent, s.spatial_spec, s.aggregation, e.modality, e.value,
       e.uncertainty, e.alternatives, e.discrepancy_flags, i.observation_id, o.source_flags, o.revision_no,
       o.recorded_at AS observation_recorded_at, src.source_key, a.retrieved_at
FROM belief.estimate e
JOIN world.entity ent ON ent.id = e.subject_id
JOIN ontology.concept_version c ON c.id = e.concept_version_id
JOIN world.support s ON s.id = e.support_id
LEFT JOIN belief.input i ON i.estimate_id = e.id
LEFT JOIN evidence.observation o ON o.id = i.observation_id
LEFT JOIN evidence.source src ON src.id = o.source_id
LEFT JOIN evidence.acquisition a ON a.id = o.acquisition_id
WHERE e.release_id = :r
  AND (CAST(:subject AS uuid) IS NULL OR e.subject_id = :subject)
  AND (CAST(:concept AS text) IS NULL OR c.concept_id = :concept)
ORDER BY c.concept_id, ent.label, s.temporal_extent->>'start'
"""


def estimates(
    conn: Connection, release_id: UUID, *, subject_id: UUID | None = None, concept_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            text(ESTIMATES_SQL), {"r": release_id, "subject": subject_id, "concept": concept_id}
        ).mappings()
    ]
