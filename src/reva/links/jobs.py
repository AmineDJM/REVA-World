"""Worker job kind ``links_materialise`` (agent LINKWIRE): the materialised link store, scheduled daily.

Payload ``{"atlas_origin": "live"|"test_fixture"|null, "atlas_snapshot": sha256|null,
"kg_origin": "live"|"test_fixture"|null, "top_k": 1..100, "with_atlas": bool, "skip_unchanged": bool}``.

The job resolves its inputs first (atlas snapshot, KG watermark = highest ``kg.load`` id, taxonomy sha256,
method version, ``top_k``); with ``skip_unchanged`` (default for jobs) a materialisation of exactly those
inputs that already exists is reused and nothing is recorded (the store is append-only and ~1 KB per link).
Otherwise :func:`reva.links.store.materialise` derives every link and writes it in one worker transaction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from reva.kernel.errors import ValidationProblem
from reva.links import store as link_store

ORIGINS = ("live", "test_fixture")


def _origin(payload: dict[str, Any], key: str, default: str | None) -> str | None:
    v = payload.get(key, default)
    if v is not None and v not in ORIGINS:
        raise ValidationProblem(f"{key} must be live, test_fixture or null", **{key: str(v)[:32]})
    return v


def parse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"atlas_origin", "atlas_snapshot", "kg_origin", "top_k", "with_atlas", "skip_unchanged"}
    if unknown:
        raise ValidationProblem("unknown links_materialise payload keys", keys=sorted(unknown)[:10])
    sha = payload.get("atlas_snapshot")
    if sha is not None and (
        not isinstance(sha, str) or len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha)
    ):
        raise ValidationProblem("atlas_snapshot must be a sha256 hex digest")
    top_k = payload.get("top_k", link_store.DEFAULT_TOP_K)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValidationProblem("top_k must be an integer within 1..100")
    return {
        "atlas_origin": _origin(payload, "atlas_origin", "live"),
        "atlas_sha256": sha,
        "kg_origin": _origin(payload, "kg_origin", "live"),
        "top_k": top_k,
        "with_atlas": bool(payload.get("with_atlas", True)),
        "skip_unchanged": bool(payload.get("skip_unchanged", True)),
    }


def existing_materialisation(
    conn: Any, *, atlas_sha256: str | None, kg_origin: str | None, kg_watermark: int | None, top_k: int
) -> int | None:
    """Id of a materialisation of exactly these inputs (method, taxonomy, snapshot, KG origin + watermark, top-k)."""
    try:
        row = conn.execute(
            text(
                """SELECT id FROM kg.link_materialisation
                    WHERE method_version = :mv AND taxonomy_sha256 = :ts
                      AND atlas_snapshot_sha256 IS NOT DISTINCT FROM :sha
                      AND kg_origin IS NOT DISTINCT FROM :ko
                      AND kg_load_watermark IS NOT DISTINCT FROM :wm
                      AND (parameters->>'top_k')::int = :k
                    ORDER BY id DESC LIMIT 1"""
            ),
            {
                "mv": link_store.METHOD_VERSION,
                "ts": link_store.taxonomy_sha256(),
                "sha": atlas_sha256,
                "ko": kg_origin,
                "wm": kg_watermark,
                "k": top_k,
            },
        ).scalar_one_or_none()
    except Exception as e:  # store table absent (database before 0012): materialise reports it
        if "link_materialisation" in str(e):
            conn.rollback()
            return None
        raise
    return None if row is None else int(row)


def handle_links_materialise(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Worker job handler (job kind ``links_materialise``); scheduled daily after ``atlas_snapshot``."""
    p = parse_payload(payload)
    sha = p["atlas_sha256"]
    with ctx.worker_engine.connect() as conn:
        if p["with_atlas"] and sha is None:
            from reva.atlas.snapshot import SnapshotUnavailable, resolve_snapshot

            try:
                sha = resolve_snapshot(conn, ctx.store, origin=p["atlas_origin"]).sha256
            except SnapshotUnavailable:
                sha = None  # KG kinds only (materialise logs it); recorded as such in the store
        wm_id, _ = link_store.kg_watermark(conn, p["kg_origin"])
        same = (
            existing_materialisation(
                conn,
                atlas_sha256=sha if p["with_atlas"] else None,
                kg_origin=p["kg_origin"],
                kg_watermark=wm_id,
                top_k=p["top_k"],
            )
            if p["skip_unchanged"]
            else None
        )
    inputs = {
        "atlas_snapshot_sha256": sha if p["with_atlas"] else None,
        "kg_origin": p["kg_origin"],
        "kg_load_watermark": wm_id,
        "top_k": p["top_k"],
        "method_version": link_store.METHOD_VERSION,
    }
    if same is not None:
        return {"reused": True, "materialisation_id": same, "inputs": inputs}
    res = link_store.materialise(
        ctx.worker_engine,
        ctx.store,
        atlas_origin=p["atlas_origin"],
        atlas_sha256=sha if p["with_atlas"] else None,
        kg_origin=p["kg_origin"],
        top_k=p["top_k"],
        with_atlas=p["with_atlas"],
    )
    out = res.to_json()
    out["report"] = {"counts": res.report.get("counts")}
    return {"reused": False, **out, "inputs": inputs}
