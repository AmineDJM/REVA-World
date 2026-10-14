"""Sources, connectors, ontology, models, coverage/gaps and proposals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from functools import lru_cache
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Connection, text

from reva.api import schemas as s
from reva.api.auth import Principal, require_scope
from reva.api.deps import (
    Timer,
    decode_cursor,
    get_engine,
    origin_warnings,
    page,
    page_limit,
    read_conn,
    release_ref,
    require_aware,
)
from reva.api.errors import Forbidden, NotFound
from reva.api.routes.beliefs import resolve_release
from reva.ontology.registry import REGISTRY_ROOT, load_yaml

router = APIRouter(prefix="/v1", tags=["catalog"], dependencies=[Depends(require_scope("read"))])


# ---------------------------------------------------------------------------
# Sources and connectors
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=s.SourceListResponse)
def list_sources(request: Request) -> s.SourceListResponse:
    """Registered sources with latest metadata version, rights and activity."""
    t = Timer()
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            """SELECT src.id, src.source_key, src.title, src.provider_url, src.lineage_family,
                      COALESCE(m.rights, src.rights) AS rights, COALESCE(m.metadata, src.metadata) AS metadata,
                      m.version AS metadata_version,
                      ARRAY(SELECT cv.name || '@' || cv.version FROM evidence.connector_version cv
                            WHERE cv.source_id = src.id ORDER BY cv.name, cv.version) AS connectors,
                      (SELECT count(*) FROM evidence.observation o WHERE o.source_id = src.id) AS observations,
                      (SELECT max(a.retrieved_at) FROM evidence.acquisition a WHERE a.source_id = src.id)
                        AS last_retrieved_at
               FROM evidence.source src
               LEFT JOIN LATERAL (SELECT version, metadata, rights FROM evidence.source_metadata_version v
                                  WHERE v.source_id = src.id ORDER BY version DESC LIMIT 1) m ON true
               ORDER BY src.source_key""")).mappings()]
    return s.SourceListResponse(data=[s.SourceOut(**r) for r in rows], context_mode="registry", origin="none",
                                modality="metadata", cost=t.cost(len(rows)))


@router.get("/connectors", response_model=s.ConnectorListResponse)
def list_connectors(request: Request) -> s.ConnectorListResponse:
    """Connector versions with licence, declared capabilities, cursor and latest health."""
    t = Timer()
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            """SELECT cv.id, cv.name, cv.version, src.source_key, cv.declared_capabilities, cv.spec_sha256,
                      cv.code_digest, cv.recorded_at,
                      COALESCE(m.rights, src.rights)->>'licence' AS licence,
                      h.status AS h_status, h.metrics AS h_metrics, h.recorded_at AS h_at,
                      cur.cursor,
                      (SELECT count(*) FROM evidence.acquisition a WHERE a.connector_version_id = cv.id)
                        AS acquisitions,
                      (SELECT max(a.retrieved_at) FROM evidence.acquisition a WHERE a.connector_version_id = cv.id)
                        AS last_retrieved_at
               FROM evidence.connector_version cv
               JOIN evidence.source src ON src.id = cv.source_id
               LEFT JOIN LATERAL (SELECT rights FROM evidence.source_metadata_version v
                                  WHERE v.source_id = src.id ORDER BY version DESC LIMIT 1) m ON true
               LEFT JOIN LATERAL (SELECT status, metrics, recorded_at FROM ops.connector_health ch
                                  WHERE ch.connector_version_id = cv.id ORDER BY recorded_at DESC LIMIT 1) h ON true
               LEFT JOIN ops.connector_cursor cur ON cur.connector_name = cv.name
               ORDER BY cv.name, cv.version""")).mappings()]
    data = [
        s.ConnectorOut(
            id=r["id"], name=r["name"], version=r["version"], source_key=r["source_key"], licence=r["licence"],
            declared_capabilities=r["declared_capabilities"], spec_sha256=r["spec_sha256"],
            code_digest=r["code_digest"], recorded_at=r["recorded_at"],
            latest_health=s.ConnectorHealth(status=r["h_status"], metrics=r["h_metrics"], recorded_at=r["h_at"])
            if r["h_status"] else None,
            cursor=r["cursor"], acquisitions=r["acquisitions"], last_retrieved_at=r["last_retrieved_at"],
        )
        for r in rows
    ]
    return s.ConnectorListResponse(data=data, context_mode="registry", origin="none", modality="metadata",
                                   cost=t.cost(len(data)))


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------


@router.get("/ontology", response_model=s.OntologyResponse)
def get_ontology(
    request: Request,
    kind: str | None = Query(None, pattern="^(entity_type|quantity|predicate|role|category|unit|component)$"),
) -> s.OntologyResponse:
    """Versioned concepts and source mappings."""
    t = Timer()
    with read_conn(request) as conn:
        concepts = [s.ConceptOut(**dict(r)) for r in conn.execute(text(
            """SELECT id, concept_id, version, kind, labels, definition, owner, semantic_profile, registry_sha256,
                      recorded_at FROM ontology.concept_version
               WHERE (CAST(:k AS text) IS NULL OR kind = :k) ORDER BY concept_id, version"""),
            {"k": kind}).mappings()]
        mappings = [s.MappingOut(**dict(r)) for r in conn.execute(text(
            """SELECT m.id, m.mapping_key, m.version, m.source_ref, c.concept_id AS target_concept_id,
                      c.version AS target_concept_version, m.mapping_kind, m.transform_spec, m.applicability,
                      m.approval, m.recorded_at
               FROM ontology.mapping m JOIN ontology.concept_version c ON c.id = m.target_concept_id
               ORDER BY m.mapping_key, m.version""")).mappings()]
    return s.OntologyResponse(data=s.Ontology(concepts=concepts, mappings=mappings), context_mode="registry",
                              origin="none", modality="metadata", cost=t.cost(len(concepts) + len(mappings)))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get("/models", response_model=s.ModelListResponse)
def list_models(
    request: Request,
    kind: str | None = Query(None, max_length=50),
    approval_status: str | None = Query(None, pattern="^(approved|candidate|retired)$"),
) -> s.ModelListResponse:
    """Registered model versions with applicability and assumptions."""
    t = Timer()
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            """SELECT mv.id, mv.model_key, mv.version, mv.kind, mv.approval_status, mv.applicability, mv.assumptions,
                      mv.input_ports, mv.output_ports, mv.training_cutoff, mv.spec_sha256, mv.recorded_at,
                      (SELECT count(*) FROM model.evaluation ev WHERE ev.model_version_id = mv.id)
                        AS evaluation_count
               FROM model.version mv
               WHERE (CAST(:k AS text) IS NULL OR mv.kind = :k)
                 AND (CAST(:a AS text) IS NULL OR mv.approval_status = :a)
               ORDER BY mv.model_key, mv.version"""), {"k": kind, "a": approval_status}).mappings()]
    return s.ModelListResponse(data=[s.ModelVersionOut(**r) for r in rows], context_mode="registry", origin="none",
                               modality="metadata", cost=t.cost(len(rows)))


@router.get("/models/{model_ref}/evaluations", response_model=s.ModelEvaluationListResponse,
            responses={404: {"model": s.Problem}})
def list_evaluations(
    model_ref: str,
    request: Request,
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.ModelEvaluationListResponse:
    """Evaluations (scores, replay mode, task scope) for a model version UUID or every version of a model key."""
    t = Timer()
    cur = decode_cursor(cursor, 2)
    try:
        mid: UUID | None = UUID(model_ref)
        key = None
    except ValueError:
        mid, key = None, model_ref[:200]
    with read_conn(request) as conn:
        found = conn.execute(text(
            """SELECT count(*) FROM model.version WHERE (CAST(:i AS uuid) IS NOT NULL AND id = :i)
               OR (CAST(:k AS text) IS NOT NULL AND model_key = :k)"""), {"i": mid, "k": key}).scalar_one()
        if not found:
            raise NotFound("model not found", model=model_ref[:200])
        rows = [dict(r) for r in conn.execute(text(
            """SELECT ev.id, ev.model_version_id, ev.context_id, ev.replay_mode, ev.task_scope, ev.metrics,
                      ev.evaluation_manifest_sha256, ev.recorded_at,
                      COALESCE((SELECT jsonb_agg(jsonb_build_object('decision', p.decision, 'policy_version',
                                 p.policy_version, 'reviewer', p.reviewer, 'reasons', p.reasons,
                                 'recorded_at', p.recorded_at) ORDER BY p.recorded_at)
                                FROM model.promotion p WHERE p.evaluation_id = ev.id), '[]'::jsonb) AS promotions,
                      c.origin
               FROM model.evaluation ev JOIN model.version mv ON mv.id = ev.model_version_id
               JOIN meta.context c ON c.id = ev.context_id
               WHERE ((CAST(:i AS uuid) IS NOT NULL AND mv.id = :i) OR (CAST(:k AS text) IS NOT NULL
                      AND mv.model_key = :k))
                 AND (CAST(:c0 AS timestamptz) IS NULL
                      OR (ev.recorded_at, ev.id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
               ORDER BY ev.recorded_at DESC, ev.id DESC LIMIT :lim"""),
            {"i": mid, "k": key, "c0": cur[0] if cur else None, "c1": cur[1] if cur else None,
             "lim": limit + 1}).mappings()]
    rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
    origins = {r.pop("origin") for r in rows}
    org = "none" if not origins else (origins.pop() if len(origins) == 1 else "mixed")
    warnings = origin_warnings(org)
    if not rows:
        warnings.append(s.ApiWarning(code="no_evaluations", message="No evaluation has been recorded for this model"))
    return s.ModelEvaluationListResponse(
        data=[s.ModelEvaluation(**r) for r in rows], context_mode="evaluation", origin=org,  # type: ignore[arg-type]
        modality="evaluation", warnings=warnings, cost=t.cost(len(rows)), next_cursor=nxt,
        historical_validation=s.Assessment(status="reported" if rows else "not_assessed",
                                           reason=None if rows else "no evaluation recorded"))


# ---------------------------------------------------------------------------
# Coverage and gaps (relative to the versioned requirement catalogue)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def requirement_catalogue() -> dict[str, Any]:
    return load_yaml(REGISTRY_ROOT / "requirements" / "mvp.yaml")  # type: ignore[no-any-return]


def _ratio(n: int, d: int) -> str:
    return f"{n}/{d}"


def coverage_cells(conn: Connection, rel: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    """One record per (requirement, entity, year) cell with its classification."""
    cat = requirement_catalogue()
    keys = sorted({g for r in cat["requirements"] for g in r["geography"]})
    ids = {r["external_key"]: r["entity_id"] for r in conn.execute(text(
        "SELECT external_key, entity_id FROM world.alias WHERE provider = 'iso3166_alpha3' "
        "AND external_key = ANY(:k)"), {"k": keys}).mappings()}
    cells: list[dict[str, Any]] = []
    for req in cat["requirements"]:
        concept = conn.execute(text(
            "SELECT semantic_profile FROM ontology.concept_version WHERE concept_id = :c "
            "ORDER BY recorded_at DESC LIMIT 1"), {"c": req["concept"]}).scalar_one_or_none()
        expected_agg = (concept or {}).get("aggregation")
        found: dict[tuple[str, int], dict[str, Any]] = {}
        if rel is not None:
            for r in conn.execute(text(
                """SELECT e.subject_id, EXTRACT(YEAR FROM lower(sp.valid_range))::int AS year, e.value,
                          sp.aggregation, a.retrieved_at, o.source_flags
                   FROM belief.estimate e
                   JOIN ontology.concept_version c ON c.id = e.concept_version_id
                   JOIN world.support sp ON sp.id = e.support_id
                   LEFT JOIN belief.input i ON i.estimate_id = e.id
                   LEFT JOIN evidence.observation o ON o.id = i.observation_id
                   LEFT JOIN evidence.acquisition a ON a.id = o.acquisition_id
                   WHERE e.release_id = :r AND c.concept_id = :c AND e.subject_id = ANY(:s)"""),
                    {"r": rel["id"], "c": req["concept"], "s": [ids[k] for k in req["geography"] if k in ids]}
            ).mappings():
                k = (str(r["subject_id"]), r["year"])
                prev = found.get(k)
                if prev is None or (prev["value"].get("kind") == "missing" and r["value"].get("kind") != "missing"):
                    found[k] = dict(r)
        for geo in req["geography"]:
            eid = ids.get(geo)
            for year in range(int(req["period"][0]), int(req["period"][1]) + 1):
                hit = found.get((str(eid), year)) if eid else None
                cell = {"req": req, "entity_key": geo, "entity_id": eid, "year": year, "row": hit}
                if hit is None:
                    cell["state"] = "absent"
                elif hit["value"].get("kind") == "missing":
                    cell["state"] = "typed_missing"
                else:
                    cell["state"] = "available"
                    cell["semantic"] = expected_agg is None or hit["aggregation"] == expected_agg
                    ra = hit["retrieved_at"]
                    cell["fresh"] = ra is not None and now - ra <= timedelta(days=int(req["acceptable_freshness_days"]))
                cells.append(cell)
    return cells


COVERAGE_STATEMENT = (
    "Coverage is reported only relative to the finite, versioned requirement catalogue {v} under its declared "
    "thresholds. It is not a measure of knowledge of any country or of reality."
)


def _release_or_none(conn: Connection, release_id: UUID | None, as_of: datetime | None) -> dict[str, Any] | None:
    try:
        return resolve_release(conn, release_id, as_of)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) == "no_release":
            return None
        raise


@router.get("/coverage", response_model=s.CoverageResponse)
def get_coverage(
    request: Request,
    release_id: UUID | None = None,
    as_of: datetime | None = Query(None, description="Use the release published at/before this instant"),
) -> s.CoverageResponse:
    """Catalogue-relative coverage of the pinned release: numerator/denominator per requirement."""
    t = Timer()
    require_aware(as_of, "as_of")
    now = as_of or datetime.now(UTC)
    with read_conn(request) as conn:
        rel = _release_or_none(conn, release_id, as_of)
        cells = coverage_cells(conn, rel, now)
    cat = requirement_catalogue()
    reqs: list[s.RequirementCoverage] = []
    totals = {"denominator": 0, "available": 0, "semantically_compatible": 0, "fresh": 0}
    wden = Fraction(0)
    wav = Fraction(0)
    for req in cat["requirements"]:
        mine = [c for c in cells if c["req"] is req]
        den = len(mine)
        av = sum(1 for c in mine if c["state"] == "available")
        sem = sum(1 for c in mine if c["state"] == "available" and c.get("semantic"))
        fr = sum(1 for c in mine if c["state"] == "available" and c.get("fresh"))
        reqs.append(s.RequirementCoverage(
            requirement_id=req["id"], task=req["task"], concept=req["concept"], geography=list(req["geography"]),
            period=list(req["period"]), weight=float(req["weight"]),
            acceptable_freshness_days=int(req["acceptable_freshness_days"]), denominator=den, available=av,
            typed_missing=sum(1 for c in mine if c["state"] == "typed_missing"),
            absent=sum(1 for c in mine if c["state"] == "absent"), semantically_compatible=sem, fresh=fr,
            availability_ratio=_ratio(av, den), semantic_ratio=_ratio(sem, den), freshness_ratio=_ratio(fr, den)))
        totals["denominator"] += den
        totals["available"] += av
        totals["semantically_compatible"] += sem
        totals["fresh"] += fr
        wden += Fraction(str(req["weight"])) * den
        wav += Fraction(str(req["weight"])) * av
    warnings = origin_warnings(rel["origin"]) if rel else [
        s.ApiWarning(code="no_release", message="No published belief release; every cell is uncovered")]
    data = s.Coverage(
        catalogue_version=cat["catalogue_version"], statement=COVERAGE_STATEMENT.format(v=cat["catalogue_version"]),
        release_id=rel["id"] if rel else None, requirements=reqs,
        totals=s.CoverageTotals(**totals, weighted_denominator=float(wden), weighted_available=float(wav)))
    return s.CoverageResponse(
        data=data, context_id=rel["context_id"] if rel else None, context_mode="belief",
        origin=rel["origin"] if rel else "none", modality="metadata", baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"] if rel else None, warnings=warnings, cost=t.cost(len(cells)))


def _gap_action(state: str, flags: dict[str, Any] | None) -> tuple[str, str]:
    if state == "typed_missing":
        if flags and flags.get("provider_returned_no_rows"):
            return ("provider returned no rows for this exact key; stored as typed Missing",
                    "Seek an eligible alternative source or mirror data through bounded discovery; never impute zero")
        return ("source reports the value as missing", "Check the provider's missingness flags and later vintages")
    if state == "absent":
        return ("no eligible estimate in the pinned release",
                "Ingest the registered source (or add a reviewed mapping/connector), then publish a new release")
    if state == "stale":
        return ("estimate is older than the requirement's acceptable freshness", "Re-run the source connector")
    if state == "semantic_mismatch":
        return ("estimate support/definition does not match the requirement", "Review the concept mapping")
    return ("no published release", "Publish a belief release")


@router.get("/gaps", response_model=s.GapListResponse)
def get_gaps(
    request: Request,
    release_id: UUID | None = None,
    as_of: datetime | None = None,
    requirement_id: str | None = Query(None, max_length=50),
    entity_key: str | None = Query(None, max_length=16),
) -> s.GapListResponse:
    """Actionable gaps against the requirement catalogue (e.g. Algeria crude flows missing)."""
    t = Timer()
    require_aware(as_of, "as_of")
    now = as_of or datetime.now(UTC)
    with read_conn(request) as conn:
        rel = _release_or_none(conn, release_id, as_of)
        cells = coverage_cells(conn, rel, now)
    gaps: list[s.Gap] = []
    for c in cells:
        state = c["state"]
        if state == "available":
            if not c.get("semantic"):
                state = "semantic_mismatch"
            elif not c.get("fresh"):
                state = "stale"
            else:
                continue
        if rel is None:
            state = "no_release"
        if requirement_id and c["req"]["id"] != requirement_id:
            continue
        if entity_key and c["entity_key"] != entity_key.upper():
            continue
        flags = c["row"]["source_flags"] if c["row"] else None
        reason, action = _gap_action(state, flags)
        gaps.append(s.Gap(requirement_id=c["req"]["id"], concept=c["req"]["concept"], entity_key=c["entity_key"],
                          entity_id=c["entity_id"], period=c["year"], gap_kind=state, reason=reason,  # type: ignore[arg-type]
                          source_flags=flags, action=action))
    warnings = origin_warnings(rel["origin"]) if rel else []
    return s.GapListResponse(
        data=gaps, context_id=rel["context_id"] if rel else None, context_mode="belief",
        origin=rel["origin"] if rel else "none", modality="metadata", baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"] if rel else None, warnings=warnings, cost=t.cost(len(cells)))


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@router.get("/proposals", response_model=s.ProposalListResponse)
def list_proposals(
    request: Request,
    pending_only: bool = False,
    proposal_type: str | None = Query(None, max_length=100),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.ProposalListResponse:
    """Autonomy proposals with gate results and human reviews."""
    t = Timer()
    cur = decode_cursor(cursor, 2)
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            """SELECT p.id, p.proposal_type, p.manifest_sha256, p.permission_tier, p.summary, p.author, p.recorded_at,
                 COALESCE((SELECT jsonb_agg(jsonb_build_object('id', r.id, 'decision', r.decision, 'reviewer',
                   r.reviewer, 'comment', r.comment, 'recorded_at', r.recorded_at) ORDER BY r.recorded_at)
                   FROM ops.proposal_review r WHERE r.proposal_id = p.id), '[]'::jsonb) AS reviews,
                 COALESCE((SELECT jsonb_agg(jsonb_build_object('id', g.id, 'gate_name', g.gate_name,
                   'policy_version', g.policy_version, 'decision', g.decision, 'evidence_sha256', g.evidence_sha256,
                   'evaluator', g.evaluator, 'recorded_at', g.recorded_at) ORDER BY g.recorded_at)
                   FROM ops.gate_result g WHERE g.proposal_id = p.id), '[]'::jsonb) AS gate_results
               FROM ops.proposal p
               WHERE (CAST(:pt AS text) IS NULL OR p.proposal_type = :pt)
                 AND (NOT :pending OR NOT EXISTS (SELECT 1 FROM ops.proposal_review r WHERE r.proposal_id = p.id))
                 AND (CAST(:c0 AS timestamptz) IS NULL
                      OR (p.recorded_at, p.id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
               ORDER BY p.recorded_at DESC, p.id DESC LIMIT :lim"""),
            {"pt": proposal_type, "pending": pending_only, "c0": cur[0] if cur else None,
             "c1": cur[1] if cur else None, "lim": limit + 1}).mappings()]
    rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
    return s.ProposalListResponse(data=[s.Proposal(**r) for r in rows], context_mode="operations", origin="none",
                                  modality="metadata", cost=t.cost(len(rows)), next_cursor=nxt)


@router.post("/proposals/{proposal_id}/review", response_model=s.ReviewRecorded, status_code=201,
             responses={403: {"model": s.Problem}, 404: {"model": s.Problem}})
def review_proposal(
    proposal_id: UUID,
    body: s.ReviewRequest,
    request: Request,
    principal: Principal = Depends(require_scope("review")),
) -> s.ReviewRecorded:
    """Record a permissioned human review decision (ops.review_proposal)."""
    reviewer = principal.human_reviewer
    if reviewer is None:
        raise Forbidden("reviews must be recorded by a named human reviewer (session or human:<name> token)")
    with get_engine(request).begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM ops.proposal WHERE id = :i"), {"i": proposal_id}).scalar_one_or_none()
        if exists is None:
            raise NotFound("proposal not found", proposal_id=str(proposal_id))
        rid = conn.execute(text("SELECT ops.review_proposal(:p, :d, :r, :c)"),
                           {"p": proposal_id, "d": body.decision, "r": reviewer, "c": body.comment}).scalar_one()
    return s.ReviewRecorded(review_id=rid, proposal_id=proposal_id, decision=body.decision, reviewer=reviewer)
