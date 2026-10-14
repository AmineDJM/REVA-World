"""Observation retraction: the controlled write path.

A retraction is an append-only decision recorded against one observation
revision. It follows the evidence lock protocol (``reva.evidence.locks``): the
shared lock is taken before the row is written, so a concurrent belief release
either sees the committed retraction or selects with a cutoff earlier than its
``recorded_at``. Call it before any other evidence write in the transaction.

Selection semantics (``reva.belief.selection``): retracting a lineage's latest
revision excludes the lineage; the superseded revision is never republished as
current in its place.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from reva.adapters.postgres import repositories as repo
from reva.evidence.locks import take_evidence_write_lock
from reva.kernel.errors import ValidationProblem


def retract_observation(conn: Connection, observation_id: UUID, reason: str, *, actor: str) -> UUID:
    if not reason.strip():
        raise ValidationProblem("a retraction requires a reason")
    take_evidence_write_lock(conn)
    exists = conn.execute(
        text("SELECT 1 FROM evidence.observation WHERE id = :o"), {"o": observation_id}
    ).scalar_one_or_none()
    if exists is None:
        raise ValidationProblem("unknown observation", observation_id=str(observation_id))
    act = repo.simple_activity("evidence.retraction", "review", actor, {"observation_id": str(observation_id)})
    repo.record_activity(conn, act)
    rid = conn.execute(
        text(
            "INSERT INTO evidence.observation_retraction(observation_id, reason, activity_id) "
            "VALUES (:o, :r, :a) RETURNING id"
        ),
        {"o": observation_id, "r": reason, "a": act.id},
    ).scalar_one()
    return UUID(str(rid))
