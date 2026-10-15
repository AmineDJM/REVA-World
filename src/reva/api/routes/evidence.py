"""Source records (observations, timeseries), evidence cards and disagreement."""

from __future__ import annotations

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
    origin_of,
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

router = APIRouter(prefix="/v1", tags=["evidence"], dependencies=[Depends(require_scope("read"))])

MAX_SERIES_ROWS = 5000

# Filtering (cutoff, retraction-independent lineage filters) happens inside the
# CTE *before* revision selection: a later correction can never change an
# earlier as-of answer.
OBS_SQL = """
WITH visible AS (
  SELECT o.*,
         row_number() OVER (PARTITION BY o.source_id, o.logical_key ORDER BY o.revision_no DESC) AS rn
  FROM evidence.observation o
  JOIN ontology.concept_version c ON c.id = o.concept_version_id
  JOIN evidence.source src ON src.id = o.source_id
  JOIN world.support sp ON sp.id = o.support_id
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  WHERE (CAST(:as_of AS timestamptz) IS NULL OR o.recorded_at <= :as_of)
    AND (CAST(:subject AS uuid) IS NULL OR o.subject_id = :subject)
    AND (CAST(:concept AS text) IS NULL OR c.concept_id = :concept)
    AND (CAST(:source AS text) IS NULL OR src.source_key = :source)
    AND (CAST(:logical_key AS text) IS NULL OR o.logical_key = :logical_key)
    AND (CAST(:aggregation AS text) IS NULL OR sp.aggregation = :aggregation)
    AND (CAST(:origin AS text) IS NULL OR a.origin = :origin)
    AND (CAST(:acq AS uuid) IS NULL OR o.acquisition_id = :acq)
    AND (CAST(:srev AS text) IS NULL OR o.source_revision_id = :srev)
    AND (CAST(:y0 AS integer) IS NULL OR EXTRACT(YEAR FROM lower(sp.valid_range)) >= :y0)
    AND (CAST(:y1 AS integer) IS NULL OR EXTRACT(YEAR FROM lower(sp.valid_range)) <= :y1)
)
SELECT v.id, v.logical_key, v.revision_no, v.supersedes_id, v.source_revision_id, v.subject_id,
       ent.label AS subject_label, ek.concept_id AS subject_kind, c.concept_id, c.version AS concept_version,
       v.value, v.uncertainty, v.source_flags, v.observed_at, v.source_published_at, v.source_available_at,
       v.availability_basis, v.raw_selector, v.parser_version, v.recorded_at, v.acquisition_id,
       a.retrieved_at, a.origin, src.source_key, cv.name AS connector, cv.version AS connector_version,
       sp.id AS support_id, sp.temporal_extent, sp.spatial_spec, sp.population_spec, sp.aggregation,
       (SELECT o2.id FROM evidence.observation o2 WHERE o2.supersedes_id = v.id
          AND (CAST(:as_of AS timestamptz) IS NULL OR o2.recorded_at <= :as_of)) AS superseded_by_id,
       EXISTS (SELECT 1 FROM evidence.observation_retraction t WHERE t.observation_id = v.id
          AND (CAST(:as_of AS timestamptz) IS NULL OR t.recorded_at <= :as_of)) AS retracted
FROM visible v
JOIN world.entity ent ON ent.id = v.subject_id
JOIN ontology.concept_version ek ON ek.id = ent.kind_concept_id
JOIN ontology.concept_version c ON c.id = v.concept_version_id
JOIN evidence.source src ON src.id = v.source_id
JOIN evidence.connector_version cv ON cv.id = v.connector_version_id
JOIN evidence.acquisition a ON a.id = v.acquisition_id
JOIN world.support sp ON sp.id = v.support_id
WHERE (:all_revisions OR v.rn = 1)
  AND (CAST(:c0 AS text) IS NULL
       OR (v.logical_key, v.revision_no, v.id) > (CAST(:c0 AS text), CAST(:c1 AS integer), CAST(:c2 AS uuid)))
ORDER BY v.logical_key, v.revision_no, v.id
LIMIT :lim
"""


def obs_params(**kw: Any) -> dict[str, Any]:
    base = {
        k: None
        for k in (
            "as_of",
            "subject",
            "concept",
            "source",
            "logical_key",
            "aggregation",
            "origin",
            "acq",
            "srev",
            "y0",
            "y1",
            "c0",
            "c1",
            "c2",
        )
    }
    base["all_revisions"] = False
    base.update(kw)
    return base


def observation_model(r: dict[str, Any]) -> s.Observation:
    return s.Observation(
        id=r["id"],
        source_key=r["source_key"],
        connector=r["connector"],
        connector_version=r["connector_version"],
        acquisition_id=r["acquisition_id"],
        origin=r["origin"],
        logical_key=r["logical_key"],
        revision_no=r["revision_no"],
        supersedes_id=r["supersedes_id"],
        superseded_by_id=r["superseded_by_id"],
        source_revision_id=r["source_revision_id"],
        subject=s.EntityRef(id=r["subject_id"], label=r["subject_label"], kind=r["subject_kind"]),
        concept_id=r["concept_id"],
        concept_version=r["concept_version"],
        value=r["value"],
        uncertainty=r["uncertainty"],
        source_flags=r["source_flags"],
        support=support_summary(r),
        observed_at=r["observed_at"],
        source_published_at=r["source_published_at"],
        source_available_at=r["source_available_at"],
        availability_basis=r["availability_basis"],
        retrieved_at=r["retrieved_at"],
        recorded_at=r["recorded_at"],
        raw_selector=r["raw_selector"],
        parser_version=r["parser_version"],
        retracted=bool(r["retracted"]),
    )


OBS_ASSUMPTIONS = [
    "A source report is not certain reality",
    "Values are as reported by the provider, with the provider's units and basis",
]


@router.get("/observations", response_model=s.ObservationListResponse)
def list_observations(
    request: Request,
    subject_id: str | None = Query(None, max_length=64, description="Entity UUID or ISO3 key"),
    concept: str | None = Query(None, max_length=200),
    source: str | None = Query(None, max_length=200, description="Source key"),
    logical_key: str | None = Query(None, max_length=500),
    aggregation: str | None = Query(None, max_length=50, description="Support aggregation filter"),
    origin: str | None = Query(None, pattern="^(live|test_fixture)$"),
    acquisition_id: UUID | None = Query(None, description="Vintage: records delivered by one acquisition"),
    source_revision_id: str | None = Query(None, max_length=200, description="Vintage: provider revision label"),
    period_start: int | None = Query(None, ge=1, le=9999),
    period_end: int | None = Query(None, ge=1, le=9999),
    as_of: datetime | None = Query(
        None, description="Only records committed at/before this instant; applied before revision selection"
    ),
    revisions: str = Query("latest", pattern="^(latest|all)$"),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.ObservationListResponse:
    """Source records with vintage/support filters and revision lineage."""
    t = Timer()
    require_aware(as_of, "as_of")
    cur = decode_cursor(cursor, "siu")
    with read_conn(request) as conn:
        subj = resolve_entity(conn, subject_id) if subject_id else None
        rows = [
            dict(r)
            for r in conn.execute(
                text(OBS_SQL),
                obs_params(
                    as_of=as_of,
                    subject=subj,
                    concept=concept,
                    source=source,
                    logical_key=logical_key,
                    aggregation=aggregation,
                    origin=origin,
                    acq=acquisition_id,
                    srev=source_revision_id,
                    y0=period_start,
                    y1=period_end,
                    all_revisions=revisions == "all",
                    c0=cur[0] if cur else None,
                    c1=cur[1] if cur else None,
                    c2=cur[2] if cur else None,
                    lim=limit + 1,
                ),
            ).mappings()
        ]
    rows, nxt = page(rows, limit, lambda r: (r["logical_key"], r["revision_no"], r["id"]))
    data = [observation_model(r) for r in rows]
    org = origin_of(r["origin"] for r in rows)
    warnings = origin_warnings(org)
    if any(r["retracted"] for r in rows):
        warnings.append(s.ApiWarning(code="retracted_records", message="Some records were retracted by the source"))
    return s.ObservationListResponse(
        data=data,
        context_mode="evidence",
        origin=org,
        modality="reported_observation",  # type: ignore[arg-type]
        knowledge_cutoff=as_of,
        valid_time=valid_window(r["temporal_extent"] for r in rows),
        units=units_of(r["value"] for r in rows),
        assumptions=OBS_ASSUMPTIONS,
        uncertainty=uncertainty_summary(r["uncertainty"] for r in rows),
        data_freshness=freshness((r["retrieved_at"] for r in rows), (r["recorded_at"] for r in rows)),
        provenance_links=[link("observation", r["id"]) for r in rows[:50]],
        warnings=warnings,
        cost=t.cost(len(rows)),
        next_cursor=nxt,
    )


def concept_info(conn: Connection, concept: str) -> s.ConceptInfo:
    row = (
        conn.execute(
            text(
                """SELECT concept_id, version, kind, definition, semantic_profile FROM ontology.concept_version
           WHERE concept_id = :c ORDER BY recorded_at DESC LIMIT 1"""
            ),
            {"c": concept},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("concept not found", concept=concept)
    sp = row["semantic_profile"] or {}
    return s.ConceptInfo(
        concept_id=row["concept_id"],
        version=row["version"],
        kind=row["kind"],
        definition=row["definition"],
        unit_ref=sp.get("unit_ref"),
        aggregation=sp.get("aggregation"),
        semantic_profile=sp,
    )


def timeseries_response(
    conn: Connection,
    t: Timer,
    *,
    subject: UUID,
    concept: str,
    as_of: datetime | None,
    revisions: str,
    source: str | None = None,
) -> s.TimeseriesResponse:
    info = concept_info(conn, concept)
    ent = (
        conn.execute(
            text(
                """SELECT e.id, e.label, c.concept_id AS kind FROM world.entity e
           JOIN ontology.concept_version c ON c.id = e.kind_concept_id WHERE e.id = :i"""
            ),
            {"i": subject},
        )
        .mappings()
        .one_or_none()
    )
    if ent is None:
        raise NotFound("entity not found", entity=str(subject))
    rows = [
        dict(r)
        for r in conn.execute(
            text(OBS_SQL),
            obs_params(
                as_of=as_of,
                subject=subject,
                concept=concept,
                source=source,
                all_revisions=True,
                lim=MAX_SERIES_ROWS + 1,
            ),
        ).mappings()
    ]
    if len(rows) > MAX_SERIES_ROWS:
        raise ApiProblem(
            422,
            "query_too_large",
            f"series has more than {MAX_SERIES_ROWS} records; filter by source",
            limit=MAX_SERIES_ROWS,
        )
    lineages: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        lineages.setdefault((r["source_key"], r["logical_key"]), []).append(r)
    points: list[s.TimeseriesPoint] = []
    heads: list[dict[str, Any]] = []
    skipped_total = 0
    only_retracted: list[str] = []
    for lin in lineages.values():
        lin.sort(key=lambda r: r["revision_no"])
        # Latest revision visible at as_of that was not retracted at as_of (belief-selection semantics):
        # a retraction is never shown silently as the current value.
        usable = [r for r in lin if not r["retracted"]]
        if not usable:
            only_retracted.append(lin[-1]["logical_key"])
            continue
        head = usable[-1]
        skipped = sum(1 for r in lin if r["retracted"] and r["revision_no"] > head["revision_no"])
        skipped_total += skipped
        heads.append(head)
        temporal = head["temporal_extent"] or {}
        flags, flags_cut = bounded_source_flags(head["source_flags"])
        points.append(
            s.TimeseriesPoint(
                period_label=temporal.get("label") or temporal.get("start"),
                valid_start=temporal.get("start"),
                valid_end=temporal.get("end"),
                temporal_kind=temporal.get("kind", "unknown"),
                aggregation=head["aggregation"],
                support_id=head["support_id"],
                source_key=head["source_key"],
                logical_key=head["logical_key"],
                observation_id=head["id"],
                revision_no=head["revision_no"],
                value=head["value"],
                uncertainty=head["uncertainty"],
                recorded_at=head["recorded_at"],
                retrieved_at=head["retrieved_at"],
                source_published_at=head["source_published_at"],
                origin=head["origin"],
                source_flags=flags,
                source_flags_truncated=flags_cut,
                retracted_revisions_skipped=skipped,
                revisions=[
                    s.RevisionEntry(
                        observation_id=r["id"],
                        revision_no=r["revision_no"],
                        value=r["value"],
                        source_revision_id=r["source_revision_id"],
                        retrieved_at=r["retrieved_at"],
                        recorded_at=r["recorded_at"],
                        source_published_at=r["source_published_at"],
                        retracted=bool(r["retracted"]),
                    )
                    for r in lin
                ]
                if revisions == "all"
                else None,
            )
        )
    points.sort(key=lambda p: (p.valid_start or "", p.source_key, p.logical_key))
    org = origin_of(r["origin"] for r in heads)
    warnings = origin_warnings(org)
    if skipped_total or only_retracted:
        warnings.append(
            s.ApiWarning(
                code="retracted_records",
                message=f"{skipped_total} retracted revision(s) were skipped in favour of the latest non-retracted "
                f"revision; {len(only_retracted)} record(s) with only retracted revisions are omitted"
                + (f" ({', '.join(sorted(only_retracted)[:10])})" if only_retracted else "")
                + ". See /v1/observations?revisions=all for the full lineage.",
            )
        )
    n_missing = sum(1 for p in points if p.value.kind == "missing")
    if n_missing:
        warnings.append(
            s.ApiWarning(code="missing_values", message=f"{n_missing} period(s) are typed missing (never zero)")
        )
    if len({p.source_key for p in points}) > 1:
        warnings.append(
            s.ApiWarning(
                code="multiple_sources", message="Points come from more than one source; definitions may differ"
            )
        )
    starts = [p.valid_start for p in points if p.valid_start]
    ends = [p.valid_end or p.valid_start for p in points if p.valid_start]
    return s.TimeseriesResponse(
        data=s.Timeseries(
            subject=s.EntityRef(**dict(ent)),
            concept=info,
            revisions_mode=revisions,  # type: ignore[arg-type]
            as_of=as_of,
            points=points,
        ),
        context_mode="evidence",
        origin=org,
        modality="reported_observation",  # type: ignore[arg-type]
        estimand={
            "subject_id": str(subject),
            "concept_id": info.concept_id,
            "concept_version": info.version,
            "definition": info.definition,
        },
        units=units_of(r["value"] for r in heads) or ([info.unit_ref] if info.unit_ref else []),
        support={"aggregation": info.aggregation, "spatial": heads[0]["spatial_spec"] if heads else None},
        knowledge_cutoff=as_of,
        valid_time={"start": min(starts), "end": max(e for e in ends if e)} if starts else None,
        assumptions=OBS_ASSUMPTIONS,
        uncertainty=uncertainty_summary(r["uncertainty"] for r in heads),
        data_freshness=freshness((r["retrieved_at"] for r in heads), (r["recorded_at"] for r in heads)),
        provenance_links=[link("observation", p.observation_id) for p in points[:100]],
        warnings=warnings,
        cost=t.cost(len(rows)),
    )


@router.get("/timeseries", response_model=s.TimeseriesResponse, responses={404: {"model": s.Problem}})
def get_timeseries(
    request: Request,
    subject_id: str = Query(..., max_length=64, description="Entity UUID or ISO3 key"),
    concept: str = Query(..., max_length=200),
    source: str | None = Query(None, max_length=200),
    as_of: datetime | None = Query(
        None, description="Records committed at/before this instant; filtering precedes revision selection"
    ),
    revisions: str = Query("latest", pattern="^(latest|all)$"),
) -> s.TimeseriesResponse:
    t = Timer()
    require_aware(as_of, "as_of")
    with read_conn(request) as conn:
        subj = resolve_entity(conn, subject_id)
        return timeseries_response(
            conn, t, subject=subj, concept=concept, as_of=as_of, revisions=revisions, source=source
        )


# ---------------------------------------------------------------------------
# Evidence cards, studies and source disagreement
# ---------------------------------------------------------------------------

EVIDENCE_LIST = """
WITH items AS (
  SELECT DISTINCT ON (cl.claim_key) cl.id, 'scientific_card' AS kind,
         COALESCE(cl.card->>'question', cl.claim_key) AS title, cl.status, cl.evidence_kind,
         cl.card->>'relevance_to_reva' AS summary, cl.recorded_at
  FROM evidence.claim cl ORDER BY cl.claim_key, cl.version DESC
), studies AS (
  SELECT st.id, 'study' AS kind,
         COALESCE(st.metadata->>'title', st.doi, st.id::text) AS title, NULL::text AS status,
         NULL::text AS evidence_kind, st.doi AS summary, st.recorded_at
  FROM evidence.study st
), disagreements AS (
  SELECT e.id, 'source_disagreement' AS kind,
         c.concept_id || ' / ' || ent.label || ' / ' || COALESCE(sp.temporal_extent->>'label', '') AS title,
         NULL::text AS status, NULL::text AS evidence_kind,
         array_to_string(ARRAY(SELECT jsonb_array_elements_text(e.discrepancy_flags)), ', ') AS summary,
         NULL::timestamptz AS recorded_at
  FROM belief.estimate e
  JOIN ontology.concept_version c ON c.id = e.concept_version_id
  JOIN world.entity ent ON ent.id = e.subject_id
  JOIN world.support sp ON sp.id = e.support_id
  WHERE e.release_id = CAST(:release AS uuid) AND jsonb_array_length(e.discrepancy_flags) > 0
), allitems AS (
  SELECT * FROM items UNION ALL SELECT * FROM studies UNION ALL SELECT * FROM disagreements
)
SELECT * FROM allitems
WHERE (CAST(:kind AS text) IS NULL OR kind = :kind)
  AND (CAST(:c0 AS text) IS NULL OR (kind, id) > (CAST(:c0 AS text), CAST(:c1 AS uuid)))
ORDER BY kind, id
LIMIT :lim
"""


@router.get("/evidence", response_model=s.EvidenceListResponse)
def list_evidence(
    request: Request,
    kind: str | None = Query(None, pattern="^(scientific_card|study|source_disagreement)$"),
    release_id: UUID | None = Query(None, description="Release for source disagreement (default latest)"),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.EvidenceListResponse:
    """Scientific cards, study metadata and source disagreements in the pinned release."""
    t = Timer()
    cur = decode_cursor(cursor, "su")
    with read_conn(request) as conn:
        rel = release_as_of(conn, None) if release_id is None else release_by_id(conn, release_id)
        if release_id is not None and rel is None:
            raise NotFound("release not found", release_id=str(release_id))
        rows = [
            dict(r)
            for r in conn.execute(
                text(EVIDENCE_LIST),
                {
                    "release": rel["id"] if rel else None,
                    "kind": kind,
                    "c0": cur[0] if cur else None,
                    "c1": cur[1] if cur else None,
                    "lim": limit + 1,
                },
            ).mappings()
        ]
    rows, nxt = page(rows, limit, lambda r: (r["kind"], r["id"]))
    warnings = []
    if not any(r["kind"] == "scientific_card" for r in rows) and kind in (None, "scientific_card"):
        warnings.append(s.ApiWarning(code="no_cards", message="No scientific evidence cards have been imported"))
    return s.EvidenceListResponse(
        data=[s.EvidenceItem(**r) for r in rows],
        context_mode="evidence",
        origin="none",
        modality="metadata",
        baseline_release=release_ref(rel),
        warnings=warnings,
        cost=t.cost(len(rows)),
        next_cursor=nxt,
    )


@router.get("/evidence/{evidence_id}", response_model=s.EvidenceDetailResponse, responses={404: {"model": s.Problem}})
def get_evidence(evidence_id: UUID, request: Request) -> s.EvidenceDetailResponse:
    t = Timer()
    links: list[s.ProvenanceLink] = []
    with read_conn(request) as conn:
        claim = (
            conn.execute(text("SELECT * FROM evidence.claim WHERE id = :i"), {"i": evidence_id})
            .mappings()
            .one_or_none()
        )
        if claim is not None:
            effects = [
                dict(r)
                for r in conn.execute(
                    text(
                        """SELECT id, study_id, estimand, estimate, uncertainty, dependency_family, source_span
                   FROM evidence.effect WHERE claim_id = :i ORDER BY id"""
                    ),
                    {"i": evidence_id},
                ).mappings()
            ]
            card = claim["card"]
            detail = s.EvidenceDetail(
                id=claim["id"],
                kind="scientific_card",
                title=card.get("question") or claim["claim_key"],
                status=claim["status"],
                evidence_kind=claim["evidence_kind"],
                card=card,
                claim_key=claim["claim_key"],
                claim_version=claim["version"],
                effects=[{k: (str(v) if isinstance(v, UUID) else v) for k, v in e.items()} for e in effects],
                recorded_at=claim["recorded_at"],
            )
            warnings = (
                []
                if claim["status"] == "reviewed"
                else [
                    s.ApiWarning(
                        code="unreviewed_card", message=f"Card status is {claim['status']}; it cannot inform beliefs"
                    )
                ]
            )
            return s.EvidenceDetailResponse(
                data=detail,
                context_mode="evidence",
                origin="none",
                modality="metadata",
                warnings=warnings,
                cost=t.cost(1),
            )
        study = (
            conn.execute(text("SELECT * FROM evidence.study WHERE id = :i"), {"i": evidence_id})
            .mappings()
            .one_or_none()
        )
        if study is not None:
            detail = s.EvidenceDetail(
                id=study["id"],
                kind="study",
                title=(study["metadata"] or {}).get("title") or study["doi"] or "study",
                study={
                    "doi": study["doi"],
                    "metadata": study["metadata"],
                    "source_artifact_sha256": study["source_artifact_sha256"],
                    "acquisition_id": str(study["acquisition_id"]) if study["acquisition_id"] else None,
                },
                recorded_at=study["recorded_at"],
            )
            if study["acquisition_id"]:
                links.append(link("acquisition", study["acquisition_id"]))
            return s.EvidenceDetailResponse(
                data=detail,
                context_mode="evidence",
                origin="none",
                modality="metadata",
                provenance_links=links,
                cost=t.cost(1),
            )
        est = (
            conn.execute(
                text(
                    """SELECT e.id, e.release_id, e.subject_id, ent.label AS subject_label, c.concept_id, e.value,
                      e.alternatives, e.discrepancy_flags, sp.id AS support_id, sp.temporal_extent, sp.spatial_spec,
                      sp.population_spec, sp.aggregation, i.observation_id, src.source_key, r.origin
               FROM belief.estimate e JOIN world.entity ent ON ent.id = e.subject_id
               JOIN ontology.concept_version c ON c.id = e.concept_version_id
               JOIN world.support sp ON sp.id = e.support_id
               JOIN belief.release r ON r.id = e.release_id
               LEFT JOIN belief.input i ON i.estimate_id = e.id
               LEFT JOIN evidence.observation o ON o.id = i.observation_id
               LEFT JOIN evidence.source src ON src.id = o.source_id
               WHERE e.id = :i LIMIT 1"""
                ),
                {"i": evidence_id},
            )
            .mappings()
            .one_or_none()
        )
        if est is None or not est["discrepancy_flags"]:
            raise NotFound("evidence item not found", evidence_id=str(evidence_id))
        rel = (
            conn.execute(
                text(
                    """SELECT r.*, p.sequence, p.published_at FROM belief.release r
               LEFT JOIN ops.publication p ON p.release_id = r.id WHERE r.id = :r"""
                ),
                {"r": est["release_id"]},
            )
            .mappings()
            .one()
        )
    dis = s.SourceDisagreement(
        estimate_id=est["id"],
        release_id=est["release_id"],
        subject=s.EntityRef(id=est["subject_id"], label=est["subject_label"]),
        concept_id=est["concept_id"],
        support=support_summary(dict(est)),
        chosen={
            "value": est["value"],
            "source_key": est["source_key"],
            "observation_id": str(est["observation_id"]) if est["observation_id"] else None,
        },
        alternatives=est["alternatives"],
        discrepancy_flags=est["discrepancy_flags"],
        note="Alternatives are retained, never averaged across incomparable definitions; agreement between "
        "sources of the same lineage family is not independent evidence.",
    )
    detail = s.EvidenceDetail(
        id=est["id"],
        kind="source_disagreement",
        title=f"{est['concept_id']} / {est['subject_label']}",
        disagreement=dis,
    )
    org = est["origin"]
    return s.EvidenceDetailResponse(
        data=detail,
        context_id=rel["context_id"],
        context_mode="belief",
        origin=org,
        modality="selected_estimate",
        baseline_release=release_ref(dict(rel)),
        knowledge_cutoff=rel["knowledge_cutoff"],
        model_disagreement=s.Assessment(status="reported", detail={"flags": est["discrepancy_flags"]}),
        provenance_links=[link("estimate", est["id"])],
        warnings=origin_warnings(org),
        cost=t.cost(1),
    )
