"""Pinned belief releases and their estimates (release-pinned reads only)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Connection, text

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import (
    Timer,
    bounded_source_flags,
    decode_cursor,
    freshness,
    link,
    origin_warnings,
    page,
    page_limit,
    read_conn,
    release_ref,
    require_aware,
    support_summary,
    uncertainty_summary,
    units_of,
    valid_window,
)
from reva.api.errors import ApiProblem, NotFound
from reva.api.routes.world import resolve_entity
from reva.belief.snapshots import release_as_of, release_by_id
from reva.world import queries as wq

router = APIRouter(prefix="/v1", tags=["beliefs"], dependencies=[Depends(require_scope("read"))])


def resolve_release(
    conn: Connection, release_id: UUID | None, as_of: datetime | None, origin: str | None = None
) -> dict[str, Any]:
    """Explicit release, else the release published at/before ``as_of``, else the latest."""
    if release_id is not None:
        rel = release_by_id(conn, release_id)
        if rel is None:
            raise NotFound("belief release not found", release_id=str(release_id))
        if rel.get("sequence") is None:
            raise ApiProblem(409, "release_not_published", "belief release exists but is not published")
        return rel
    rel = release_as_of(conn, as_of, origin)
    if rel is None:
        raise ApiProblem(
            404,
            "no_release",
            "no belief release was published at or before the requested time"
            if as_of
            else "no belief release has been published",
            as_of=as_of.isoformat() if as_of else None,
        )
    return rel


ESTIMATE_SQL = """
SELECT e.id, e.release_id, e.subject_id, ent.label AS subject_label, ek.concept_id AS subject_kind,
       c.concept_id, c.version AS concept_version, e.modality, e.value, e.uncertainty, e.alternatives,
       e.discrepancy_flags, sp.id AS support_id, sp.temporal_extent, sp.spatial_spec, sp.population_spec,
       sp.aggregation, mv.id AS method_id, mv.model_key, mv.version AS method_version,
       mv.approval_status, inp.observation_id, inp.revision_no, inp.source_key, inp.retrieved_at,
       inp.source_flags,
       COALESCE(sp.temporal_extent->>'start', '') AS sort_start
FROM belief.estimate e
JOIN world.entity ent ON ent.id = e.subject_id
JOIN ontology.concept_version ek ON ek.id = ent.kind_concept_id
JOIN ontology.concept_version c ON c.id = e.concept_version_id
JOIN world.support sp ON sp.id = e.support_id
JOIN model.version mv ON mv.id = e.method_version_id
LEFT JOIN LATERAL (
  SELECT i.observation_id, o.revision_no, src.source_key, a.retrieved_at, o.source_flags
  FROM belief.input i
  JOIN evidence.observation o ON o.id = i.observation_id
  JOIN evidence.source src ON src.id = o.source_id
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  WHERE i.estimate_id = e.id ORDER BY i.id LIMIT 1
) inp ON true
WHERE e.release_id = :r
  AND (CAST(:subjects AS uuid[]) IS NULL OR e.subject_id = ANY(:subjects))
  AND (CAST(:concepts AS text[]) IS NULL OR c.concept_id = ANY(:concepts))
  AND (CAST(:y AS integer) IS NULL OR EXTRACT(YEAR FROM lower(sp.valid_range)) = :y)
  AND (CAST(:c0 AS text) IS NULL
       OR (c.concept_id COLLATE "C", e.subject_id::text COLLATE "C",
           COALESCE(sp.temporal_extent->>'start', '') COLLATE "C", e.id)
          > (CAST(:c0 AS text) COLLATE "C", CAST(:c1 AS text) COLLATE "C", CAST(:c2 AS text) COLLATE "C",
             CAST(:c3 AS uuid)))
ORDER BY c.concept_id COLLATE "C", e.subject_id::text COLLATE "C",
         COALESCE(sp.temporal_extent->>'start', '') COLLATE "C", e.id
LIMIT :lim
"""


def _uuid_or_none(v: Any) -> UUID | None:
    if v is None or isinstance(v, UUID):
        return v
    try:
        return UUID(str(v))
    except ValueError:
        return None


def estimate_model(r: dict[str, Any], mark: dict[str, Any] | None = None) -> s.BeliefEstimate:
    flags, flags_cut = bounded_source_flags(r.get("source_flags"))
    return s.BeliefEstimate(
        id=r["id"],
        release_id=r["release_id"],
        subject=s.EntityRef(id=r["subject_id"], label=r["subject_label"], kind=r["subject_kind"]),
        concept_id=r["concept_id"],
        concept_version=r["concept_version"],
        support=support_summary(r),
        modality=r["modality"],
        value=r["value"],
        uncertainty=r["uncertainty"],
        alternatives=r["alternatives"],
        discrepancy_flags=r["discrepancy_flags"],
        source_key=r["source_key"],
        observation_id=r["observation_id"],
        observation_revision_no=r["revision_no"],
        retrieved_at=r["retrieved_at"],
        source_flags=flags,
        source_flags_truncated=flags_cut,
        stale=mark is not None,
        stale_reason=None if mark is None else mark.get("reason"),
        proposal_id=None if mark is None else _uuid_or_none(mark.get("proposal_id")),
        served_from_release_id=r.get("served_from_release_id", r["release_id"]),
        method=s.ModelVersionRef(
            id=r["method_id"],
            model_key=r["model_key"],
            version=r["method_version"],
            approval_status=r["approval_status"],
        ),
    )


def beliefs_response(
    conn: Connection,
    t: Timer,
    *,
    release_id: UUID | None,
    as_of: datetime | None,
    origin: str | None,
    subjects: list[UUID] | None,
    concepts: list[str] | None,
    year: int | None,
    cursor: str | None,
    limit: int,
) -> s.BeliefListResponse:
    rel = resolve_release(conn, release_id, as_of, origin)
    cur = decode_cursor(cursor, "sssu")

    def fetch(release_id: Any, concept_filter: list[str] | None) -> list[dict[str, Any]]:
        return [
            dict(r, served_from_release_id=release_id)
            for r in conn.execute(
                text(ESTIMATE_SQL),
                {
                    "r": release_id,
                    "subjects": subjects or None,
                    "concepts": concept_filter or None,
                    "y": year,
                    "c0": cur[0] if cur else None,
                    "c1": cur[1] if cur else None,
                    "c2": cur[2] if cur else None,
                    "c3": cur[3] if cur else None,
                    "lim": limit + 1,
                },
            ).mappings()
        ]

    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        # Byte order (COLLATE "C" in ESTIMATE_SQL) so the merged order equals the SQL keyset order.
        return (r["concept_id"], str(r["subject_id"]), r["sort_start"], r["id"])

    rows = fetch(rel["id"], concepts)
    # Publication pauses (connector repair): a concept the release withheld is served from the last earlier
    # release that published it, marked stale; an active pause marks the pinned values stale. Every source
    # query uses the same keyset cursor and order, so merging them keeps pagination exact.
    marks = wq.publication_staleness(conn, rel, as_of=as_of)
    served_from: dict[str, dict[str, Any]] = {}
    for concept, mark in sorted(marks.items()):
        if not mark.get("withheld_in_release") or (concepts and concept not in concepts):
            continue
        prev = wq.last_release_with_concept(conn, rel, concept)
        if prev is None:
            continue
        served_from[concept] = prev
        have = {(r["subject_id"], r["support_id"]) for r in rows if r["concept_id"] == concept}
        rows += [r for r in fetch(prev["id"], [concept]) if (r["subject_id"], r["support_id"]) not in have]
    if served_from:
        rows.sort(key=key)
    rows, nxt = page(rows, limit, key)
    method = (
        conn.execute(
            text(
                "SELECT id, model_key, version, approval_status, assumptions FROM model.version "
                "WHERE model_key || '@' || version = :m"
            ),
            {"m": rel["method"]},
        )
        .mappings()
        .one_or_none()
    )
    data = [estimate_model(r, marks.get(r["concept_id"])) for r in rows]
    warnings = origin_warnings(rel["origin"])
    if marks:
        parts = []
        for concept, mark in sorted(marks.items()):
            how = (
                f"withheld from this release; serving release #{served_from[concept]['sequence']} as stale"
                if concept in served_from
                else "withheld from this release; no earlier release published it"
                if mark.get("withheld_in_release")
                else f"paused since {mark.get('paused_since')}; values are stale"
            )
            parts.append(f"{concept} ({mark.get('reason')}): {how}")
        warnings.append(
            s.ApiWarning(
                code="publication_paused",
                message="Belief publication is paused pending a reviewed connector repair: " + "; ".join(parts)[:1500],
            )
        )
    n_missing = sum(1 for d in data if d.value.kind == "missing")
    if n_missing:
        warnings.append(
            s.ApiWarning(code="missing_values", message=f"{n_missing} estimate(s) are typed missing (never zero)")
        )
    flags = Counter(f for d in data for f in d.discrepancy_flags)
    sources = Counter(d.source_key for d in data if d.source_key)
    assumptions = list(method["assumptions"]) if method and isinstance(method["assumptions"], list) else []
    return s.BeliefListResponse(
        data=data,
        context_id=rel["context_id"],
        context_mode="belief",
        origin=rel["origin"],
        modality="selected_estimate",
        units=units_of(r["value"] for r in rows),
        baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"],
        valid_time=valid_window(r["temporal_extent"] for r in rows),
        model_versions=[
            s.ModelVersionRef(
                id=method["id"],
                model_key=method["model_key"],
                version=method["version"],
                approval_status=method["approval_status"],
            )
        ]
        if method
        else [],
        assumptions=assumptions,
        uncertainty=uncertainty_summary(r["uncertainty"] for r in rows),
        data_freshness=freshness((r["retrieved_at"] for r in rows), []),
        evidence_profile=s.Assessment(status="reported", detail={"selected_from_sources": dict(sources)}),
        applicability=s.Assessment(
            status="reported",
            detail={"method": rel["method"], "note": "selected source reports; no imputation or averaging"},
        ),
        model_disagreement=s.Assessment(status="reported", detail={"discrepancy_flags": dict(flags)})
        if flags
        else s.Assessment(status="not_assessed", reason="no alternative eligible source in this page"),
        historical_validation=s.Assessment(status="not_applicable", reason="selected source reports are not forecasts"),
        provenance_links=[link("release", rel["id"])]
        + [link("release", p["id"]) for p in served_from.values()]
        + [link("estimate", d.id) for d in data[:50]],
        warnings=warnings,
        cost=t.cost(len(rows)),
        next_cursor=nxt,
    )


@router.get("/beliefs", response_model=s.BeliefListResponse, responses={404: {"model": s.Problem}})
def list_beliefs(
    request: Request,
    release_id: UUID | None = Query(None, description="Pin an explicit release"),
    as_of: datetime | None = Query(None, description="Use the release published at/before this instant"),
    origin: str | None = Query(None, pattern="^(live|test_fixture)$"),
    subject_id: str | None = Query(None, max_length=64, description="Entity UUID or ISO3 key"),
    concept: str | None = Query(None, max_length=200),
    period: int | None = Query(None, ge=1, le=9999),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.BeliefListResponse:
    """Pinned selected/estimated state from one published release."""
    t = Timer()
    require_aware(as_of, "as_of")
    with read_conn(request) as conn:
        subj = [resolve_entity(conn, subject_id)] if subject_id else None
        return beliefs_response(
            conn,
            t,
            release_id=release_id,
            as_of=as_of,
            origin=origin,
            subjects=subj,
            concepts=[concept] if concept else None,
            year=period,
            cursor=cursor,
            limit=limit,
        )


RELEASE_LIST = """
SELECT r.*, p.sequence, p.published_at FROM belief.release r
JOIN ops.publication p ON p.release_id = r.id
WHERE (CAST(:as_of AS timestamptz) IS NULL OR p.published_at <= :as_of)
  AND (CAST(:origin AS text) IS NULL OR r.origin = :origin)
  AND (CAST(:c0 AS bigint) IS NULL OR p.sequence < CAST(:c0 AS bigint))
ORDER BY p.sequence DESC LIMIT :lim
"""


def release_model(conn: Connection, rel: dict[str, Any]) -> s.BeliefRelease:
    concepts = [
        s.ReleaseConceptCount(concept_id=r["concept_id"], estimates=r["n"], missing=r["missing"])
        for r in conn.execute(
            text(
                """SELECT c.concept_id, count(*) AS n, count(*) FILTER (WHERE e.value->>'kind' = 'missing')
                              AS missing
                       FROM belief.estimate e JOIN ontology.concept_version c ON c.id = e.concept_version_id
                       WHERE e.release_id = :r GROUP BY c.concept_id ORDER BY c.concept_id"""
            ),
            {"r": rel["id"]},
        ).mappings()
    ]
    arts = [
        s.ReleaseArtifact(sha256=r["artifact_sha256"], role=r["role"])
        for r in conn.execute(
            text("SELECT artifact_sha256, role FROM belief.release_artifact WHERE release_id = :r ORDER BY role"),
            {"r": rel["id"]},
        ).mappings()
    ]
    arts += [
        s.ReleaseArtifact(sha256=rel["manifest_sha256"], role="release_manifest"),
        s.ReleaseArtifact(sha256=rel["ontology_manifest_sha256"], role="ontology_manifest"),
        s.ReleaseArtifact(sha256=rel["model_manifest_sha256"], role="model_manifest"),
    ]
    return s.BeliefRelease(
        id=rel["id"],
        sequence=rel.get("sequence"),
        published_at=rel.get("published_at"),
        knowledge_cutoff=rel["knowledge_cutoff"],
        origin=rel["origin"],
        method=rel["method"],
        context_id=rel["context_id"],
        manifest_sha256=rel["manifest_sha256"],
        ontology_manifest_sha256=rel["ontology_manifest_sha256"],
        model_manifest_sha256=rel["model_manifest_sha256"],
        recorded_at=rel["recorded_at"],
        estimate_count=sum(c.estimates for c in concepts),
        concepts=concepts,
        artifacts=arts,
    )


@router.get("/belief-releases", response_model=s.BeliefReleaseListResponse)
def list_releases(
    request: Request,
    as_of: datetime | None = Query(None, description="Only releases published at/before this instant"),
    origin: str | None = Query(None, pattern="^(live|test_fixture)$"),
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> s.BeliefReleaseListResponse:
    """Published releases, newest publication sequence first."""
    t = Timer()
    require_aware(as_of, "as_of")
    cur = decode_cursor(cursor, "i")
    with read_conn(request) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(RELEASE_LIST), {"as_of": as_of, "origin": origin, "c0": cur[0] if cur else None, "lim": limit + 1}
            ).mappings()
        ]
        rows, nxt = page(rows, limit, lambda r: (r["sequence"],))
        data = [release_model(conn, r) for r in rows]
    origins = {d.origin for d in data}
    org = "none" if not origins else (origins.pop() if len(origins) == 1 else "mixed")
    return s.BeliefReleaseListResponse(
        data=data,
        context_mode="belief",
        origin=org,
        modality="metadata",  # type: ignore[arg-type]
        knowledge_cutoff=as_of,
        warnings=origin_warnings(org),
        cost=t.cost(len(data)),
        next_cursor=nxt,
    )


@router.get(
    "/belief-releases/{release_id}", response_model=s.BeliefReleaseResponse, responses={404: {"model": s.Problem}}
)
def get_release(release_id: UUID, request: Request) -> s.BeliefReleaseResponse:
    t = Timer()
    with read_conn(request) as conn:
        rel = resolve_release(conn, release_id, None)
        data = release_model(conn, rel)
    return s.BeliefReleaseResponse(
        data=data,
        context_id=rel["context_id"],
        context_mode="belief",
        origin=rel["origin"],
        modality="metadata",
        baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"],
        model_versions=[s.ModelVersionRef(model_key=rel["method"].split("@")[0], version=rel["method"].split("@")[-1])],
        provenance_links=[link("release", rel["id"])],
        warnings=origin_warnings(rel["origin"]),
        cost=t.cost(1),
    )
