"""Decision engine (V5, agent PRED-ENGINE): decision → distributions per variable and horizon.

``GET /v1/predict/decisions`` lists the reference decisions and the instruments a decision can be composed from;
``POST /v1/predict/decision`` propagates one decision (a reference id or a composed decision) read-only through
attributed effect estimates (PRED-EFFECT's ``kg.effect_estimate_current``) and REVA's simulators (IO price model,
crude exposure, retirement age), with Monte Carlo over the uncertainty the estimates publish.

Every result is labelled **"scénario non validé — pas une probabilité"** unless the exact model chain has a
back-test and a calibration record (none today). Unquantified causal links are listed without numbers; one
estimate per edge and one path per variable (nothing summed); every hop names its estimate key and source.
Read scope; nothing is written.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import text

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import read_conn
from reva.api.errors import NotFound
from reva.predict import engine as pe
from reva.predict.decision import Decision, instruments, reference_decisions

router = APIRouter(prefix="/v1/predict", tags=["prediction"], dependencies=[Depends(require_scope("read"))])

MAX_API_DRAWS = 5000
UNVALIDATED_WARNING = s.ApiWarning(code="unvalidated_scenario", message=pe.LABEL_UNVALIDATED)
NOT_ADVICE_WARNING = s.ApiWarning(
    code="not_financial_advice", message="Research output, not financial advice or a recommendation."
)


# ---------------------------------------------------------------------------------------------- models
class InstrumentOut(s.ApiModel):
    id: str
    label_fr: str
    label_en: str
    magnitude_units: list[str]
    direct_changes: list[dict[str, Any]]
    plugins: list[str]


class DecisionsData(s.ApiModel):
    decisions: list[dict[str, Any]] = Field(
        description="Reference decisions (registry/predict/reference_decisions.yaml)"
    )
    instruments: list[InstrumentOut]
    label: str = Field(description="Label every propagation result carries until its chain is validated")


class DecisionsResponse(s.Envelope):
    data: DecisionsData


class PredictRequest(s.ApiModel):
    reference_id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$", description="A reference decision")
    decision: Decision | None = Field(None, description="A composed decision (instead of reference_id)")
    draws: int = Field(2000, ge=100, le=MAX_API_DRAWS, description="Monte-Carlo draws")
    seed: int = Field(0, ge=0, le=2**63 - 1, description="Run seed of the named counter-based streams")
    as_of_max: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Use only estimates knowable at this date (replays)"
    )
    origin: Literal["live", "test_fixture", "any"] = "any"


class Quantiles(s.OpenModel):
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    mean: float
    degenerate: bool = Field(description="All draws equal: no published uncertainty on this value's chain")


class PeriodValue(s.OpenModel):
    period: str
    missing_reason: str | None = Field(description="Why this period has no value (e.g. lag_not_published)")
    p05: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p95: float | None = None
    mean: float | None = None
    degenerate: bool | None = None
    scenario_level: dict[str, Any] | None = Field(
        None, description="Baseline (persistence of the latest observation) plus the change, when units compose"
    )


class VariableResult(s.OpenModel):
    id: str
    variable: dict[str, Any]
    geo: str
    unit: str = Field(description="Change unit: percent, pp, log_points, pp_gdp, level:<unit>")
    kind: Literal["estimate_chain", "plugin_output"]
    frequency: Literal["quarter", "year", "undated"]
    depth: int
    path: list[str] = Field(description="Hop ids from the decision to this variable")
    periods: list[PeriodValue]
    undated: dict[str, Any] | None
    baseline: dict[str, Any] | None
    uncertainty_complete: bool
    point_estimates: list[str] = Field(description="Estimates on the path without published uncertainty")
    notes: list[str]


class Validation(s.OpenModel):
    status: Literal["unvalidated", "validated"]
    label: str
    chain_signature: str


class PredictionData(s.OpenModel):
    engine: str
    label: str
    validation: Validation
    disclaimers_fr: list[str]
    decision: dict[str, Any]
    settings: dict[str, Any]
    direct_changes: list[dict[str, Any]]
    variables: list[VariableResult]
    hops: list[dict[str, Any]] = Field(description="Every hop: decision, estimate (key, source, coefficient) or plugin")
    unquantified_links: list[dict[str, Any]] = Field(description="Liens non chiffrés: never given a number")
    excluded_links: list[dict[str, Any]]
    alternative_paths: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    effect_layer: dict[str, Any]
    chain: list[str]
    provenance: dict[str, Any] | None = None


class PredictResponse(s.Envelope):
    data: PredictionData


# ---------------------------------------------------------------------------------------------- routes
@router.get("/decisions", response_model=DecisionsResponse)
def list_decisions() -> DecisionsResponse:
    """Reference decisions and the instruments a decision can be composed from."""
    env: dict[str, Any] = {
        "context_mode": "registry",
        "origin": "none",
        "modality": "metadata",
        "warnings": [UNVALIDATED_WARNING],
    }
    data = DecisionsData(
        decisions=[d.model_dump(mode="json") for d in reference_decisions().values()],
        instruments=[InstrumentOut(**i.as_json()) for i in instruments().values()],
        label=pe.LABEL_UNVALIDATED,
    )
    return DecisionsResponse(data=data, **env)


def _store(request: Request) -> Any:
    st = getattr(request.app.state, "atlas_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.atlas_store = st
    return st


@router.post("/decision", response_model=PredictResponse, responses={404: {"description": "Unknown reference id"}})
def predict_decision(body: PredictRequest, request: Request) -> PredictResponse:
    """Propagate one decision (read-only). Distributions reflect only the published uncertainty of the
    estimates used; the result is an unvalidated scenario, not a probability or a forecast."""
    from reva.kernel.errors import ValidationProblem
    from reva.predict.run import run_on_connection

    if (body.reference_id is None) == (body.decision is None):
        raise ValidationProblem("give exactly one of reference_id or decision")
    if body.reference_id is not None:
        d = reference_decisions().get(body.reference_id)
        if d is None:
            raise NotFound(f"unknown reference decision {body.reference_id!r}")
    else:
        assert body.decision is not None
        d = body.decision
    with read_conn(request) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        out = run_on_connection(
            conn, _store(request), d, draws=body.draws, seed=body.seed, as_of_max=body.as_of_max, origin=body.origin
        )
    warnings = [UNVALIDATED_WARNING, NOT_ADVICE_WARNING]
    if out["effect_layer"].get("layer") != "present":
        warnings.append(
            s.ApiWarning(
                code="effect_layer_absent",
                message="No effect-estimate layer on this server: nothing beyond the simulators can be quantified.",
            )
        )
    env: dict[str, Any] = {
        "context_mode": "simulation",
        "origin": "mixed" if body.origin == "any" else body.origin,
        "modality": "assumption_conditioned_scenario",
        "assumptions": [dc["basis_en"] for dc in out["direct_changes"]],
        "warnings": warnings,
    }
    return PredictResponse(data=PredictionData.model_validate(out), **env)
