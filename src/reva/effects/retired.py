"""Reasons for effect-estimate retractions recorded before the loader copied them (AUDIT4 L5, FIX6).

``kg.effect_estimate_retraction`` is append-only with one row per retracted estimate row, and
``kg.retraction_annotation`` only annotates KG statement / causal-claim retractions (no migration may widen it).
The curator's retirement records (``retired:`` in a ``registry/effects/literature/*.yaml`` entry: reason and
replacing estimate ids) are therefore appended for the existing retractions as one content-addressed JSON artifact
(schema ``reva-effect-retraction-annotation/1``) with a ``meta.activity`` that cites it. Nothing is edited; running it
again appends nothing (rows already annotated with the same record are skipped).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import text

from reva.effects.curation import Entry

ANNOTATION_SCHEMA = "reva-effect-retraction-annotation/1"
ACTIVITY_KIND = "effects.retraction_annotation"


def pending_annotations(conn: Any, entries: Iterable[Entry], *, origin: str) -> list[dict[str, Any]]:
    """Existing retractions of retired estimates whose stored detail lacks the retirement record and that no
    earlier annotation activity covers."""
    want: dict[str, dict[str, Any]] = {}
    for e in entries:
        want.update(e.retired_by_estimate_key())
    if not want:
        return []
    done = {
        int(x)
        for x in conn.execute(
            text("""SELECT jsonb_array_elements_text(parameters->'retraction_ids') FROM meta.activity
                    WHERE kind = :k"""),
            {"k": ACTIVITY_KIND},
        ).scalars()
    }
    rows = conn.execute(
        text("""SELECT id, estimate_key, reason, detail, load_id FROM kg.effect_estimate_retraction
                WHERE origin = :o AND estimate_key = ANY(:k) ORDER BY id"""),
        {"o": origin, "k": sorted(want)},
    ).mappings()
    out = []
    for r in rows:
        if int(r["id"]) in done or (isinstance(r["detail"], Mapping) and "retired" in r["detail"]):
            continue
        out.append(
            {
                "retraction_id": int(r["id"]),
                "estimate_key": r["estimate_key"],
                "load_id": int(r["load_id"]),
                "recorded_reason": r["reason"],
                "recorded_detail": r["detail"],
                "retired": want[r["estimate_key"]],
            }
        )
    return out


def annotate_existing_retractions(
    engine: Any, store: Any, entries: Iterable[Entry], *, origin: str = "live", actor: str = "agent:FIX6"
) -> dict[str, Any]:
    from reva.adapters.postgres import repositories as repo

    with engine.begin() as conn:
        todo = pending_annotations(conn, entries, origin=origin)
        if not todo:
            return {"annotated": 0}
        doc = {"schema": ANNOTATION_SCHEMA, "origin": origin, "audit": "AUDIT4 L5", "annotations": todo}
        sha = repo.store_json_artifact(conn, store, doc, "evidence", schema_version=ANNOTATION_SCHEMA)
        act = repo.simple_activity(
            ACTIVITY_KIND,
            "review",
            actor,
            {"retraction_ids": [t["retraction_id"] for t in todo], "artifact_sha256": sha},
        )
        repo.record_activity(conn, act, outputs=[sha])
    return {"annotated": len(todo), "artifact_sha256": sha, "activity_id": str(act.id), "rows": todo}


__all__ = ["ACTIVITY_KIND", "ANNOTATION_SCHEMA", "annotate_existing_retractions", "pending_annotations"]
