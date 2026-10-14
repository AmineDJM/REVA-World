"""Evidence/publication lock protocol (R-03).

Every transaction that writes rows ``belief.publish_release`` selects from
(observations, acquisitions, retractions, eligibility decisions, claims) takes
the *shared* transaction advisory lock ``EVIDENCE_PUBLICATION_LOCK`` as its first
statement. ``publish_release`` takes the same key *exclusively* and only then
reads its cutoff (``clock_timestamp()``) and runs selection, inside the same
transaction. Consequently:

* no evidence write is in flight while a release selects (every writer that
  started before the exclusive lock was granted has committed, or rolled back);
* every writer that starts after the lock is released records rows with a
  server timestamp later than the cutoff (``recorded_at`` is server-set).

So a release can never omit an observation whose ``recorded_at`` is at or before
its declared ``knowledge_cutoff``, and later as-of reconstructions agree with it.
Writers share the lock among themselves, so ingests still run concurrently.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

EVIDENCE_PUBLICATION_LOCK = 7262020


def take_evidence_write_lock(conn: Connection) -> None:
    """Shared lock for evidence writers (call first in the write transaction)."""
    conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": EVIDENCE_PUBLICATION_LOCK})


def take_evidence_snapshot_lock(conn: Connection) -> None:
    """Exclusive lock for belief publication: waits for in-flight evidence writers."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": EVIDENCE_PUBLICATION_LOCK})
