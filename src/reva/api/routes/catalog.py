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
    origin_of,
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


def _acq_origin(n_live: int, n_fixture: int) -> str:
    """Honest origin of a connector/source from its acquisitions (never assumed live)."""
    if n_live and n_fixture:
        return "mixed"
    if n_live:
        return "live"
    if n_fixture:
        return "test_fixture"
    return "none"


def _origin_fields(r: dict[str, Any]) -> dict[str, Any]:
    n_live, n_fix = int(r.pop("n_live") or 0), int(r.pop("n_fixture") or 0)
    return {
        "origin": _acq_origin(n_live, n_fix),
        "latest_acquisition_origin": r.pop("latest_origin"),
        "acquisitions_by_origin": s.AcquisitionOriginCounts(live=n_live, test_fixture=n_fix),
    }


def _listing_origin(items: list[Any]) -> str:
    return origin_of(
        x
        for o in items
        if o.origin != "none"
        for x in (("live", "test_fixture") if o.origin == "mixed" else (o.origin,))
    )


ACQ_ORIGIN_SQL = """
LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE a.origin = 'live') AS n_live,
                          count(*) FILTER (WHERE a.origin = 'test_fixture') AS n_fixture,
                          count(*) AS n_acq, max(a.retrieved_at) AS last_retrieved_at
                   FROM evidence.acquisition a WHERE a.{col} = {ref}) ag ON true
LEFT JOIN LATERAL (SELECT a.origin AS latest_origin FROM evidence.acquisition a WHERE a.{col} = {ref}
                   ORDER BY a.retrieved_at DESC, a.recorded_at DESC LIMIT 1) la ON true
"""


@router.get("/sources", response_model=s.SourceListResponse)
def list_sources(request: Request) -> s.SourceListResponse:
    """Registered sources with latest metadata version, rights, activity and acquisition origin."""
    t = Timer()
    with read_conn(request) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT src.id, src.source_key, src.title, src.provider_url, src.lineage_family,
                      COALESCE(m.rights, src.rights) AS rights, COALESCE(m.metadata, src.metadata) AS metadata,
                      m.version AS metadata_version,
                      ARRAY(SELECT cv.name || '@' || cv.version FROM evidence.connector_version cv
                            WHERE cv.source_id = src.id ORDER BY cv.name, cv.version) AS connectors,
                      (SELECT count(*) FROM evidence.observation o WHERE o.source_id = src.id) AS observations,
                      ag.last_retrieved_at, ag.n_live, ag.n_fixture, la.latest_origin
               FROM evidence.source src
               LEFT JOIN LATERAL (SELECT version, metadata, rights FROM evidence.source_metadata_version v
                                  WHERE v.source_id = src.id ORDER BY version DESC LIMIT 1) m ON true
               """
                    + ACQ_ORIGIN_SQL.format(col="source_id", ref="src.id")
                    + """
               ORDER BY src.source_key"""
                )
            ).mappings()
        ]
    data = []
    for r in rows:
        extra = _origin_fields(r)  # pops the aggregate columns first
        data.append(s.SourceOut(**r, **extra))
    org = _listing_origin(data)
    return s.SourceListResponse(
        data=data,
        context_mode="registry",
        origin=org,  # type: ignore[arg-type]
        modality="metadata",
        warnings=origin_warnings(org),
        cost=t.cost(len(data)),
    )


@router.get("/connectors", response_model=s.ConnectorListResponse)
def list_connectors(request: Request) -> s.ConnectorListResponse:
    """Connector versions with licence, declared capabilities, cursor, latest health and acquisition origin."""
    t = Timer()
    with read_conn(request) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT cv.id, cv.name, cv.version, src.source_key, cv.declared_capabilities, cv.spec_sha256,
                      cv.code_digest, cv.recorded_at,
                      COALESCE(m.rights, src.rights)->>'licence' AS licence,
                      h.status AS h_status, h.metrics AS h_metrics, h.recorded_at AS h_at,
                      cur.cursor, ag.n_acq AS acquisitions, ag.last_retrieved_at, ag.n_live, ag.n_fixture,
                      la.latest_origin
               FROM evidence.connector_version cv
               JOIN evidence.source src ON src.id = cv.source_id
               LEFT JOIN LATERAL (SELECT rights FROM evidence.source_metadata_version v
                                  WHERE v.source_id = src.id ORDER BY version DESC LIMIT 1) m ON true
               LEFT JOIN LATERAL (SELECT status, metrics, recorded_at FROM ops.connector_health ch
                                  WHERE ch.connector_version_id = cv.id ORDER BY recorded_at DESC LIMIT 1) h ON true
               LEFT JOIN ops.connector_cursor cur ON cur.connector_name = cv.name
               """
                    + ACQ_ORIGIN_SQL.format(col="connector_version_id", ref="cv.id")
                    + """
               ORDER BY cv.name, cv.version"""
                )
            ).mappings()
        ]
    for r in rows:
        r["origin_fields"] = _origin_fields(r)
    data = [
        s.ConnectorOut(
            id=r["id"],
            name=r["name"],
            version=r["version"],
            source_key=r["source_key"],
            licence=r["licence"],
            declared_capabilities=r["declared_capabilities"],
            spec_sha256=r["spec_sha256"],
            code_digest=r["code_digest"],
            recorded_at=r["recorded_at"],
            latest_health=s.ConnectorHealth(status=r["h_status"], metrics=r["h_metrics"], recorded_at=r["h_at"])
            if r["h_status"]
            else None,
            cursor=r["cursor"],
            acquisitions=r["acquisitions"],
            last_retrieved_at=r["last_retrieved_at"],
            **r["origin_fields"],
        )
        for r in rows
    ]
    org = _listing_origin(data)
    return s.ConnectorListResponse(
        data=data,
        context_mode="registry",
        origin=org,  # type: ignore[arg-type]
        modality="metadata",
        warnings=origin_warnings(org),
        cost=t.cost(len(data)),
    )


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
        concepts = [
            s.ConceptOut(**dict(r))
            for r in conn.execute(
                text(
                    """SELECT id, concept_id, version, kind, labels, definition, owner, semantic_profile, registry_sha256,
                      recorded_at FROM ontology.concept_version
               WHERE (CAST(:k AS text) IS NULL OR kind = :k) ORDER BY concept_id, version"""
                ),
                {"k": kind},
            ).mappings()
        ]
        mappings = [
            s.MappingOut(**dict(r))
            for r in conn.execute(
                text(
                    """SELECT m.id, m.mapping_key, m.version, m.source_ref, c.concept_id AS target_concept_id,
                      c.version AS target_concept_version, m.mapping_kind, m.transform_spec, m.applicability,
                      m.approval, m.recorded_at
               FROM ontology.mapping m JOIN ontology.concept_version c ON c.id = m.target_concept_id
               ORDER BY m.mapping_key, m.version"""
                )
            ).mappings()
        ]
    return s.OntologyResponse(
        data=s.Ontology(concepts=concepts, mappings=mappings),
        context_mode="registry",
        origin="none",
        modality="metadata",
        cost=t.cost(len(concepts) + len(mappings)),
    )


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
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT mv.id, mv.model_key, mv.version, mv.kind, mv.approval_status, mv.applicability, mv.assumptions,
                      mv.input_ports, mv.output_ports, mv.training_cutoff, mv.spec_sha256, mv.recorded_at,
                      (SELECT count(*) FROM model.evaluation ev WHERE ev.model_version_id = mv.id)
                        AS evaluation_count
               FROM model.version mv
               WHERE (CAST(:k AS text) IS NULL OR mv.kind = :k)
                 AND (CAST(:a AS text) IS NULL OR mv.approval_status = :a)
               ORDER BY mv.model_key, mv.version"""
                ),
                {"k": kind, "a": approval_status},
            ).mappings()
        ]
        # Origin of the data behind this view: the evaluation contexts of the listed models plus the latest
        # published release they would be applied to (registry entries themselves have no origin).
        origins = list(
            conn.execute(
                text(
                    """SELECT DISTINCT c.origin FROM model.evaluation ev JOIN meta.context c ON c.id = ev.context_id
               WHERE ev.model_version_id = ANY(:ids)
               UNION
               SELECT origin FROM (SELECT r.origin FROM belief.release r JOIN ops.publication p ON p.release_id = r.id
                                   ORDER BY p.sequence DESC LIMIT 1) latest"""
                ),
                {"ids": [r["id"] for r in rows]},
            ).scalars()
        )
    org = origin_of(origins)
    return s.ModelListResponse(
        data=[s.ModelVersionOut(**r) for r in rows],
        context_mode="registry",
        origin=org,
        modality="metadata",
        warnings=origin_warnings(org),  # type: ignore[arg-type]
        assumptions=[
            "origin describes the evaluation data and the latest published release "
            "these models apply to; registry entries themselves have no origin"
        ],
        cost=t.cost(len(rows)),
    )


# Naive last-value baseline every challenger must beat (registry/models/methods.yaml).
NAIVE_BASELINE_MODEL_KEY = "forecast.last_value"
RETROSPECTIVE_LABEL = "revised-data retrospective — not a real-time backtest"
REPLAY_MODE_LABELS = {
    "revised_data_retrospective": RETROSPECTIVE_LABEL,
    "operational_as_known": "operational as-known — what REVA actually had published at the cutoff",
    "available_then_reconstruction": "available-then reconstruction — archived vintages; not what REVA believed then",
    "analytic_verification": "analytic verification — checked against analytic solutions; not an empirical backtest",
}

EVALUATION_SQL = """
SELECT ev.id, ev.model_version_id, mv.model_key, mv.version AS model_version, ev.context_id, c.origin,
       c.knowledge_cutoff AS ctx_cutoff,
       ev.replay_mode, ev.task_scope, ev.metrics, ev.evaluation_manifest_sha256, ev.recorded_at,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('decision', p.decision, 'policy_version',
                  p.policy_version, 'reviewer', p.reviewer, 'reasons', p.reasons,
                  'recorded_at', p.recorded_at) ORDER BY p.recorded_at)
                 FROM model.promotion p WHERE p.evaluation_id = ev.id), '[]'::jsonb) AS promotions,
       (mv.model_key = :naive OR COALESCE(ev.metrics->>'is_naive_baseline', '') = 'true') AS is_naive_baseline
FROM model.evaluation ev JOIN model.version mv ON mv.id = ev.model_version_id
JOIN meta.context c ON c.id = ev.context_id
"""


def _period(r: dict[str, Any]) -> str:
    return str((r["task_scope"] or {}).get("period") or (r["metrics"] or {}).get("period") or "")


@router.get(
    "/models/{model_ref}/evaluations",
    response_model=s.ModelEvaluationListResponse,
    responses={404: {"model": s.Problem}},
)
def list_evaluations(
    model_ref: str,
    request: Request,
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.ModelEvaluationListResponse:
    """Evaluations (scores, replay mode + label, task scope) for a model version UUID or every version of a key.

    For every evaluation manifest and period on the page, the naive last-value baseline's row from the same
    manifest is included (``included_as = naive_baseline_comparator``) so scores are never shown without it.
    """
    t = Timer()
    cur = decode_cursor(cursor, "tu")
    try:
        mid: UUID | None = UUID(model_ref)
        key = None
    except ValueError:
        mid, key = None, model_ref[:200]
    with read_conn(request) as conn:
        found = conn.execute(
            text(
                """SELECT count(*) FROM model.version WHERE (CAST(:i AS uuid) IS NOT NULL AND id = :i)
               OR (CAST(:k AS text) IS NOT NULL AND model_key = :k)"""
            ),
            {"i": mid, "k": key},
        ).scalar_one()
        if not found:
            raise NotFound("model not found", model=model_ref[:200])
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    EVALUATION_SQL
                    + """
               WHERE ((CAST(:i AS uuid) IS NOT NULL AND mv.id = :i) OR (CAST(:k AS text) IS NOT NULL
                      AND mv.model_key = :k))
                 AND (CAST(:c0 AS timestamptz) IS NULL
                      OR (ev.recorded_at, ev.id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
               ORDER BY ev.recorded_at DESC, ev.id DESC LIMIT :lim"""
                ),
                {
                    "i": mid,
                    "k": key,
                    "c0": cur[0] if cur else None,
                    "c1": cur[1] if cur else None,
                    "lim": limit + 1,
                    "naive": NAIVE_BASELINE_MODEL_KEY,
                },
            ).mappings()
        ]
        rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
        for r in rows:
            r["included_as"] = "requested_model"
        manifests = sorted({r["evaluation_manifest_sha256"] for r in rows})
        comparators: list[dict[str, Any]] = []
        if manifests:
            wanted = {(r["evaluation_manifest_sha256"], _period(r)) for r in rows}
            have = {r["id"] for r in rows}
            for r in conn.execute(
                text(
                    EVALUATION_SQL
                    + """
                   WHERE ev.evaluation_manifest_sha256 = ANY(:m)
                     AND (mv.model_key = :naive OR COALESCE(ev.metrics->>'is_naive_baseline', '') = 'true')
                   ORDER BY ev.recorded_at DESC, ev.id DESC LIMIT 1000"""
                ),
                {"m": manifests, "naive": NAIVE_BASELINE_MODEL_KEY},
            ).mappings():
                r = dict(r)
                if r["id"] in have or (r["evaluation_manifest_sha256"], _period(r)) not in wanted:
                    continue
                r["included_as"] = "naive_baseline_comparator"
                comparators.append(r)
    all_rows = rows + comparators
    org = origin_of(r["origin"] for r in all_rows)
    warnings = origin_warnings(org)
    if not rows:
        warnings.append(s.ApiWarning(code="no_evaluations", message="No evaluation has been recorded for this model"))
    if any(r["replay_mode"] == "revised_data_retrospective" for r in all_rows):
        warnings.append(
            s.ApiWarning(
                code="revised_data_retrospective",
                message=f"{RETROSPECTIVE_LABEL}: scores use today's revised historical values split by year; no "
                "forecast was issued at these origins.",
            )
        )
    groups = {(r["evaluation_manifest_sha256"], _period(r)) for r in rows}
    naive_groups = {(r["evaluation_manifest_sha256"], _period(r)) for r in all_rows if r["is_naive_baseline"]}
    if groups - naive_groups:
        warnings.append(
            s.ApiWarning(
                code="naive_baseline_missing",
                message=f"{len(groups - naive_groups)} evaluation set(s) have no naive baseline row; their scores cannot "
                "be compared to the naive forecast",
            )
        )
    cutoffs = [r.pop("ctx_cutoff") for r in all_rows]
    known = [c for c in cutoffs if c is not None]
    data = [
        s.ModelEvaluation(**r, replay_mode_label=REPLAY_MODE_LABELS.get(r["replay_mode"], r["replay_mode"]))
        for r in all_rows
    ]
    modes = sorted({r["replay_mode"] for r in all_rows})
    return s.ModelEvaluationListResponse(
        data=data,
        context_mode="evaluation",
        origin=org,  # type: ignore[arg-type]
        modality="evaluation",
        warnings=warnings,
        cost=t.cost(len(data)),
        next_cursor=nxt,
        knowledge_cutoff=max(known) if known else None,
        knowledge_cutoff_basis="evaluation_context" if known else None,
        historical_validation=s.Assessment(
            status="reported", detail={"replay_modes": modes, "labels": [REPLAY_MODE_LABELS.get(m, m) for m in modes]}
        )
        if rows
        else s.Assessment(status="not_assessed", reason="no evaluation recorded"),
    )


BACKTEST_SQL = """
SELECT ev.id, ev.model_version_id, mv.model_key, mv.version AS model_version, ev.context_id, c.origin,
       c.knowledge_cutoff, ev.replay_mode, ev.task_scope, ev.metrics, ev.evaluation_manifest_sha256, ev.recorded_at,
       a.parameters->>'release_id' AS release_id
FROM model.evaluation ev JOIN model.version mv ON mv.id = ev.model_version_id
JOIN meta.context c ON c.id = ev.context_id
JOIN meta.activity a ON a.id = ev.provenance_activity_id
WHERE mv.model_key = :k AND ev.task_scope->>'task' = :task
  AND (CAST(:c0 AS timestamptz) IS NULL
       OR (ev.recorded_at, ev.id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
ORDER BY ev.recorded_at DESC, ev.id DESC LIMIT :lim
"""


def _backtest_row(r: dict[str, Any], function: str) -> s.ModelBacktest:
    from reva.evaluation.backtests import design_of

    metrics = r["metrics"] or {}
    scope = r["task_scope"] or {}
    replay_label = REPLAY_MODE_LABELS.get(r["replay_mode"], r["replay_mode"])
    labels = [replay_label] + [
        str(metrics[k]) for k in ("label", "model_label", "statement") if isinstance(metrics.get(k), str) and metrics[k]
    ]
    try:
        release_id = UUID(r["release_id"]) if r["release_id"] else None
    except ValueError:
        release_id = None
    return s.ModelBacktest(
        id=r["id"],
        model_version_id=r["model_version_id"],
        model_key=r["model_key"],
        model_version=r["model_version"],
        task=str(scope.get("task")),
        country=scope.get("country"),
        backtest_function=function,
        context_id=r["context_id"],
        origin=r["origin"],
        release_id=release_id,
        knowledge_cutoff=r["knowledge_cutoff"],
        replay_mode=r["replay_mode"],
        replay_mode_label=replay_label,
        design=design_of(scope),
        metrics=metrics,
        labels=list(dict.fromkeys(labels)),
        evaluation_manifest_sha256=r["evaluation_manifest_sha256"],
        recorded_at=r["recorded_at"],
    )


@router.get(
    "/models/{model_key}/backtests", response_model=s.ModelBacktestListResponse, responses={404: {"model": s.Problem}}
)
def list_backtests(
    model_key: str,
    request: Request,
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.ModelBacktestListResponse:
    """Stored back-tests of a model (all versions of the key), newest first: metrics, design, labels and the
    sha256 of the evaluation artifact. Back-tests are revised-data retrospectives computed by the worker job
    ``backtest`` from the datasets pinned by a published release; a model without a back-test function returns
    an empty list with the warning ``backtest_unavailable``."""
    from reva.evaluation.backtests import BACKTESTS

    t = Timer()
    cur = decode_cursor(cursor, "tu")
    key = model_key[:200]
    spec = BACKTESTS.get(key)
    with read_conn(request) as conn:
        found = conn.execute(text("SELECT count(*) FROM model.version WHERE model_key = :k"), {"k": key}).scalar_one()
        if not found:
            raise NotFound("model not found", model=key)
        rows: list[dict[str, Any]] = []
        if spec is not None:
            rows = [
                dict(r)
                for r in conn.execute(
                    text(BACKTEST_SQL),
                    {
                        "k": key,
                        "task": spec.task,
                        "c0": cur[0] if cur else None,
                        "c1": cur[1] if cur else None,
                        "lim": limit + 1,
                    },
                ).mappings()
            ]
    rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
    data = [_backtest_row(r, spec.function) for r in rows] if spec is not None else []
    org = origin_of(r.origin for r in data)
    warnings = origin_warnings(org)
    if spec is None:
        warnings.append(
            s.ApiWarning(
                code="backtest_unavailable",
                message=f"Model {key} has no back-test function; nothing can be back-tested",
            )
        )
    elif not data:
        warnings.append(
            s.ApiWarning(
                code="no_backtests", message="No back-test has been stored for this model yet (worker job 'backtest')"
            )
        )
    if data:
        warnings.append(
            s.ApiWarning(
                code="revised_data_retrospective",
                message=f"{RETROSPECTIVE_LABEL}: back-tests use today's revised provider data for past years; no "
                "forecast was issued at these jump-off years. Errors describe the method on past data, not a "
                "probability statement about future projections.",
            )
        )
    known = [d.knowledge_cutoff for d in data if d.knowledge_cutoff is not None]
    return s.ModelBacktestListResponse(
        data=data,
        context_mode="evaluation",
        origin=org,
        modality="evaluation",  # type: ignore[arg-type]
        warnings=warnings,
        cost=t.cost(len(data)),
        next_cursor=nxt,
        knowledge_cutoff=max(known) if known else None,
        knowledge_cutoff_basis="evaluation_context" if known else None,
        assumptions=[spec.description] if spec is not None else [],
        historical_validation=s.Assessment(
            status="reported",
            detail={"replay_modes": sorted({d.replay_mode for d in data}), "labels": [RETROSPECTIVE_LABEL]},
        )
        if data
        else s.Assessment(
            status="not_assessed", reason="no back-test stored" if spec is not None else "no back-test function"
        ),
    )


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
    ids = {
        r["external_key"]: r["entity_id"]
        for r in conn.execute(
            text(
                "SELECT external_key, entity_id FROM world.alias WHERE provider = 'iso3166_alpha3' "
                "AND external_key = ANY(:k)"
            ),
            {"k": keys},
        ).mappings()
    }
    cells: list[dict[str, Any]] = []
    for req in cat["requirements"]:
        concept = conn.execute(
            text(
                "SELECT semantic_profile FROM ontology.concept_version WHERE concept_id = :c "
                "ORDER BY recorded_at DESC LIMIT 1"
            ),
            {"c": req["concept"]},
        ).scalar_one_or_none()
        expected_agg = (concept or {}).get("aggregation")
        found: dict[tuple[str, int], dict[str, Any]] = {}
        if rel is not None:
            for r in conn.execute(
                text(
                    """SELECT e.subject_id, EXTRACT(YEAR FROM lower(sp.valid_range))::int AS year, e.value,
                          sp.aggregation, a.retrieved_at, o.source_flags
                   FROM belief.estimate e
                   JOIN ontology.concept_version c ON c.id = e.concept_version_id
                   JOIN world.support sp ON sp.id = e.support_id
                   LEFT JOIN belief.input i ON i.estimate_id = e.id
                   LEFT JOIN evidence.observation o ON o.id = i.observation_id
                   LEFT JOIN evidence.acquisition a ON a.id = o.acquisition_id
                   WHERE e.release_id = :r AND c.concept_id = :c AND e.subject_id = ANY(:s)"""
                ),
                {"r": rel["id"], "c": req["concept"], "s": [ids[k] for k in req["geography"] if k in ids]},
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
    except Exception as e:
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
        reqs.append(
            s.RequirementCoverage(
                requirement_id=req["id"],
                task=req["task"],
                concept=req["concept"],
                geography=list(req["geography"]),
                period=list(req["period"]),
                weight=float(req["weight"]),
                acceptable_freshness_days=int(req["acceptable_freshness_days"]),
                denominator=den,
                available=av,
                typed_missing=sum(1 for c in mine if c["state"] == "typed_missing"),
                absent=sum(1 for c in mine if c["state"] == "absent"),
                semantically_compatible=sem,
                fresh=fr,
                availability_ratio=_ratio(av, den),
                semantic_ratio=_ratio(sem, den),
                freshness_ratio=_ratio(fr, den),
            )
        )
        totals["denominator"] += den
        totals["available"] += av
        totals["semantically_compatible"] += sem
        totals["fresh"] += fr
        wden += Fraction(str(req["weight"])) * den
        wav += Fraction(str(req["weight"])) * av
    warnings = (
        origin_warnings(rel["origin"])
        if rel
        else [s.ApiWarning(code="no_release", message="No published belief release; every cell is uncovered")]
    )
    data = s.Coverage(
        catalogue_version=cat["catalogue_version"],
        statement=COVERAGE_STATEMENT.format(v=cat["catalogue_version"]),
        release_id=rel["id"] if rel else None,
        requirements=reqs,
        totals=s.CoverageTotals(**totals, weighted_denominator=float(wden), weighted_available=float(wav)),
    )
    return s.CoverageResponse(
        data=data,
        context_id=rel["context_id"] if rel else None,
        context_mode="belief",
        origin=rel["origin"] if rel else "none",
        modality="metadata",
        baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"] if rel else None,
        warnings=warnings,
        cost=t.cost(len(cells)),
    )


def _gap_action(state: str, flags: dict[str, Any] | None) -> tuple[str, str]:
    if state == "typed_missing":
        if flags and flags.get("provider_returned_no_rows"):
            return (
                "provider returned no rows for this exact key; stored as typed Missing",
                "Seek an eligible alternative source or mirror data through bounded discovery; never impute zero",
            )
        return ("source reports the value as missing", "Check the provider's missingness flags and later vintages")
    if state == "absent":
        return (
            "no eligible estimate in the pinned release",
            "Ingest the registered source (or add a reviewed mapping/connector), then publish a new release",
        )
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
        gaps.append(
            s.Gap(
                requirement_id=c["req"]["id"],
                concept=c["req"]["concept"],
                entity_key=c["entity_key"],
                entity_id=c["entity_id"],
                period=c["year"],
                gap_kind=state,
                reason=reason,  # type: ignore[arg-type]
                source_flags=flags,
                action=action,
            )
        )
    warnings = origin_warnings(rel["origin"]) if rel else []
    return s.GapListResponse(
        data=gaps,
        context_id=rel["context_id"] if rel else None,
        context_mode="belief",
        origin=rel["origin"] if rel else "none",
        modality="metadata",
        baseline_release=release_ref(rel),
        knowledge_cutoff=rel["knowledge_cutoff"] if rel else None,
        warnings=warnings,
        cost=t.cost(len(cells)),
    )


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
    cur = decode_cursor(cursor, "tu")
    with read_conn(request) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
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
               ORDER BY p.recorded_at DESC, p.id DESC LIMIT :lim"""
                ),
                {
                    "pt": proposal_type,
                    "pending": pending_only,
                    "c0": cur[0] if cur else None,
                    "c1": cur[1] if cur else None,
                    "lim": limit + 1,
                },
            ).mappings()
        ]
    rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
    return s.ProposalListResponse(
        data=[s.Proposal(**r) for r in rows],
        context_mode="operations",
        origin="none",
        modality="metadata",
        cost=t.cost(len(rows)),
        next_cursor=nxt,
    )


@router.post(
    "/proposals/{proposal_id}/review",
    response_model=s.ReviewRecorded,
    status_code=201,
    responses={403: {"model": s.Problem}, 404: {"model": s.Problem}},
)
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
        rid = conn.execute(
            text("SELECT ops.review_proposal(:p, :d, :r, :c)"),
            {"p": proposal_id, "d": body.decision, "r": reviewer, "c": body.comment},
        ).scalar_one()
    return s.ReviewRecorded(review_id=rid, proposal_id=proposal_id, decision=body.decision, reviewer=reviewer)


# ---------------------------------------------------------------------------
# Discovery candidates (read-only)
# ---------------------------------------------------------------------------

DISCOVERY_JOB_SCAN = 50
CANDIDATE_NOTE = s.ApiWarning(
    code="unreviewed_candidates",
    message="Discovery candidates are lexical search results and proposals. Ranking never grants semantic "
    "equivalence. Promotion needs passing gates: tier-1 changes within the signed policy scope are "
    "promoted automatically; tier-2/3 changes need a human review.",
)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _gate(res: dict[str, Any]) -> dict[str, Any]:
    return res.get("gate") if isinstance(res.get("gate"), dict) else {}  # type: ignore[return-value]


def _job_candidates(job: dict[str, Any], proposal_dataset: dict[str, str]) -> list[s.DiscoveryCandidate]:
    res = job["result_ref"] if isinstance(job["result_ref"], dict) else {}
    payload = job["payload"] if isinstance(job["payload"], dict) else {}
    mode = res.get("mode") or payload.get("mode")
    origin = "test_fixture" if mode == "fixture" else ("live" if mode == "live" else None)
    search = res.get("search") if isinstance(res.get("search"), dict) else {}
    inspections = {i.get("code"): i for i in search.get("inspections") or [] if isinstance(i, dict)}
    gate = _gate(res)
    proposal_id = gate.get("proposal_id")
    # Exact link: the job's gated proposal records the dataset it was generated from.
    chosen = proposal_dataset.get(str(proposal_id)) if proposal_id else None
    out = []
    for n, e in enumerate(search.get("ranked") or [], start=1):
        if not isinstance(e, dict) or not isinstance(e.get("code"), str):
            continue
        code = e["code"][:200]
        insp = inspections.get(e["code"])
        selected = chosen is not None and chosen == e["code"]
        out.append(
            s.DiscoveryCandidate(
                candidate_kind="search_result",
                job_id=job["id"],
                proposal_id=UUID(str(proposal_id)) if selected and proposal_id else None,
                origin=origin,  # type: ignore[arg-type]
                provider=str(res.get("catalogue") or payload.get("catalogue") or "eurostat")[:100],
                dataset_code=code,
                requirement_id=res.get("requirement_id"),
                rank=n,
                score=_num(e.get("score")),
                jaccard=_num(e.get("jaccard")),
                inspected=None if insp is None else bool(insp.get("ok")),
                inspection_error=None if insp is None or insp.get("error") is None else str(insp.get("error"))[:500],
                selected=selected,
                discovery_status=str(res.get("status")) if res.get("status") else None,
                gate_outcome=gate.get("outcome") if selected else None,
                recorded_at=job["finished_at"] or job["updated_at"],
            )
        )
    return out


@router.get("/discovery/candidates", response_model=s.DiscoveryCandidateListResponse)
def list_discovery_candidates(
    request: Request,
    limit: int = Query(200, ge=1, le=1000, description="Maximum candidates returned"),
) -> s.DiscoveryCandidateListResponse:
    """Candidates from the latest completed discovery jobs (their recorded search) and connector proposals."""
    t = Timer()
    with read_conn(request) as conn:
        jobs = [
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT id, payload, result_ref, finished_at, updated_at FROM ops.job
               WHERE job_type = 'discovery' AND status = 'succeeded' AND result_ref IS NOT NULL
               ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT :n"""
                ),
                {"n": DISCOVERY_JOB_SCAN},
            ).mappings()
        ]
        proposals = [
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT p.id, p.proposal_type, p.permission_tier, p.summary, p.recorded_at,
                      ARRAY(SELECT r.decision FROM ops.proposal_review r WHERE r.proposal_id = p.id
                            ORDER BY r.recorded_at) AS reviews,
                      (SELECT g.decision FROM ops.gate_result g WHERE g.proposal_id = p.id
                       ORDER BY g.recorded_at DESC LIMIT 1) AS gate_outcome
               FROM ops.proposal p WHERE p.proposal_type LIKE 'connector%'
               ORDER BY p.recorded_at DESC, p.id DESC LIMIT :n"""
                ),
                {"n": limit + 1},
            ).mappings()
        ]
        gate_ids = []
        for j in jobs:
            pid = _gate(j["result_ref"] if isinstance(j["result_ref"], dict) else {}).get("proposal_id")
            try:
                gate_ids.append(UUID(str(pid)))
            except ValueError:
                continue
        proposal_dataset = {
            str(r[0]): r[1]
            for r in conn.execute(
                text("SELECT id, summary->'scope'->>'dataset' FROM ops.proposal WHERE id = ANY(:i)"), {"i": gate_ids}
            )
            if r[1]
        }
    items: list[s.DiscoveryCandidate] = []
    proposal_origin: dict[str, str | None] = {}
    for job in jobs:
        found = _job_candidates(job, proposal_dataset)
        items.extend(found)
        for c in found:
            if c.proposal_id is not None:
                proposal_origin[str(c.proposal_id)] = c.origin
    for p in proposals:
        scope = (p["summary"] or {}).get("scope") if isinstance(p["summary"], dict) else None
        scope = scope if isinstance(scope, dict) else {}
        items.append(
            s.DiscoveryCandidate(
                candidate_kind="connector_proposal",
                proposal_id=p["id"],
                origin=proposal_origin.get(str(p["id"])),  # type: ignore[arg-type]
                dataset_code=str(scope["dataset"])[:200] if scope.get("dataset") else None,
                requirement_id=None,
                selected=True,
                proposal_type=p["proposal_type"],
                permission_tier=p["permission_tier"],
                gate_outcome=p["gate_outcome"],
                reviews=list(p["reviews"] or []),
                recorded_at=p["recorded_at"],
            )
        )
    items.sort(key=lambda c: c.recorded_at, reverse=True)
    truncated = len(items) > limit
    items = items[:limit]
    org = origin_of(c.origin for c in items)
    warnings = [*origin_warnings(org), CANDIDATE_NOTE]
    if truncated:
        warnings.append(s.ApiWarning(code="truncated", message=f"More candidates exist than limit={limit}"))
    if len(jobs) == DISCOVERY_JOB_SCAN:
        warnings.append(
            s.ApiWarning(
                code="job_scan_bounded",
                message=f"Only the latest {DISCOVERY_JOB_SCAN} completed discovery jobs are read",
            )
        )
    return s.DiscoveryCandidateListResponse(
        data=items,
        context_mode="operations",
        origin=org,
        modality="metadata",  # type: ignore[arg-type]
        warnings=warnings,
        cost=t.cost(len(items)),
    )
