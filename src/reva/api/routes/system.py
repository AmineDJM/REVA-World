"""Health, session, jobs and metrics."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from reva.api import schemas as s
from reva.api.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL,
    Principal,
    authenticated,
    get_auth,
    require_scope,
)
from reva.api.deps import Timer, decode_cursor, get_engine, page, page_limit, read_conn
from reva.api.errors import ApiProblem, NotFound

health_router = APIRouter(tags=["health"])
session_router = APIRouter(prefix="/v1", tags=["session"])
jobs_router = APIRouter(prefix="/v1", tags=["jobs"], dependencies=[Depends(require_scope("read"))])
metrics_router = APIRouter(tags=["metrics"])

EXPECTED_SCHEMAS = ("meta", "ontology", "world", "evidence", "belief", "model", "sim", "ops")


@health_router.get("/health/live", response_model=s.HealthLive)
def live() -> s.HealthLive:
    """Process liveness only; no database or source calls."""
    return s.HealthLive(status="ok")


@health_router.get("/health/ready", response_model=s.HealthReady, responses={503: {"model": s.HealthReady}})
def ready(request: Request) -> Any:
    """Database reachable, migrations applied, runtime role sees the expected schema."""
    checks: list[s.HealthCheck] = []
    try:
        with get_engine(request).connect() as conn:
            conn.execute(text("SELECT 1"))
            checks.append(s.HealthCheck(name="database", ok=True))
            ver = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
            checks.append(s.HealthCheck(name="migrations", ok=bool(ver), detail=",".join(ver) or "no version"))
            row = conn.execute(
                text(
                    """SELECT current_user AS role,
                         (SELECT count(*) FROM unnest(CAST(:schemas AS text[])) sch
                           WHERE has_schema_privilege(sch, 'USAGE')) AS schemas_visible,
                         has_table_privilege('belief.release', 'SELECT') AS can_read_releases,
                         has_table_privilege('evidence.observation', 'INSERT') AS can_write_evidence"""
                ),
                {"schemas": list(EXPECTED_SCHEMAS)},
            ).mappings().one()
            role_ok = (
                row["role"] == "reva_api"
                and row["schemas_visible"] == len(EXPECTED_SCHEMAS)
                and row["can_read_releases"]
                and not row["can_write_evidence"]
            )
            checks.append(s.HealthCheck(
                name="runtime_role", ok=bool(role_ok),
                detail=f"role={row['role']} schemas={row['schemas_visible']}/{len(EXPECTED_SCHEMAS)}",
            ))
    except ApiProblem:
        raise
    except Exception as e:
        checks.append(s.HealthCheck(name="database", ok=False, detail=type(e).__name__))
    # Object store: reported, never fatal (a wrong bucket/key shows here instead of at first use).
    try:
        from reva.adapters.objects.factory import get_store, probe_store

        p = probe_store(get_store())
        checks.append(s.HealthCheck(name="object_store", ok=p["status"] == "ok", detail=p.get("error")))
    except Exception as e:
        checks.append(s.HealthCheck(name="object_store", ok=False, detail=type(e).__name__))
    ok = bool(checks) and all(c.ok for c in checks if c.name != "object_store")
    body = s.HealthReady(status="ready" if ok else "not_ready", checks=checks)
    return JSONResponse(body.model_dump(mode="json"), status_code=200 if ok else 503)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def _session_info(p: Principal) -> s.SessionInfo:
    return s.SessionInfo(username=p.username, principal=p.actor, auth_method=p.method,  # type: ignore[arg-type]
                         scopes=sorted(p.scopes), expires_at=p.expires_at, csrf_token=p.csrf)


@session_router.post("/session", response_model=s.SessionInfo,
                     responses={401: {"model": s.Problem}, 429: {"model": s.Problem}})
def login(body: s.LoginRequest, request: Request, response: Response) -> s.SessionInfo:
    """Private UI login. Sets an HttpOnly signed session cookie and a CSRF cookie."""
    auth = get_auth(request)
    principal, cookie = auth.login(request, body.username, body.password)
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(SESSION_COOKIE, cookie, max_age=max_age, httponly=True, secure=auth.secure_cookies,
                        samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, principal.csrf or "", max_age=max_age, httponly=False,
                        secure=auth.secure_cookies, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return _session_info(principal)


@session_router.get("/session", response_model=s.SessionInfo, responses={401: {"model": s.Problem}})
def current_session(principal: Principal = Depends(authenticated)) -> s.SessionInfo:
    return _session_info(principal)


@session_router.delete("/session", status_code=204, responses={401: {"model": s.Problem}})
def logout(request: Request, principal: Principal = Depends(authenticated)) -> Response:
    get_auth(request).logout(principal)
    resp = Response(status_code=204)
    auth = get_auth(request)
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=auth.secure_cookies, httponly=True, samesite="strict")
    resp.delete_cookie(CSRF_COOKIE, path="/", secure=auth.secure_cookies, samesite="strict")
    return resp


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

JOB_SQL = """SELECT id, job_type, payload, priority, status, attempts, max_attempts, progress,
  (checkpoint IS NOT NULL OR checkpoint_sha256 IS NOT NULL) AS has_checkpoint, result_ref, last_error,
  created_by, created_at, updated_at, available_at, finished_at FROM ops.job"""


_DB_MARKERS = ("[SQL:", "[parameters:", "(Background on this error", "\nDETAIL:", "\nCONTEXT:", "\nLINE ",
               "\nHINT:")
_DB_EXCEPTIONS = frozenset({"DBAPIError", "ProgrammingError", "IntegrityError", "OperationalError", "DataError",
                            "InternalError", "NotSupportedError", "InterfaceError", "DatabaseError",
                            "StatementError", "PendingRollbackError"})
_DRIVER_PREFIX = re.compile(r"^\((?:psycopg|psycopg2|sqlalchemy)[\w.]*\)\s*")


def public_error(err: Any) -> dict[str, Any] | None:
    """The job error as shown over HTTP: never SQL text, bound parameters or driver context.

    The worker stores ``str(exception)``; for database exceptions that can include the statement and its
    parameters. The full (redacted) text stays in the worker log only.
    """
    if not isinstance(err, dict):
        return None
    out = {k: err[k] for k in ("code", "retryable", "attempt", "exception") if k in err}
    msg = str(err.get("message") or "")
    is_db = str(err.get("exception", "")) in _DB_EXCEPTIONS or any(m in msg for m in _DB_MARKERS)
    if is_db:
        for m in _DB_MARKERS:
            msg = msg.split(m, 1)[0]
        msg = _DRIVER_PREFIX.sub("", msg.strip().splitlines()[0] if msg.strip() else "")
        msg = (msg[:300] + " (database error; details in the worker log)").strip()
        out["message"] = msg
    else:
        out["message"] = msg[:2000]
        if isinstance(err.get("details"), dict):
            out["details"] = err["details"]
    return out


def job_model(row: dict[str, Any]) -> s.Job:
    jid = row["id"]
    links = {"self": f"/v1/jobs/{jid}"}
    if row["job_type"] == "simulation":
        links["simulation"] = f"/v1/simulations/{jid}"
    return s.Job(
        id=jid, job_type=row["job_type"], status=row["status"], payload=row["payload"], priority=row["priority"],
        attempts=row["attempts"], max_attempts=row["max_attempts"], progress=row["progress"] or {},
        has_checkpoint=bool(row["has_checkpoint"]), result_ref=row["result_ref"],
        last_error=public_error(row["last_error"]),
        created_by=row["created_by"], created_at=row["created_at"], updated_at=row["updated_at"],
        available_at=row["available_at"], finished_at=row["finished_at"],
        cancellable=row["status"] in ("queued", "running"), links=links,
    )


def fetch_job(conn: Any, job_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(text(JOB_SQL + " WHERE id = :i"), {"i": job_id}).mappings().one_or_none()
    return dict(row) if row else None


@jobs_router.get("/jobs", response_model=s.JobListResponse)
def list_jobs(
    request: Request,
    job_type: str | None = Query(None, max_length=50),
    status: str | None = Query(None, max_length=30),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.JobListResponse:
    t = Timer()
    cur = decode_cursor(cursor, "tu")
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(
            text(JOB_SQL + """ WHERE (CAST(:jt AS text) IS NULL OR job_type = :jt)
                 AND (CAST(:st AS text) IS NULL OR status = :st)
                 AND (CAST(:c0 AS timestamptz) IS NULL
                      OR (created_at, id) < (CAST(:c0 AS timestamptz), CAST(:c1 AS uuid)))
                 ORDER BY created_at DESC, id DESC LIMIT :lim"""),
            {"jt": job_type, "st": status, "c0": cur[0] if cur else None, "c1": cur[1] if cur else None,
             "lim": limit + 1},
        ).mappings()]
    rows, nxt = page(rows, limit, lambda r: (r["created_at"].isoformat(), r["id"]))
    return s.JobListResponse(data=[job_model(r) for r in rows], context_mode="operations", origin="none",
                             modality="metadata", cost=t.cost(len(rows)), next_cursor=nxt)


@jobs_router.get("/jobs/{job_id}", response_model=s.Job, responses={404: {"model": s.Problem}})
def get_job(job_id: UUID, request: Request) -> s.Job:
    with read_conn(request) as conn:
        row = fetch_job(conn, job_id)
    if row is None:
        raise NotFound("job not found", job_id=str(job_id))
    return job_model(row)


# Scope needed to cancel a job of each type: the scope that may submit (or operate) that type.
# Unknown job types cannot be cancelled through the API.
CANCEL_SCOPE: dict[str, str] = {
    "simulation": "simulate", "replay": "simulate", "evaluation": "simulate", "population": "simulate",
    "backtest": "simulate",
    "ingest": "ingest", "discovery": "ingest", "publish_beliefs": "ingest", "gaps": "ingest",
    "health": "ingest", "gc": "ingest",
}


@jobs_router.post("/jobs/{job_id}/cancel", response_model=s.JobCancelResult,
                  responses={404: {"model": s.Problem}, 403: {"model": s.Problem}})
def cancel_job(job_id: UUID, request: Request, principal: Principal = Depends(authenticated)) -> s.JobCancelResult:
    """Cooperative cancellation; the partial audit trail is preserved.

    Authorised per job type (``CANCEL_SCOPE``): ``simulate`` cancels simulation/replay/evaluation/population
    jobs, ``ingest`` cancels ingestion/publication/discovery/maintenance jobs.
    """
    with get_engine(request).begin() as conn:
        jt = conn.execute(text("SELECT job_type FROM ops.job WHERE id = :i"), {"i": job_id}).scalar_one_or_none()
        if jt is None:
            raise NotFound("job not found", job_id=str(job_id))
        need = CANCEL_SCOPE.get(jt)
        if need is None:
            raise ApiProblem(403, "forbidden", f"jobs of type '{jt}' cannot be cancelled through the API",
                             job_type=jt)
        if need not in principal.scopes:
            raise ApiProblem(403, "forbidden", f"scope '{need}' required to cancel a {jt} job",
                             required_scope=need, job_type=jt)
        st = conn.execute(text("SELECT ops.request_cancel(:j, :a)"), {"j": job_id, "a": principal.actor}).scalar_one()
    return s.JobCancelResult(job_id=job_id, status=st)


# ---------------------------------------------------------------------------
# Metrics (Prometheus text; authenticated)
# ---------------------------------------------------------------------------


class RequestMetrics:
    def __init__(self) -> None:
        self.requests: Counter[tuple[str, int]] = Counter()
        self.duration_ms_sum: Counter[str] = Counter()

    def observe(self, route: str, status: int, ms: float) -> None:
        self.requests[(route, status)] += 1
        self.duration_ms_sum[route] += ms


@metrics_router.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_scope("read"))])
def metrics(request: Request) -> PlainTextResponse:
    m: RequestMetrics = request.app.state.metrics
    lines = ["# HELP reva_http_requests_total HTTP requests by route template and status",
             "# TYPE reva_http_requests_total counter"]
    for (route, status), n in sorted(m.requests.items()):
        lines.append(f'reva_http_requests_total{{route="{route}",status="{status}"}} {n}')
    lines += ["# TYPE reva_http_request_duration_ms_sum counter"]
    for route, v in sorted(m.duration_ms_sum.items()):
        lines.append(f'reva_http_request_duration_ms_sum{{route="{route}"}} {v:.3f}')
    with read_conn(request) as conn:
        rows = conn.execute(text("SELECT job_type, status, count(*) FROM ops.job GROUP BY 1, 2 ORDER BY 1, 2")).all()
        oldest = conn.execute(text(
            "SELECT COALESCE(EXTRACT(EPOCH FROM clock_timestamp() - min(created_at)), 0) FROM ops.job "
            "WHERE status = 'queued'")).scalar_one()
    lines += ["# TYPE reva_jobs gauge"]
    for jt, st, n in rows:
        lines.append(f'reva_jobs{{job_type="{jt}",status="{st}"}} {n}')
    lines += ["# TYPE reva_queue_oldest_queued_seconds gauge", f"reva_queue_oldest_queued_seconds {float(oldest):.3f}"]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
