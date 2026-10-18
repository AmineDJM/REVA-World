"""V8 decision studio (agent V8DECISION): thin page bundles over the V5 decision engine.

* ``GET  /v1/v8/decision/templates``   — reference decisions, instruments, schema limits, comparison presets;
  starts the engine warm-up (atlas catalog + query file) in the background on first call.
* ``POST /v1/v8/decision/run``         — one scenario's bundle: engine result + headline effects, propagation
  graph with attributed edges, evidence (verbatim quotes), competing estimates, « pourquoi pas de résultat ».
  Cached by input hash + data fingerprint.
* ``POST /v1/v8/decision/sensitivity`` — one-at-a-time tornado (5 % / 95 % of each published uncertainty) and
  the Monte-Carlo variance share of each estimate.
* ``POST /v1/v8/decision/compare``     — A vs B side by side, paired difference B − A (common random numbers).
* ``POST /v1/v8/decision/brief``       — Markdown brief with every citation.

Read scope, read-only transactions. Every result is « scénario non validé — pas une probabilité » unless the
engine's judge registry validates its chain; never a forecast; not financial advice.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import Connection, text

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import read_conn
from reva.api.errors import NotFound
from reva.api.routes.predict import NOT_ADVICE_WARNING, UNVALIDATED_WARNING, PredictionData
from reva.predict.decision import Decision
from reva.v8.decision import runner as rn
from reva.v8.decision import service as sv
from reva.v8.decision.templates import templates as _templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/v8/decision", tags=["v8"], dependencies=[Depends(require_scope("read"))])

SCENARIO_ENV: dict[str, Any] = {
    "context_mode": "simulation",
    "modality": "assumption_conditioned_scenario",
}


# ---------------------------------------------------------------------------------------------- models
class ScenarioIn(s.ApiModel):
    reference_id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$", description="A reference decision")
    decision: Decision | None = Field(None, description="A composed decision (instead of reference_id)")
    draws: int = Field(2000, ge=100, le=5000, description="Monte-Carlo draws")
    seed: int = Field(0, ge=0, le=2**63 - 1)
    as_of_max: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Only estimates knowable then")
    origin: Literal["live", "test_fixture", "any"] = "any"


class SensitivityIn(ScenarioIn):
    variable: str | None = Field(None, max_length=300, description="Limit to one variable id (kind:id@GEO)")


class CompareIn(s.ApiModel):
    a: ScenarioIn
    b: ScenarioIn


class BriefIn(ScenarioIn):
    url: str | None = Field(None, max_length=2000, pattern=r"^/v8/decision(\?.*)?$", description="Studio URL to cite")
    sensitivity: bool = True


class TemplatesData(s.OpenModel):
    label: str
    disclaimers_fr: list[str]
    not_advice_fr: str
    references: list[dict[str, Any]]
    instruments: list[dict[str, Any]]
    schema_: dict[str, Any] = Field(alias="schema")
    compare_presets: list[dict[str, Any]]
    engine: dict[str, Any] = Field(description="Warm-up state of the engine in this process")


class TemplatesResponse(s.Envelope):
    data: TemplatesData


class RunData(s.OpenModel):
    scenario_hash: str = Field(description="sha256 of the scenario inputs (decision + draws, seed, as_of, origin)")
    cache_key: str
    cached: bool
    compute_ms: float
    result: PredictionData
    headline: list[dict[str, Any]]
    graph: dict[str, Any] = Field(description="nodes / edges: decision → direct changes → variables; stops as nodes")
    evidence: dict[str, dict[str, Any]] = Field(description="Per estimate key: citation, locator, verbatim quote, …")
    conflicts: list[dict[str, Any]] = Field(description="Competing estimates of one edge, side by side")
    diagnosis: dict[str, Any] = Field(description="Pourquoi pas de résultat: where and why each chain stops")
    honesty: dict[str, Any]
    labels: dict[str, str]


class RunResponse(s.Envelope):
    data: RunData


class SensitivityData(s.OpenModel):
    method: Literal["one_at_a_time"]
    method_fr: str
    z: float
    variables: list[dict[str, Any]]
    runs: int
    compute_ms: float
    scenario_hash: str
    label: str
    cached: bool


class SensitivityResponse(s.Envelope):
    data: SensitivityData


class CompareData(s.OpenModel):
    paired: bool
    paired_fr: str
    changed_fields: list[str]
    variables: list[dict[str, Any]]
    a: dict[str, Any]
    b: dict[str, Any]


class CompareResponse(s.Envelope):
    data: CompareData


class BriefData(s.OpenModel):
    markdown: str
    filename: str
    scenario_hash: str
    label: str
    n_citations: int


class BriefResponse(s.Envelope):
    data: BriefData


# ---------------------------------------------------------------------------------------------- helpers
def _store(request: Request) -> Any:
    st = getattr(request.app.state, "atlas_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.atlas_store = st
    return st


def _scenario(body: ScenarioIn) -> sv.Scenario:
    try:
        d = sv.resolve(body.reference_id, body.decision)
    except sv.UnknownReference as e:
        raise NotFound(f"unknown reference decision {e.args[0]!r}") from e
    return sv.Scenario(d, draws=body.draws, seed=body.seed, as_of_max=body.as_of_max, origin=body.origin)


def _factory(conn: Connection, request: Request, sc: sv.Scenario) -> sv.ContextFactory:
    store = _store(request)
    return lambda d: rn.db_contexts(conn, store, d, origin=sc.origin, as_of_max=sc.as_of_max)


def _env(origin: str, warnings: list[s.ApiWarning], assumptions: list[str] | None = None) -> dict[str, Any]:
    return {
        **SCENARIO_ENV,
        "origin": "mixed" if origin == "any" else origin,
        "warnings": warnings,
        "assumptions": assumptions or [],
    }


def _warnings(result: dict[str, Any] | None = None) -> list[s.ApiWarning]:
    w = [UNVALIDATED_WARNING, NOT_ADVICE_WARNING]
    if result is not None and (result.get("validation") or {}).get("status") == "validated":
        w = [NOT_ADVICE_WARNING]
    if result is not None and (result.get("effect_layer") or {}).get("layer") != "present":
        w.append(
            s.ApiWarning(
                code="effect_layer_absent",
                message="No effect-estimate layer on this server: nothing beyond the simulators can be quantified.",
            )
        )
    return w


# ---------------------------------------------------------------------------------------------- routes
@router.get("/templates", response_model=TemplatesResponse)
def templates(request: Request) -> TemplatesResponse:
    """Reference decisions, instruments and schema limits for the composer (and engine warm-up state)."""
    try:
        rn.start_warmup(request.app.state.engine, _store(request))
    except Exception:  # warm-up is an optimisation only: the first run pays the cost instead
        log.warning("decision engine warm-up not started", exc_info=True)
    data = TemplatesData.model_validate({**_templates(), "engine": rn.engine_state()})
    env: dict[str, Any] = {
        "context_mode": "registry",
        "origin": "none",
        "modality": "metadata",
        "warnings": [UNVALIDATED_WARNING],
    }
    return TemplatesResponse(data=data, **env)


@router.post("/run", response_model=RunResponse, responses={404: {"description": "Unknown reference id"}})
def run(body: ScenarioIn, request: Request) -> RunResponse:
    """One scenario's page bundle (read-only, cached by input hash). An unvalidated scenario, not a forecast."""
    sc = _scenario(body)
    with read_conn(request) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        fp = rn.data_fingerprint(conn)
        bundle, _, hit = sv.run(sc, fp, _factory(conn, request, sc))
    res = bundle["result"]
    data = RunData.model_validate({**bundle, "cached": hit})
    return RunResponse(
        data=data, **_env(sc.origin, _warnings(res), [dc["basis_en"] for dc in res.get("direct_changes") or []])
    )


@router.post("/sensitivity", response_model=SensitivityResponse)
def sensitivity(body: SensitivityIn, request: Request) -> SensitivityResponse:
    """One-at-a-time sensitivity of each result to each estimate's published uncertainty (tornado)."""
    sc = _scenario(body)
    with read_conn(request) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        fp = rn.data_fingerprint(conn)
        out, hit = sv.sensitivity(sc, fp, _factory(conn, request, sc), variable=body.variable)
    return SensitivityResponse(
        data=SensitivityData.model_validate({**out, "cached": hit}), **_env(sc.origin, _warnings())
    )


@router.post("/compare", response_model=CompareResponse)
def compare(body: CompareIn, request: Request) -> CompareResponse:
    """Two scenarios side by side; paired difference B − A when both use the same draws and seed."""
    a, b = _scenario(body.a), _scenario(body.b)
    if a.origin != b.origin or a.as_of_max != b.as_of_max:
        from reva.kernel.errors import ValidationProblem

        raise ValidationProblem("A and B must read the same data (same origin and as_of_max)")
    with read_conn(request) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        fp = rn.data_fingerprint(conn)
        out = sv.compare(a, b, fp, _factory(conn, request, a))
    return CompareResponse(data=CompareData.model_validate(out), **_env(a.origin, _warnings()))


@router.post("/brief", response_model=BriefResponse)
def brief(body: BriefIn, request: Request) -> BriefResponse:
    """Markdown brief of the scenario with every citation (for the Dossier or a download)."""
    sc = _scenario(body)
    with read_conn(request) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        fp = rn.data_fingerprint(conn)
        out = sv.brief(sc, fp, _factory(conn, request, sc), url=body.url, with_sensitivity=body.sensitivity)
    return BriefResponse(data=BriefData.model_validate(out), **_env(sc.origin, _warnings()))


def warm(app: Any) -> str:
    """Start-up warm-up (reva.api.warmup): the decision engine's own background warm-up (atlas, registry, IO)."""
    from reva.api.warmup import request_for

    started = rn.start_warmup(app.state.engine, _store(request_for(app)))
    return "engine warm-up started" if started else "engine warm-up already running or done"
