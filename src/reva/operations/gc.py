"""Two-pass orphan GC for staged objects.

An object present in the store under one of the GC namespaces with no
``meta.artifact_location`` row (by storage URI, or by its content address in
the same namespace) is an orphan candidate: the bytes were written before a
publication transaction that never committed. It is deleted only when

* its object timestamp is older than ``min_age`` (default 7 days), AND
* it has been seen unreferenced by at least two separate scans
  (``ops.gc_candidate.confirmed_scans >= 2``), AND
* a reference re-check immediately before deletion still finds no location.

Referenced (published) objects are never deleted; candidates that became
referenced are dropped from ops.gc_candidate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from reva.operations import audit
from reva.operations.context import JobContext
from reva.ports.storage import ArtifactStore

log = logging.getLogger("reva.gc")

GC_NAMESPACES: tuple[str, ...] = ("raw", "normalised", "quarantine", "belief", "scenarios", "evaluation", "models")
MIN_AGE = timedelta(days=7)
REQUIRED_SCANS = 2
_SHA = re.compile(r"([0-9a-f]{64})")


@dataclass
class GcReport:
    scanned: int = 0
    referenced: int = 0
    candidates: int = 0
    deleted: list[str] = field(default_factory=list)
    retained_young: int = 0
    retained_unconfirmed: int = 0
    dropped_candidates: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "referenced": self.referenced, "candidates": self.candidates,
                "deleted": len(self.deleted), "retained_young": self.retained_young,
                "retained_unconfirmed": self.retained_unconfirmed, "dropped_candidates": self.dropped_candidates}


def _sha_from_uri(uri: str) -> str | None:
    m = _SHA.findall(uri)
    return m[-1] if m else None


def _referenced(conn: Any, ns: str, uris: list[str]) -> set[str]:
    if not uris:
        return set()
    by_uri = set(conn.execute(
        text("SELECT storage_uri FROM meta.artifact_location WHERE storage_uri = ANY(:u)"), {"u": uris}
    ).scalars())
    shas = {u: s for u in uris if (s := _sha_from_uri(u))}
    by_sha = set(conn.execute(
        text("SELECT artifact_sha256 FROM meta.artifact_location WHERE namespace = :ns AND artifact_sha256 = ANY(:s)"),
        {"ns": ns, "s": sorted(set(shas.values()))},
    ).scalars()) if shas else set()
    return {u for u in uris if u in by_uri or shas.get(u) in by_sha}


def gc_scan(
    engine: Engine,
    store: ArtifactStore,
    *,
    min_age: timedelta = MIN_AGE,
    namespaces: tuple[str, ...] = GC_NAMESPACES,
    actor: str = "worker:gc",
    now: datetime | None = None,
    delete: bool = True,
) -> GcReport:
    report = GcReport()
    with engine.connect() as conn:
        db_now: datetime = now or conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    seen_orphans: dict[str, tuple[float, str]] = {}
    for ns in namespaces:
        listing = list(store.list_uris(ns))
        report.scanned += len(listing)
        uris = [u for u, _ in listing]
        with engine.connect() as conn:
            refs: set[str] = set()
            for i in range(0, len(uris), 1000):
                refs |= _referenced(conn, ns, uris[i:i + 1000])
        report.referenced += len(refs)
        for uri, mtime in listing:
            if uri not in refs:
                seen_orphans[uri] = (mtime, ns)
    with engine.begin() as conn:
        # Candidates no longer present or now referenced are forgotten.
        stale = conn.execute(text("SELECT storage_uri FROM ops.gc_candidate")).scalars().all()
        drop = [u for u in stale if u not in seen_orphans]
        if drop:
            conn.execute(text("DELETE FROM ops.gc_candidate WHERE storage_uri = ANY(:u)"), {"u": drop})
            report.dropped_candidates = len(drop)
        scans: dict[str, int] = {}
        for uri in sorted(seen_orphans):
            scans[uri] = int(conn.execute(
                text("""INSERT INTO ops.gc_candidate(storage_uri) VALUES (:u)
                        ON CONFLICT (storage_uri) DO UPDATE SET confirmed_scans = ops.gc_candidate.confirmed_scans + 1
                        RETURNING confirmed_scans"""),
                {"u": uri},
            ).scalar_one())
    report.candidates = len(seen_orphans)
    for uri in sorted(seen_orphans):
        mtime, ns = seen_orphans[uri]
        age = db_now - datetime.fromtimestamp(mtime, UTC)
        if age < min_age:
            report.retained_young += 1
            continue
        if scans[uri] < REQUIRED_SCANS:
            report.retained_unconfirmed += 1
            continue
        if not delete:
            continue
        with engine.begin() as conn:
            if _referenced(conn, ns, [uri]):  # re-check right before deletion
                conn.execute(text("DELETE FROM ops.gc_candidate WHERE storage_uri = :u"), {"u": uri})
                continue
            store.delete_orphan(uri)
            conn.execute(text("DELETE FROM ops.gc_candidate WHERE storage_uri = :u"), {"u": uri})
            audit.record(conn, actor, "gc.delete_orphan", {"storage_uri": uri},
                         {"age_seconds": int(age.total_seconds()), "confirmed_scans": scans[uri]})
        report.deleted.append(uri)
    if report.deleted:
        log.info("gc deleted %d orphan objects", len(report.deleted))
    return report


def handle_gc(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    days = float(payload.get("min_age_days", 7))
    if days < 7:
        from reva.kernel.errors import PolicyDenied

        raise PolicyDenied("orphan GC never deletes objects younger than 7 days")
    rep = gc_scan(ctx.worker_engine, ctx.store, min_age=timedelta(days=days), actor="worker:gc")
    return {"status": "completed", **rep.as_dict()}
