"""Atlas snapshots: the set of atlas tables recorded at a knowledge cutoff (no schema change).

An **atlas snapshot** pins, per atlas table (``evidence.dataset`` rows with
``concept_id = 'atlas.table'``), the latest version recorded at or before its cutoff with
the snapshot's origin. It is recorded (worker role) as

* a canonical JSON manifest artifact (namespace ``normalised``, schema version
  ``atlas-snapshot/1``) listing every pinned table by dataset id and artifact/manifest sha256;
* a ``meta.activity`` (kind ``atlas.snapshot``) whose inputs are the pinned table artifacts and
  whose output is the manifest (parameters: origin, cutoff, manifest sha256, table count).

Like belief publication, :func:`create_atlas_snapshot` takes the evidence snapshot lock
(exclusive) before reading its cutoff, so no dataset registration is in flight: a table
recorded at or before the cutoff can never be missing from the snapshot. Readers
(:func:`resolve_snapshot`, API role) load the manifest by sha256 and verify its bytes.
Snapshots are immutable; a new snapshot is a new manifest.

Entry point: ``uv run python -m reva.atlas.snapshot --origin live`` (worker credentials).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.atlas.tables import ATLAS_DATASET_CONCEPT, NAMESPACE
from reva.kernel.errors import IntegrityFailure, RevaError, ValidationProblem
from reva.ports.storage import ArtifactStore

SNAPSHOT_SCHEMA = "reva-atlas-snapshot/1"
SNAPSHOT_SCHEMA_VERSION = "atlas-snapshot/1"
ACTIVITY_KIND = "atlas.snapshot"

TABLES_AS_OF_SQL = """
SELECT DISTINCT ON (dataset_key)
       id::text AS dataset_id, dataset_key, subject_key, artifact_sha256, manifest_sha256, parser_version,
       row_count, recorded_at
FROM evidence.dataset
WHERE concept_id = :c AND origin = :o AND recorded_at <= :cut
ORDER BY dataset_key, recorded_at DESC, id DESC
"""


class SnapshotUnavailable(RevaError):
    """No recorded atlas snapshot matches the request (typed, never an empty success)."""

    code = "atlas_snapshot_unavailable"
    http_status = 404


@dataclass(frozen=True)
class AtlasSnapshot:
    sha256: str
    origin: str
    cutoff: datetime
    tables: tuple[dict[str, Any], ...]
    activity_id: str | None = None
    reused: bool = False  # create_atlas_snapshot(skip_unchanged=True) returned the previous snapshot

    def ref(self) -> dict[str, Any]:
        return {
            "snapshot_sha256": self.sha256,
            "origin": self.origin,
            "knowledge_cutoff": self.cutoff.isoformat(),
            "tables": len(self.tables),
            "rows": sum(int(t["row_count"]) for t in self.tables),
        }


def atlas_tables_as_of(conn: Connection, *, cutoff: datetime, origin: str) -> list[dict[str, Any]]:
    rows = conn.execute(text(TABLES_AS_OF_SQL), {"c": ATLAS_DATASET_CONCEPT, "o": origin, "cut": cutoff}).mappings()
    return [
        {
            **{k: v for k, v in r.items() if k != "recorded_at"},
            "row_count": int(r["row_count"]),
            "recorded_at": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]


def _pins(tables: Any) -> list[tuple[str, str]]:
    return sorted((str(t["dataset_key"]), str(t["artifact_sha256"])) for t in tables)


def create_atlas_snapshot(
    engine: Engine,
    store: ArtifactStore,
    *,
    origin: str,
    actor: str = "worker:atlas",
    skip_unchanged: bool = False,
) -> AtlasSnapshot:
    """Record a snapshot of every atlas table at the current database time (worker engine).

    ``skip_unchanged`` (scheduled daily snapshots): when the latest recorded snapshot of this
    origin pins exactly the same table artifacts, nothing is recorded and that snapshot is
    returned (``AtlasSnapshot.reused``)."""
    from reva.adapters.postgres import repositories as repo
    from reva.evidence.locks import take_evidence_snapshot_lock

    if origin not in ("live", "test_fixture"):
        raise ValidationProblem("atlas snapshot origin must be live or test_fixture", origin=origin)
    with engine.begin() as conn:
        take_evidence_snapshot_lock(conn)  # waits for in-flight dataset registrations
        cutoff: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        tables = atlas_tables_as_of(conn, cutoff=cutoff, origin=origin)
        if skip_unchanged:
            ref = latest_snapshot_ref(conn, origin)
            if ref is not None:
                prev = _load_manifest(store, ref["sha"])
                if prev.get("origin") == origin and _pins(prev["tables"]) == _pins(tables):
                    return AtlasSnapshot(
                        ref["sha"],
                        origin,
                        datetime.fromisoformat(prev["knowledge_cutoff"]),
                        tuple(prev["tables"]),
                        ref["activity_id"],
                        reused=True,
                    )
        manifest = {
            "schema": SNAPSHOT_SCHEMA,
            "origin": origin,
            "knowledge_cutoff": cutoff.isoformat(),
            "selection_rule": "per atlas dataset_key: latest evidence.dataset version with recorded_at <= cutoff",
            "tables": tables,
        }
        sha = repo.store_json_artifact(
            conn,
            store,
            manifest,
            NAMESPACE,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            rights={"licence": "derived; each pinned table keeps its source rights"},
        )
        act = repo.simple_activity(
            ACTIVITY_KIND,
            "normalisation",
            actor,
            {"atlas_origin": origin, "cutoff": cutoff.isoformat(), "snapshot_sha256": sha, "tables": len(tables)},
        )
        repo.record_activity(
            conn,
            act,
            inputs=[(t["artifact_sha256"], {"role": "atlas_table", "dataset_key": t["dataset_key"]}) for t in tables],
            outputs=[sha],
        )
    return AtlasSnapshot(sha, origin, cutoff, tuple(tables), str(act.id))


def _load_manifest(store: ArtifactStore, sha: str) -> dict[str, Any]:
    data = store.get(NAMESPACE, sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise IntegrityFailure("atlas snapshot manifest bytes do not match their sha256", sha256=sha)
    doc = json.loads(data)
    if doc.get("schema") != SNAPSHOT_SCHEMA:
        raise IntegrityFailure("artifact is not an atlas snapshot manifest", sha256=sha)
    return doc  # type: ignore[no-any-return]


LATEST_SQL = """
SELECT id::text AS activity_id, parameters->>'snapshot_sha256' AS sha, parameters->>'atlas_origin' AS origin
FROM meta.activity
WHERE kind = :k AND (CAST(:o AS text) IS NULL OR parameters->>'atlas_origin' = :o)
  AND (CAST(:sha AS text) IS NULL OR parameters->>'snapshot_sha256' = :sha)
ORDER BY (parameters->>'cutoff')::timestamptz DESC, recorded_at DESC
LIMIT 1
"""


def latest_snapshot_ref(
    conn: Connection, origin: str | None = None, sha256: str | None = None
) -> dict[str, Any] | None:
    row = conn.execute(text(LATEST_SQL), {"k": ACTIVITY_KIND, "o": origin, "sha": sha256}).mappings().one_or_none()
    return dict(row) if row else None


def resolve_snapshot(
    conn: Connection, store: ArtifactStore, *, origin: str | None = None, sha256: str | None = None
) -> AtlasSnapshot:
    """The requested (or latest, optionally per origin) recorded snapshot; ``NotFound`` if none."""
    ref = latest_snapshot_ref(conn, origin, sha256)
    if ref is None:
        raise SnapshotUnavailable(
            "no atlas snapshot recorded" + (f" for origin {origin}" if origin else ""),
            origin=origin,
            snapshot=sha256,
        )
    doc = _load_manifest(store, ref["sha"])
    if doc["origin"] != ref["origin"]:
        raise IntegrityFailure("atlas snapshot manifest origin differs from its activity", sha256=ref["sha"])
    return AtlasSnapshot(
        ref["sha"],
        doc["origin"],
        datetime.fromisoformat(doc["knowledge_cutoff"]),
        tuple(doc["tables"]),
        ref["activity_id"],
    )


def take_snapshot(
    settings: Any,
    engine: Engine,
    store: ArtifactStore,
    *,
    origin: str = "live",
    warm: bool = False,
    skip_unchanged: bool = False,
) -> dict[str, Any]:
    """Record (or reuse) a snapshot; ``warm`` also builds the local DuckDB query file.

    A fixture-origin snapshot is refused in production (review ATLAS-5)."""
    if origin == "test_fixture" and getattr(settings, "reva_env", "") == "production":
        from reva.kernel.errors import PolicyDenied

        raise PolicyDenied("fixture atlas snapshots are not allowed in production")
    snap = create_atlas_snapshot(engine, store, origin=origin, skip_unchanged=skip_unchanged)
    out: dict[str, Any] = {**snap.ref(), "reused": snap.reused, "activity_id": snap.activity_id}
    if warm:
        from reva.atlas.query import build_snapshot_db

        out["query_file"] = str(build_snapshot_db(snap, store))
    return out


def handle_atlas_snapshot(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Worker job handler (job kind ``atlas_snapshot``; payload ``{"origin", "warm", "skip_unchanged"}``).

    Scheduled daily after the atlas ingests; with ``skip_unchanged`` (default for jobs) a day
    without new table versions records nothing."""
    origin = str(payload.get("origin", "live"))
    if origin not in ("live", "test_fixture"):
        raise ValidationProblem("atlas snapshot origin must be live or test_fixture", origin=origin[:32])
    return take_snapshot(
        ctx.settings,
        ctx.worker_engine,
        ctx.store,
        origin=origin,
        warm=bool(payload.get("warm", False)),
        skip_unchanged=bool(payload.get("skip_unchanged", True)),
    )


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - operator entry point
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="Record an atlas snapshot (worker credentials).")
    ap.add_argument("--origin", choices=("live", "test_fixture"), default="live")
    ap.add_argument("--warm", action="store_true", help="also build the local DuckDB query file now")
    ap.add_argument("--skip-unchanged", action="store_true", help="reuse the latest snapshot if no table changed")
    args = ap.parse_args(argv)
    settings = get_settings()
    out = take_snapshot(
        settings,
        make_engine("worker", settings),
        get_store(),
        origin=args.origin,
        warm=args.warm,
        skip_unchanged=args.skip_unchanged,
    )
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
