"""Prospective forecast registry (V5, agent PRED-REGISTRY): questions, sealed forecasts, outcomes, scores.

Read-only (``read`` scope). Every question was generated from an atlas series, forecast by BASELINE forecasters
only (persistence / random walk, seasonal naive, AR(p) fitted as of issue, historical quantiles) plus, where
declared, the IMF WEO projection attributed to the IMF, and sealed before resolution: the batch manifest's sha256
is re-verified on every read. Outcomes come from the declared atlas table version registered after the seal.
Back-tests are ``final-vintage pseudo-real-time`` (revised data) and never real-time skill. Not advice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.evaluation.registry import read as R
from reva.evaluation.registry.forecasts import all_models
from reva.evaluation.registry.service import BACKTEST_LABEL, LABEL

router = APIRouter(prefix="/v1/forecasts", tags=["forecasts"], dependencies=[Depends(require_scope("read"))])

OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(
    None, description="Rows of this origin only; 'any' = both. Default: live in production, any elsewhere"
)
QType = Literal["quantity", "interval", "binary"]
Status = Literal["pending", "resolved", "void", "not_prospective"]
BASELINE_WARNING = s.ApiWarning(code="baseline_forecasts_only", message=LABEL)
NOT_ADVICE = s.ApiWarning(
    code="not_advice",
    message="Statistical baseline forecasts for measuring skill; not financial or investment advice.",
)
BACKTEST_WARNING = s.ApiWarning(code="final_vintage_pseudo_real_time", message=BACKTEST_LABEL)


def _origin(request: Request, origin: str | None) -> str | None:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _store(request: Request) -> Any:
    st = getattr(request.app.state, "forecast_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.forecast_store = st
    return st


def _models() -> list[s.ModelVersionRef]:
    return [
        s.ModelVersionRef(model_key=k, version=v, approval_status="candidate")
        for k, (v, _d) in sorted(all_models().items())
    ]


def _env(
    t: Timer,
    origins: list[str | None],
    rows: int,
    *,
    modality: str = "forecast",
    extra: list[s.ApiWarning] | None = None,
) -> dict[str, Any]:
    origin = origin_of(origins)
    return {
        "context_mode": "evaluation",
        "origin": origin,
        "modality": modality,
        "model_versions": _models(),
        "historical_validation": s.Assessment(
            status="not_assessed",
            reason="baseline forecasts are what the registry judges; scores accumulate as questions resolve",
        ),
        "warnings": [BASELINE_WARNING, NOT_ADVICE, *(extra or []), *origin_warnings(origin)],
        "cost": t.cost(rows),
    }


# ---------------------------------------------------------------------------------------------- models
class ForecastFamily(s.ApiModel):
    key: str
    label_fr: str
    label_en: str
    concept_id: str | None
    definition: str | None = Field(description="Verbatim definition (or REVA's derivation rule) of the variable")
    definition_source: str | None
    dataset_key: str | None = Field(description="Declared resolution source (atlas table); null while awaited")
    indicator: str | None
    frequency: Literal["M", "Q", "A"]
    value_rule: str
    unit: str
    question_types: list[str]
    horizons: list[int]
    countries: list[str]
    reference_forecasters: list[str]
    status: Literal["available", "awaiting_table", "awaiting_source"]
    status_reason: str | None
    table_latest_recorded_at: datetime | None
    resolution_note: str | None


class FamiliesResponse(s.Envelope):
    data: list[ForecastFamily]


class BatchModel(s.ApiModel):
    model: str | None
    n_issued: int | None
    n_abstained: int | None


class Batch(s.ApiModel):
    batch_sha256: str = Field(description="Seal: sha256 of the canonical batch manifest (questions + forecasts + pins)")
    context_id: str
    cycle: str | None
    origin: str | None
    issued_at: str | None = Field(description="Database-clock time the forecasts were frozen")
    recorded_at: datetime
    n_questions: int | None
    by_type: dict[str, int]
    label: str | None
    models: list[BatchModel]


class BatchesResponse(s.Envelope):
    data: list[Batch]


class QuestionSummary(s.ApiModel):
    question_id: str
    question_key: str
    cycle: str
    family: str
    concept_id: str | None
    label_fr: str
    label_en: str
    geo: str
    geo_label_fr: str
    geo_label_en: str
    reference_period: str
    type: QType
    unit: str
    threshold: str | None = Field(description="Binary questions: 'strictly above' threshold (decimal string)")
    text_fr: str
    text_en: str
    expected_resolution: str
    void_after: str
    resolution_source: str | None
    status: Status
    outcome: str | None = Field(description="Resolved value (decimal string) as published by the declared source")
    n_forecasts: int
    batch_sha256: str
    issued_at: str | None


class QuestionsPage(s.ApiModel):
    total: int
    offset: int
    questions: list[QuestionSummary]


class QuestionsResponse(s.Envelope):
    data: QuestionsPage


class QuestionDetail(s.ApiModel):
    question: dict[str, Any] = Field(description="The sealed question, verbatim from the batch manifest")
    forecasts: list[dict[str, Any]] = Field(description="Frozen forecasts (or abstentions with their reason) + score")
    outcome: dict[str, Any] | None
    status: Status
    seal: dict[str, Any]


class QuestionResponse(s.Envelope):
    data: QuestionDetail


class Relative(s.ApiModel):
    n_common: int
    ratio: float | None = Field(description="mean(metric of model) / mean(metric of persistence) on common questions")
    skill: float | None = Field(description="1 - ratio (positive = better than persistence)")


class LeaderboardRow(s.ApiModel):
    model: str
    version: str
    description: str
    kind: Literal["baseline", "external_reference"]
    n_issued: int
    n_abstained: int
    n_scored: int
    mae: float | None
    crps: float | None
    log_score: float | None
    brier: float | None
    binary_log_score: float | None
    interval_score: float | None
    coverage: float | None
    n_intervals: int
    by_type: dict[str, dict[str, Any]]
    mae_vs_persistence: Relative
    crps_vs_persistence: Relative
    brier_vs_persistence: Relative


class Leaderboard(s.ApiModel):
    label: str
    baseline_for_skill: str
    questions: dict[str, int] = Field(
        description="Question counts by status (pending, resolved, void, not_prospective)"
    )
    models: list[LeaderboardRow]


class LeaderboardResponse(s.Envelope):
    data: Leaderboard


class Calibration(s.ApiModel):
    model: str
    family: str | None
    n_scored: int
    pit_histogram: dict[str, Any]
    reliability: dict[str, Any]
    interval_coverage: dict[str, Any]


class CalibrationResponse(s.Envelope):
    data: Calibration


class Backtest(s.ApiModel):
    backtest_sha256: str
    label: str
    vintage_mode: str | None
    run_at: str | None
    origin: str | None
    years: int | None
    data_tables: dict[str, Any]
    families: list[dict[str, Any]]


class BacktestsResponse(s.Envelope):
    data: list[Backtest]


# ---------------------------------------------------------------------------------------------- routes
@router.get("/families", response_model=FamiliesResponse)
def get_families(request: Request, origin: OriginParam | None = ORIGIN_QUERY) -> FamiliesResponse:
    """Question families (``registry/forecasts/question_specs.yaml``) and whether their source table has landed."""
    t = Timer()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        fams = R.families(conn, o)
    env = _env(t, [o or "none"], len(fams), modality="metadata")
    env["context_mode"] = "registry"
    env["origin"] = o or "none"
    return FamiliesResponse(data=[ForecastFamily(**f) for f in fams], **env)


@router.get("/batches", response_model=BatchesResponse)
def get_batches(request: Request, origin: OriginParam | None = ORIGIN_QUERY) -> BatchesResponse:
    """Sealed issue batches (one per issue run), newest first."""
    t = Timer()
    with read_conn(request) as conn:
        bs = R.batches(conn, _origin(request, origin))
    return BatchesResponse(data=[Batch(**b) for b in bs], **_env(t, [b["origin"] for b in bs], len(bs)))


@router.get("/questions", response_model=QuestionsResponse)
def get_questions(
    request: Request,
    family: str | None = Query(None, max_length=80),
    geo: str | None = Query(None, max_length=16),
    type: QType | None = None,
    status: Status | None = None,
    cycle: str | None = Query(None, pattern=r"^\d{4}-Q[1-4]$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> QuestionsResponse:
    """Registry questions with their status (pending / resolved / void / not prospective) and outcome."""
    t = Timer()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        rows, total = R.questions(
            conn,
            _store(request),
            o,
            family=family,
            geo=geo,
            qtype=type,
            status=status,
            cycle=cycle,
            limit=limit,
            offset=offset,
        )
        origins = [b["origin"] for b in R.batches(conn, o)]
    page = QuestionsPage(total=total, offset=offset, questions=[QuestionSummary(**r) for r in rows])
    return QuestionsResponse(data=page, **_env(t, origins, len(rows)))


@router.get("/questions/{question_id}", response_model=QuestionResponse, responses={404: {"model": s.Problem}})
def get_question(request: Request, question_id: str = Path(pattern=r"^fq_[0-9a-f]{24}$")) -> QuestionResponse:
    """One sealed question: its frozen forecasts (seal re-verified), outcome and scores."""
    t = Timer()
    with read_conn(request) as conn:
        d = R.question(conn, _store(request), question_id)
    if d is None:
        raise NotFound("forecast question not found", question_id=question_id)
    return QuestionResponse(data=QuestionDetail(**d), **_env(t, [d["seal"]["origin"]], len(d["forecasts"])))


@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    request: Request, family: str | None = Query(None, max_length=80), origin: OriginParam | None = ORIGIN_QUERY
) -> LeaderboardResponse:
    """Scores of resolved questions per forecaster (MAE, CRPS, log score, Brier, interval coverage), with skill
    relative to persistence on the questions both scored. Empty until questions resolve."""
    t = Timer()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        lb = R.leaderboard(conn, _store(request), o, family)
        origins = [b["origin"] for b in R.batches(conn, o)]
    return LeaderboardResponse(data=Leaderboard(**lb), **_env(t, origins, len(lb["models"]), modality="evaluation"))


@router.get("/calibration", response_model=CalibrationResponse)
def get_calibration(
    request: Request,
    model: str = Query("forecast.registry_persistence", max_length=80),
    family: str | None = Query(None, max_length=80),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CalibrationResponse:
    """PIT histogram, reliability table (binary) and empirical interval coverage of one forecaster."""
    if model not in all_models():
        raise NotFound("unknown registry forecaster", model=model)
    t = Timer()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        c = R.calibration_of(conn, o, model, family)
        origins = [b["origin"] for b in R.batches(conn, o)]
    return CalibrationResponse(data=Calibration(**c), **_env(t, origins, c["n_scored"], modality="evaluation"))


@router.get("/backtests", response_model=BacktestsResponse)
def get_backtests(
    request: Request,
    calibration: bool = Query(False, description="Include PIT / reliability tables"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> BacktestsResponse:
    """Pseudo-real-time back-tests of the baselines on FINAL-VINTAGE data (labelled; never real-time skill)."""
    t = Timer()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        bts = R.backtests(conn, _store(request), o, with_calibration=calibration)
    env = _env(t, [b["origin"] for b in bts], len(bts), modality="evaluation", extra=[BACKTEST_WARNING])
    return BacktestsResponse(data=[Backtest(**b) for b in bts], **env)
