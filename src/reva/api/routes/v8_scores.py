"""V8 scoreboard (agent V8SCORES): REVA's numeric forecasts against every official forecaster, wins AND losses.

Read-only. The judged results come from ``registry/scoreboard/tracks.yaml`` (generated from the agents' result
files and reports, validated on load: no row can claim a win without a source file that records it); the live
part (sealed batches, questions, resolutions, next resolution dates, ensemble cells) is read from the forecast
registry tables. Nothing here is advice; a "win" is always stated with its evaluation kind and caveats.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, get_engine, read_conn
from reva.api.errors import NotFound
from reva.v8.scores import live as L
from reva.v8.scores import registry as REG
from reva.v8.scores import service as S

LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/v8/scores", tags=["v8-scores"], dependencies=[Depends(require_scope("read"))])

OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(None, description="Registry rows of this origin; 'any' = both. Default: live in production")
JUDGED = s.ApiWarning(
    code="judged_forecasts",
    message="Scoreboard of pre-registered and prospective evaluations: a win counts only against the official "
    "forecast available at the same date, with proper scores and Holm correction; weak references and naive "
    "benchmarks are counted separately; development results are selection data, not evidence.",
)
NOT_ADVICE = s.ApiWarning(code="not_advice", message="Forecast evaluation; not financial, medical or policy advice.")


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


def _env(t: Timer, rows: int, extra: list[s.ApiWarning] | None = None) -> dict[str, Any]:
    return {
        "context_mode": "evaluation",
        "origin": "none",
        "modality": "evaluation",
        "historical_validation": s.Assessment(
            status="not_assessed",
            reason="this payload IS the record of historical validation (pre-registered and prospective tests)",
        ),
        "warnings": [JUDGED, NOT_ADVICE, *(extra or [])],
        "cost": t.cost(rows),
    }


def _doc() -> dict[str, Any]:
    return REG.load()


# --------------------------------------------------------------------------------------------- models
class ScoreRow(s.OpenModel):
    id: str
    track: str
    target: str
    forecaster: str | None = None
    verdict: Literal["bat", "ne_bat_pas", "egalite", "en_attente"]
    evaluation: str
    benchmark_kind: str
    candidate_kind: str
    metric: str
    ratio: float | None = None
    ratio_range: list[float] | None = None
    p_raw: float | None = None
    p_holm: float | None = None
    holm_family_size: int | None = None
    caveats: list[str] = Field(default_factory=list)
    source: dict[str, Any]


class ScoresPage(s.OpenModel):
    schema_: str = Field(alias="schema")
    headline: dict[str, int] = Field(description="Judged comparisons vs official forecasters, by verdict")
    headline_rule_fr: str
    by_evaluation: dict[str, dict[str, int]]
    weak_reference: dict[str, int]
    naive: dict[str, int]
    n_rows: int
    wins: list[dict[str, Any]]
    forecasters: list[dict[str, Any]]
    matrix: list[dict[str, Any]]
    tracks: list[dict[str, Any]]
    calendar: list[dict[str, Any]]
    vocab: dict[str, Any]
    sources: list[dict[str, Any]]
    stale_sources: list[dict[str, Any]]
    sealed_models: dict[str, int] | None


class ScoresPageResponse(s.Envelope):
    data: ScoresPage


class TrackDetail(s.OpenModel):
    track: dict[str, Any]
    counts: dict[str, int]
    rows: list[ScoreRow]
    holm_families: list[dict[str, Any]]
    charts: list[dict[str, Any]]
    calendar: list[dict[str, Any]]


class TrackResponse(s.Envelope):
    data: TrackDetail


class CellDetail(s.OpenModel):
    key: str
    cell: dict[str, Any]
    rows: list[ScoreRow]


class CellResponse(s.Envelope):
    data: CellDetail


class LiveState(s.OpenModel):
    computed_at: str
    compute_ms: float
    origin: str | None
    registries: list[dict[str, Any]]
    totals: dict[str, int]
    next_resolution: str | None
    upcoming: list[dict[str, Any]]
    upcoming_total: int
    overdue_groups: int
    refreshing: bool
    ensemble: dict[str, Any] | None


class LiveResponse(s.Envelope):
    data: LiveState


class Calendar(s.OpenModel):
    scoreboard: list[dict[str, Any]] = Field(description="Pending pre-registered / prospective tests and dated events")
    registry: list[dict[str, Any]] | None = Field(description="Next expected resolutions of sealed questions (live)")


class CalendarResponse(s.Envelope):
    data: Calendar


class DocText(s.OpenModel):
    path: str
    sha256: str | None
    text: str
    format: Literal["markdown", "yaml"]


class DocResponse(s.Envelope):
    data: DocText


# --------------------------------------------------------------------------------------------- routes
@router.get("", response_model=ScoresPageResponse)
def get_scores(request: Request, origin: OriginParam | None = ORIGIN_QUERY) -> ScoresPageResponse:
    """Page bundle: headline counts, matrix (targets × official forecasters), tracks, pending calendar."""
    t = Timer()
    doc = _doc()
    with read_conn(request) as conn:
        sealed = L.sealed_models(conn, _origin(request, origin))
    p = S.page(doc, sealed=sealed)
    try:  # warm the live registry state in the background: the page asks /live next
        L.SERVICE.get(get_engine(request), _store(request), _origin(request, origin), wait=False)
    except Exception:  # the bundle never depends on the live part
        LOG.warning("v8 scores: live warm-up failed", exc_info=True)
    return ScoresPageResponse(data=ScoresPage(**p), **_env(t, p["n_rows"]))


@router.get("/matrix", response_model=ScoresPageResponse)
def get_matrix(request: Request, origin: OriginParam | None = ORIGIN_QUERY) -> ScoresPageResponse:
    """Same bundle as ``/v1/v8/scores`` (alias kept for clients that address the matrix explicitly)."""
    return get_scores(request, origin)


@router.get("/tracks/{track_id}", response_model=TrackResponse, responses={404: {"model": s.Problem}})
def get_track(
    request: Request,
    track_id: str = Path(pattern=r"^[a-z0-9_]{2,40}$"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> TrackResponse:
    """One track: every row (wins and losses), Holm families, charts copied from its result files, calendar."""
    t = Timer()
    doc = _doc()
    with read_conn(request) as conn:
        sealed = L.sealed_models(conn, _origin(request, origin))
    d = S.track_detail(doc, track_id, sealed=sealed)
    if d is None:
        raise NotFound("unknown scoreboard track", track_id=track_id)
    return TrackResponse(data=TrackDetail(**d), **_env(t, len(d["rows"])))


@router.get("/cells/{key}", response_model=CellResponse, responses={404: {"model": s.Problem}})
def get_cell(key: str = Path(pattern=r"^[a-z0-9_.]{2,60}~[a-z0-9_]{2,40}$")) -> CellResponse:
    """One matrix cell (target~forecaster, or target~naive): its rows."""
    t = Timer()
    d = S.cell_detail(_doc(), key)
    if d is None:
        raise NotFound("no scoreboard row for this cell", key=key)
    return CellResponse(data=CellDetail(**d), **_env(t, len(d["rows"])))


@router.get("/live", response_model=LiveResponse)
def get_live(request: Request, origin: OriginParam | None = ORIGIN_QUERY) -> LiveResponse:
    """Live registry statistics: sealed batches, questions, forecasts, resolved / void / pending, next
    resolution dates per family (with the official forecasters sealed on them), ensemble cells by verdict."""
    t = Timer()
    st = L.SERVICE.get(get_engine(request), _store(request), _origin(request, origin))
    if st is None:  # wait=True always computes
        raise NotFound("live registry state unavailable")
    return LiveResponse(data=LiveState(**st), **_env(t, st["totals"]["questions"]))


@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    request: Request,
    live: bool = Query(True, description="Include the registry's next expected resolutions (slower when cold)"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CalendarResponse:
    """Pending resolutions: pre-registered/prospective scoreboard tests and dated events, plus (live) the next
    expected resolution dates of sealed registry questions."""
    t = Timer()
    doc = _doc()
    o = _origin(request, origin)
    with read_conn(request) as conn:
        sealed = L.sealed_models(conn, o)
    st = L.SERVICE.get(get_engine(request), _store(request), o) if live else None
    reg = st["upcoming"] if st else None
    cal = S.page(doc, sealed=sealed)["calendar"]
    return CalendarResponse(data=Calendar(scoreboard=cal, registry=reg), **_env(t, len(cal) + len(reg or [])))


@router.get("/doc", response_model=DocResponse, responses={404: {"model": s.Problem}})
def get_doc(path: str = Query(min_length=5, max_length=160, pattern=r"^[A-Za-z0-9_./-]+$")) -> DocResponse:
    """Text of a report or protocol referenced by the scoreboard (whitelisted paths only)."""
    t = Timer()
    d = S.report_text(_doc(), path)
    if d is None:
        raise NotFound("document not referenced by the scoreboard", path=path)
    env = _env(t, 1)
    env["modality"] = "metadata"
    return DocResponse(data=DocText(**d), **env)
