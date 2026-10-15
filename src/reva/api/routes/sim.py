"""Scenarios, simulations, replay, evaluations, discovery and the typed QuerySpec endpoint.

Long work returns 202 + job_id + status URL. Every POST that starts work
requires Idempotency-Key. Unsupported requests return typed 422 problems; the
API never fabricates outputs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from reva.api import schemas as s
from reva.api.auth import Principal, require_scope
from reva.api.deps import (
    DEFAULT_PAGE,
    SCENARIO_BANNER,
    Timer,
    decode_cursor,
    get_engine,
    idempotency_key,
    link,
    origin_warnings,
    page,
    read_conn,
    release_ref,
    uncertainty_summary,
    units_of,
)
from reva.api.errors import ApiProblem, NotFound
from reva.api.routes.beliefs import beliefs_response
from reva.api.routes.evidence import timeseries_response
from reva.api.routes.system import fetch_job, job_model
from reva.api.routes.world import relations_response
from reva.belief.snapshots import release_by_id
from reva.kernel.errors import CapabilityGap, MissingInput, ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT, load_yaml
from reva.simulation.compile import preflight_missing
from reva.simulation.spec import RESERVED_FIDELITY, SUPPORTED_FIDELITY, validate_scenario_spec

router = APIRouter(prefix="/v1", tags=["simulation"], dependencies=[Depends(require_scope("read"))])
query_router = APIRouter(prefix="/v1", tags=["query"], dependencies=[Depends(require_scope("read"))])



@lru_cache(maxsize=1)
def model_registry() -> dict[str, dict[str, Any]]:
    models = load_yaml(REGISTRY_ROOT / "models" / "methods.yaml")["models"]
    return {f"{m['model_key']}@{m['version']}": m for m in models}


def submit_job(conn: Connection, job_type: str, payload: dict[str, Any], key: str, actor: str) -> UUID:
    try:
        with conn.begin_nested():
            jid = conn.execute(
                text("SELECT ops.submit_job(:t, CAST(:p AS jsonb), :k, 0, :a)"),
                {"t": job_type, "p": json.dumps(payload, sort_keys=True), "k": key, "a": actor},
            ).scalar_one()
    except DBAPIError as e:
        msg = str(getattr(e, "orig", e))
        if "idempotency key reused" in msg:
            raise ApiProblem(409, "idempotency_conflict",
                             "Idempotency-Key was already used for a different request") from None
        raise
    return UUID(str(jid))


def _accepted(response: Response, job_id: UUID, job_type: str, status: str, status_url: str) -> s.JobAccepted:
    response.status_code = 202
    response.headers["Location"] = status_url
    return s.JobAccepted(job_id=job_id, job_type=job_type, status=status, status_url=status_url)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIO_SQL = """
SELECT sc.id, sc.context_id, c.origin, sc.spec, sc.spec_digest, sc.spec_sha256, sc.created_by, sc.recorded_at,
       c.knowledge_cutoff, sc.baseline_release_id
FROM sim.scenario sc JOIN meta.context c ON c.id = sc.context_id
"""


def scenario_model(conn: Connection, row: dict[str, Any]) -> s.Scenario:
    rel = release_by_id(conn, row["baseline_release_id"])
    assert rel is not None  # FK + trigger guarantee a published baseline
    return s.Scenario(
        id=row["id"], context_id=row["context_id"], origin=row["origin"], title=row["spec"].get("title", ""),
        baseline_release=release_ref(rel), knowledge_cutoff=row["knowledge_cutoff"], spec=row["spec"],  # type: ignore[arg-type]
        spec_digest=row["spec_digest"], spec_sha256=row["spec_sha256"], created_by=row["created_by"],
        recorded_at=row["recorded_at"],
    )


def fetch_scenario(conn: Connection, scenario_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(text(SCENARIO_SQL + " WHERE sc.id = :i"), {"i": scenario_id}).mappings().one_or_none()
    return dict(row) if row else None


@router.post("/scenarios", response_model=s.ScenarioCreated, status_code=201,
             responses={422: {"model": s.Problem}})
def create_scenario(
    request: Request,
    response: Response,
    spec: dict[str, Any] = Body(..., description="ScenarioSpec v1 (contracts/scenario.schema.json)"),
    principal: Principal = Depends(require_scope("simulate")),
) -> s.ScenarioCreated:
    """Immutable, content-addressed scenario definition pinned to a published baseline release.

    Idempotent by spec digest (an identical spec returns the existing scenario).
    """
    parsed = validate_scenario_spec(spec)
    try:
        cutoff = datetime.fromisoformat(parsed.knowledge_cutoff.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationProblem("knowledge_cutoff is not an ISO-8601 timestamp") from None
    if cutoff.tzinfo is None:
        raise ValidationProblem("knowledge_cutoff must include a timezone offset")
    with get_engine(request).begin() as conn:
        rel = release_by_id(conn, UUID(parsed.baseline_release_id))
        if rel is None or rel.get("sequence") is None:
            raise ValidationProblem("baseline_release_id is not a published belief release",
                                    baseline_release_id=parsed.baseline_release_id)
        if cutoff < rel["knowledge_cutoff"]:
            raise ValidationProblem(
                "scenario knowledge_cutoff precedes the baseline release's knowledge cutoff (would leak later "
                "information into the scenario)", baseline_cutoff=rel["knowledge_cutoff"].isoformat())
        if cutoff > datetime.now(UTC):
            raise ValidationProblem("scenario knowledge_cutoff is in the future")
        # Scenario identity is the sha256 of the canonical spec bytes (computed the same way by
        # sim.create_scenario); spec_digest is advisory metadata only.
        canonical = parsed.canonical_bytes()
        spec_sha = hashlib.sha256(canonical).hexdigest()
        existed = conn.execute(text("SELECT id FROM sim.scenario WHERE spec_sha256 = :h"),
                               {"h": spec_sha}).scalar_one_or_none()
        try:
            with conn.begin_nested():
                sid = conn.execute(
                    text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :canonical_bytes, :digest, :actor)"),
                    {"spec": json.dumps(parsed.as_json()), "canonical_bytes": canonical,
                     "digest": parsed.digest(), "actor": principal.actor},
                ).scalar_one()
        except IntegrityError as e:
            # A concurrent identical request committed first (UNIQUE spec_sha256): return that scenario.
            if getattr(getattr(e, "orig", None), "sqlstate", None) != "23505":
                raise
            existed = sid = conn.execute(text("SELECT id FROM sim.scenario WHERE spec_sha256 = :h"),
                                         {"h": spec_sha}).scalar_one_or_none()
            if sid is None:
                raise
        if existed is None:
            # sim.create_scenario serialises identical creators (advisory lock) and returns the row a
            # concurrent caller committed while we waited: that is an existing scenario, not a new one.
            inserted = conn.execute(text("SELECT meta.inserted_in_current_xact(xmin) FROM sim.scenario "
                                         "WHERE id = :i"), {"i": sid}).scalar_one_or_none()
            if not inserted:
                existed = sid
        row = fetch_scenario(conn, sid)
    assert row is not None
    if existed is not None:
        response.status_code = 200
    response.headers["Location"] = f"/v1/scenarios/{sid}"
    return s.ScenarioCreated(
        scenario_id=row["id"], context_id=row["context_id"], spec_digest=row["spec_digest"],
        spec_sha256=row["spec_sha256"], baseline_release_id=row["baseline_release_id"],
        knowledge_cutoff=row["knowledge_cutoff"], created_by=row["created_by"], recorded_at=row["recorded_at"])


def scenario_valid_time(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The scenario reference year as a half-open UTC interval [Jan 1 Y, Jan 1 Y+1).

    Scenario outputs are annual accounting quantities for one reference year; ``None`` when the stored spec
    carries no usable year (never a guessed period).
    """
    year = spec.get("reference_year")
    if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year < 9999:
        return None
    return {"start": f"{year:04d}-01-01T00:00:00Z", "end": f"{year + 1:04d}-01-01T00:00:00Z", "bounds": "[)",
            "basis": "scenario_reference_year", "reference_year": year}


def _scenario_envelope(sc: s.Scenario) -> dict[str, Any]:
    return {
        "context_id": sc.context_id, "context_mode": "simulation", "origin": sc.origin,
        "modality": "assumption_conditioned_scenario", "baseline_release": sc.baseline_release,
        "knowledge_cutoff": sc.knowledge_cutoff, "valid_time": scenario_valid_time(sc.spec),
        "warnings": [*origin_warnings(sc.origin), SCENARIO_BANNER],
    }


@router.get("/scenarios", response_model=s.ScenarioListResponse)
def list_scenarios(
    request: Request, cursor: str | None = None, limit: int = Query(DEFAULT_PAGE, ge=1, le=1000),
) -> s.ScenarioListResponse:
    t = Timer()
    cur = decode_cursor(cursor, "tu")
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            SCENARIO_SQL + """ WHERE (CAST(:c0 AS timestamptz) IS NULL
                 OR (sc.recorded_at, sc.id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
               ORDER BY sc.recorded_at DESC, sc.id DESC LIMIT :lim"""),
            {"c0": cur[0] if cur else None, "c1": cur[1] if cur else None, "lim": limit + 1}).mappings()]
        rows, nxt = page(rows, limit, lambda r: (r["recorded_at"].isoformat(), r["id"]))
        data = [scenario_model(conn, r) for r in rows]
    origins = {d.origin for d in data}
    org = "none" if not origins else (origins.pop() if len(origins) == 1 else "mixed")
    return s.ScenarioListResponse(data=data, context_mode="simulation", origin=org,  # type: ignore[arg-type]
                                  modality="assumption_conditioned_scenario",
                                  warnings=[*origin_warnings(org), SCENARIO_BANNER], cost=t.cost(len(data)),
                                  next_cursor=nxt)


@router.get("/scenarios/{scenario_id}", response_model=s.ScenarioResponse, responses={404: {"model": s.Problem}})
def get_scenario(scenario_id: UUID, request: Request) -> s.ScenarioResponse:
    t = Timer()
    with read_conn(request) as conn:
        row = fetch_scenario(conn, scenario_id)
        if row is None:
            raise NotFound("scenario not found", scenario_id=str(scenario_id))
        sc = scenario_model(conn, row)
    return s.ScenarioResponse(data=sc, provenance_links=[link("scenario", sc.id)], cost=t.cost(1),
                              assumptions=_assumptions(sc.spec), **_scenario_envelope(sc))


def _assumptions(spec: dict[str, Any]) -> list[str]:
    a = spec.get("assumptions", {})
    iv = spec.get("intervention", {})
    out = [
        f"Intervention: {iv.get('kind')} import x{iv.get('import_multiplier')} export x{iv.get('export_multiplier')}",
        f"Quantity response: {a.get('quantity_response')}",
        "No general equilibrium" if a.get("no_general_equilibrium") else "General equilibrium not excluded",
        f"Horizon: {spec.get('horizon', {}).get('periods')} {spec.get('horizon', {}).get('unit')}",
        f"Missing input policy: {spec.get('missing_input_policy')}",
    ]
    grid = a.get("elasticity_grid") or []
    if grid:
        out.append(f"Elasticity grid ({len(grid)} points) is a scenario assumption with no probability "
                   "interpretation")
    m = model_registry().get(f"{spec.get('model_key')}@{spec.get('model_version')}")
    if m:
        out += [str(x) for x in m.get("assumptions", [])]
    return out


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------


def _preflight_inputs(conn: Connection, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Inputs the pinned baseline must hold as non-missing values (missing_input_policy=fail)."""
    m = model_registry().get(f"{spec['model_key']}@{spec['model_version']}")
    if m is None:
        raise CapabilityGap(f"model {spec['model_key']}@{spec['model_version']} is not registered")
    ports = [p for p in m.get("input_ports", []) if p.get("concept")]
    missing: list[dict[str, Any]] = []
    for iso in spec["countries"]:
        for p in ports:
            row = conn.execute(text(
                """SELECT e.value FROM belief.estimate e
                   JOIN ontology.concept_version c ON c.id = e.concept_version_id
                   JOIN world.support sp ON sp.id = e.support_id
                   JOIN world.alias al ON al.entity_id = e.subject_id AND al.provider = 'iso3166_alpha3'
                   WHERE e.release_id = :r AND c.concept_id = :c AND al.external_key = :iso
                     AND EXTRACT(YEAR FROM lower(sp.valid_range)) = :y
                   ORDER BY (e.value->>'kind' = 'missing') LIMIT 1"""),
                {"r": spec["baseline_release_id"], "c": p["concept"], "iso": iso,
                 "y": spec["reference_year"]}).scalar_one_or_none()
            if row is None or row.get("kind") == "missing":
                missing.append({"country": iso, "input": p["name"], "concept": p["concept"],
                                "year": spec["reference_year"],
                                "state": "absent" if row is None else f"missing:{row.get('reason')}"})
    return missing


@router.post("/simulations", response_model=s.JobAccepted, status_code=202,
             responses={400: {"model": s.Problem}, 404: {"model": s.Problem}, 409: {"model": s.Problem},
                        422: {"model": s.Problem}})
def create_simulation(
    body: s.SimulationRequest,
    request: Request,
    response: Response,
    key: str = Depends(idempotency_key),
    principal: Principal = Depends(require_scope("simulate")),
) -> s.JobAccepted:
    """Enqueue a run of a scenario at a supported fidelity (202 + job)."""
    if body.fidelity in RESERVED_FIDELITY:
        raise CapabilityGap(
            f"fidelity {body.fidelity} is reserved and not available in V1", capability=f"fidelity.{body.fidelity}",
            supported=list(SUPPORTED_FIDELITY))
    if body.fidelity not in SUPPORTED_FIDELITY:
        raise ValidationProblem(f"unknown fidelity {body.fidelity!r}", supported=list(SUPPORTED_FIDELITY))
    with get_engine(request).begin() as conn:
        row = fetch_scenario(conn, body.scenario_id)
        if row is None:
            raise NotFound("scenario not found", scenario_id=str(body.scenario_id))
        spec = row["spec"]
        if body.fidelity != "L0" and body.fidelity != spec.get("fidelity"):
            raise ValidationProblem(
                f"fidelity {body.fidelity} does not match the scenario's declared fidelity {spec.get('fidelity')}; "
                "create a scenario for that fidelity (L0 is always allowed)")
        # skip_missing_countries / ALL: the worker computes complete countries and lists the skipped ones
        skip_ok = spec.get("missing_input_policy") == "skip_missing_countries" or spec.get("countries") == ["ALL"]
        missing = [] if skip_ok else preflight_missing(conn, spec)  # per-plugin (scalars and datasets)
        if missing:
            raise MissingInput(
                "required inputs are missing from the pinned baseline release; the run is rejected rather than "
                "imputed (missing_input_policy=fail)", missing=missing)
        jid = submit_job(conn, "simulation", {"scenario_id": str(body.scenario_id), "fidelity": body.fidelity},
                         key, principal.actor)
        job = fetch_job(conn, jid)
    assert job is not None
    return _accepted(response, jid, "simulation", job["status"], f"/v1/simulations/{jid}")


@router.get("/simulations/{sim_id}", response_model=s.SimulationStatusResponse,
            responses={404: {"model": s.Problem}})
def get_simulation(sim_id: UUID, request: Request) -> s.SimulationStatusResponse:
    """Job state, run manifest, outcome, results, assumptions, uncertainty and cost (job id or run id)."""
    t = Timer()
    with read_conn(request) as conn:
        job = fetch_job(conn, sim_id)
        if job is not None and job["job_type"] != "simulation":
            raise NotFound("job is not a simulation", id=str(sim_id))
        run = None
        ref_run = (job["result_ref"] or {}).get("run_id") if job is not None else None
        if ref_run:
            # The job's own result names its run; for an idempotent replay that is an earlier job's run.
            try:
                run = conn.execute(text("SELECT * FROM sim.run WHERE id = :r"),
                                   {"r": UUID(str(ref_run))}).mappings().one_or_none()
            except ValueError:
                run = None
        if run is None:
            run = conn.execute(text(
                "SELECT * FROM sim.run WHERE job_id = :i OR id = :i ORDER BY recorded_at DESC LIMIT 1"),
                {"i": sim_id}).mappings().one_or_none()
        if job is None and run is None:
            raise NotFound("simulation not found", id=str(sim_id))
        if job is None and run is not None and run["job_id"]:
            job = fetch_job(conn, run["job_id"])
        scenario_id = run["scenario_id"] if run is not None else UUID(str(job["payload"]["scenario_id"]))  # type: ignore[index]
        sc_row = fetch_scenario(conn, scenario_id)
        sc = scenario_model(conn, sc_row) if sc_row else None
        outcome = None
        results: list[dict[str, Any]] = []
        if run is not None:
            outcome = conn.execute(text("SELECT * FROM sim.run_outcome WHERE run_id = :r"),
                                   {"r": run["id"]}).mappings().one_or_none()
            results = [dict(r) for r in conn.execute(text(
                """SELECT r.*, e.label AS subject_label, c.concept_id AS subject_kind FROM sim.result r
                   LEFT JOIN world.entity e ON e.id = r.subject_id
                   LEFT JOIN ontology.concept_version c ON c.id = e.kind_concept_id
                   WHERE r.run_id = :r ORDER BY r.output_key, e.label, r.variant LIMIT 5000"""),
                {"r": run["id"]}).mappings()]
        model = None
        if sc is not None:
            model = conn.execute(text(
                "SELECT id, model_key, version, approval_status FROM model.version WHERE model_key = :k "
                "AND version = :v"), {"k": sc.spec.get("model_key"), "v": sc.spec.get("model_version")}
            ).mappings().one_or_none()
    data = s.SimulationStatus(
        job=job_model(job) if job else None, scenario=sc,
        run=s.SimRun(id=run["id"], fidelity=run["fidelity"], model_manifest_sha256=run["model_manifest_sha256"],
                     ontology_manifest_sha256=run["ontology_manifest_sha256"], seed_plan=run["seed_plan"],
                     execution_profile=run["execution_profile"], recorded_at=run["recorded_at"]) if run else None,
        outcome=s.SimOutcome(
            status=outcome["status"], output_manifest_sha256=outcome["output_manifest_sha256"],
            output_digest=outcome["output_digest"], applicability=outcome["applicability"],
            problems=outcome["problems"], cost=outcome["cost"], cache_hit_of=outcome["cache_hit_of"],
            recorded_at=outcome["recorded_at"]) if outcome else None,
        results=[s.SimResult(
            id=r["id"], output_key=r["output_key"],
            subject=s.EntityRef(id=r["subject_id"], label=r["subject_label"], kind=r["subject_kind"])
            if r["subject_id"] else None,
            variant=r["variant"], value=r["value"], uncertainty=r["uncertainty"], applicability=r["applicability"],
            artifact_sha256=r["artifact_sha256"]) for r in results],
    )
    env: dict[str, Any] = _scenario_envelope(sc) if sc else {
        "context_mode": "simulation", "origin": "none", "modality": "assumption_conditioned_scenario"}
    warnings: list[s.ApiWarning] = list(env.pop("warnings", []))
    if job is not None and run is not None and run["job_id"] is not None and run["job_id"] != job["id"]:
        warnings.append(s.ApiWarning(
            code="idempotent_replay",
            message=f"This job reused run {run['id']} computed by job {run['job_id']} for the same scenario, "
                    "fidelity and baseline; no new computation was performed."))
    if outcome is not None and outcome["status"] != "succeeded":
        env["modality"] = "unsupported"
        warnings.append(s.ApiWarning(code=f"run_{outcome['status']}",
                                     message=f"Run ended with status {outcome['status']}; no outputs are reported"))
    elif outcome is None:
        warnings.append(s.ApiWarning(code="not_finished", message="No outcome recorded yet; see job status"))
    cost = s.CostReport(wall_time_ms=t.cost(len(results)).wall_time_ms, rows=len(results),
                        detail={"run_cost": outcome["cost"]} if outcome else None)
    return s.SimulationStatusResponse(
        data=data, **env,
        units=units_of(r["value"] for r in results),
        model_versions=[s.ModelVersionRef(id=model["id"], model_key=model["model_key"], version=model["version"],
                                          approval_status=model["approval_status"])] if model else [],
        assumptions=_assumptions(sc.spec) if sc else [],
        uncertainty=uncertainty_summary(r["uncertainty"] for r in results),
        applicability=s.Assessment(status="reported", detail=outcome["applicability"]) if outcome
        else s.Assessment(status="not_assessed", reason="run not finished"),
        numerical_error=s.Assessment(status="reported", detail={"problems": outcome["problems"]}) if outcome
        else s.Assessment(status="not_assessed", reason="run not finished"),
        historical_validation=s.Assessment(
            status="not_applicable", reason="assumption-conditioned accounting scenario; not a forecast"),
        provenance_links=([link("scenario", sc.id)] if sc else []) + ([link("run", run["id"])] if run else [])
        + [link("sim_result", r["id"]) for r in results[:50]],
        warnings=warnings, cost=cost,
    )


# ---------------------------------------------------------------------------
# Replay and discovery
# ---------------------------------------------------------------------------


@router.post("/replay", response_model=s.JobAccepted, status_code=202,
             responses={400: {"model": s.Problem}, 409: {"model": s.Problem}, 422: {"model": s.Problem}})
def create_replay(
    body: s.ReplayRequest,
    request: Request,
    response: Response,
    key: str = Depends(idempotency_key),
    principal: Principal = Depends(require_scope("simulate")),
) -> s.JobAccepted:
    """Enqueue one explicit replay mode."""
    if body.cutoff.tzinfo is None:
        raise ValidationProblem("cutoff must include a timezone offset")
    if body.cutoff > datetime.now(UTC):
        raise ValidationProblem("cutoff is in the future")
    payload = body.model_dump(mode="json")
    with get_engine(request).begin() as conn:
        jid = submit_job(conn, "replay", payload, key, principal.actor)
        job = fetch_job(conn, jid)
    assert job is not None
    return _accepted(response, jid, "replay", job["status"], f"/v1/jobs/{jid}")


@router.post("/discovery", response_model=s.JobAccepted, status_code=202,
             responses={400: {"model": s.Problem}, 409: {"model": s.Problem}, 422: {"model": s.Problem}})
def create_discovery(
    body: s.DiscoveryRequest,
    request: Request,
    response: Response,
    key: str = Depends(idempotency_key),
    principal: Principal = Depends(require_scope("ingest")),
) -> s.JobAccepted:
    """Enqueue a bounded official-catalogue discovery job (template-only autonomy).

    V1 supports the Eurostat catalogue for the registered requirements that have a discovery template
    (``reva.autonomy.jobs.DISCOVERABLE_REQUIREMENTS``). The job payload is exactly
    ``{"requirement_id", "mode"}``, the keys the handler reads; anything else is rejected here. A tier-1
    connector change within the signed policy scope may be promoted automatically by the gate.
    """
    settings = request.app.state.settings
    if settings.reva_autonomy_kill_switch or settings.reva_autonomy_mode == "disabled":
        raise CapabilityGap("autonomous discovery is disabled by configuration", capability="discovery")
    if body.catalogue != "eurostat":
        raise CapabilityGap(f"discovery in the {body.catalogue} catalogue is not supported in V1",
                            capability=f"discovery.{body.catalogue}", supported=["eurostat"])
    unsupported = sorted(k for k in ("query", "concept_id", "max_candidates") if getattr(body, k) is not None)
    if unsupported:
        raise CapabilityGap("free-text discovery parameters are not supported in V1; discovery targets a "
                            "registered requirement (requirement_id)", unsupported=unsupported)
    try:
        from reva.autonomy.jobs import DISCOVERABLE_REQUIREMENTS
    except ImportError:
        raise CapabilityGap("the discovery service is not installed in this build", capability="discovery") from None
    if body.requirement_id not in DISCOVERABLE_REQUIREMENTS:
        raise CapabilityGap("requirement has no discovery template", requirement_id=body.requirement_id,
                            supported=list(DISCOVERABLE_REQUIREMENTS))
    if body.mode == "fixture" and settings.reva_env == "production":
        raise ValidationProblem("fixture-mode discovery is not allowed in production")
    payload = {"requirement_id": body.requirement_id, "mode": body.mode}
    with get_engine(request).begin() as conn:
        jid = submit_job(conn, "discovery", payload, key, principal.actor)
        job = fetch_job(conn, jid)
    assert job is not None
    return _accepted(response, jid, "discovery", job["status"], f"/v1/jobs/{jid}")


@router.post("/evaluations", response_model=s.JobAccepted, status_code=202,
             responses={400: {"model": s.Problem}, 409: {"model": s.Problem}, 422: {"model": s.Problem}})
def create_evaluation(
    body: s.EvaluationRequest,
    request: Request,
    response: Response,
    key: str = Depends(idempotency_key),
    principal: Principal = Depends(require_scope("simulate")),
) -> s.JobAccepted:
    """Enqueue a model evaluation (rolling-origin tournament over the latest published release).

    Results are a revised-data retrospective, not a real-time backtest; the naive last-value baseline is
    always scored. Unsupported concepts/horizons are rejected here rather than failing later.
    """
    try:
        from reva.evaluation import tournament_service as ts
    except ImportError:
        raise CapabilityGap("the model evaluation service is not installed in this build",
                            capability="evaluation") from None
    payload: dict[str, Any] = {}
    if body.concepts is not None:
        bad = sorted(set(body.concepts) - set(ts.SUPPORTED_CONCEPTS))
        if bad:
            raise CapabilityGap("concepts are not supported by the evaluation service (positive annual series only)",
                                unsupported=bad, supported=list(ts.SUPPORTED_CONCEPTS))
        payload["concepts"] = sorted(set(body.concepts))
    if body.horizons is not None:
        if any(not 1 <= h <= ts.MAX_HORIZON for h in body.horizons):
            raise ValidationProblem(f"horizons must be integers in [1, {ts.MAX_HORIZON}]")
        payload["horizons"] = sorted(set(body.horizons))
    if body.seed is not None:
        payload["seed"] = body.seed
    with get_engine(request).begin() as conn:
        jid = submit_job(conn, "evaluation", payload, key, principal.actor)
        job = fetch_job(conn, jid)
    assert job is not None
    return _accepted(response, jid, "evaluation", job["status"], f"/v1/jobs/{jid}")


# ---------------------------------------------------------------------------
# QuerySpec
# ---------------------------------------------------------------------------


@query_router.post(
    "/query",
    response_model=s.TimeseriesResponse | s.BeliefListResponse | s.RelationListResponse,
    responses={422: {"model": s.Problem}},
)
def run_query(request: Request, spec: s.QuerySpec = Body(..., discriminator="kind")) -> Any:  # type: ignore[valid-type]
    """Validated typed QuerySpec (timeseries, beliefs, relations). No SQL, GraphQL or DSL."""
    t = Timer()
    with read_conn(request) as conn:
        if isinstance(spec, s.TimeseriesQuery):
            if spec.as_of is not None and spec.as_of.tzinfo is None:
                raise ValidationProblem("as_of must include a timezone offset")
            return timeseries_response(conn, t, subject=spec.subject_id, concept=spec.concept, as_of=spec.as_of,
                                       revisions=spec.revisions)
        if isinstance(spec, s.BeliefsQuery):
            if spec.as_of is not None and spec.as_of.tzinfo is None:
                raise ValidationProblem("as_of must include a timezone offset")
            return beliefs_response(conn, t, release_id=spec.release_id, as_of=spec.as_of, origin=None,
                                    subjects=list(spec.subject_ids) or None, concepts=list(spec.concepts) or None,
                                    year=None, cursor=None, limit=spec.limit)
        if isinstance(spec, s.RelationsQuery):
            if spec.as_of is not None and spec.as_of.tzinfo is None:
                raise ValidationProblem("as_of must include a timezone offset")
            return relations_response(conn, t, entity_id=spec.entity_id, layer=spec.layer, period=spec.period,
                                      as_of=spec.as_of, origin=None, depth=spec.depth, max_edges=spec.max_edges,
                                      release_id=spec.release_id)
    raise CapabilityGap("unsupported query kind")
