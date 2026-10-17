"""Effect estimates (V5, agent PRED-EFFECT): attributed effect sizes, list / get / compare / variables.

Read-only (``read`` scope) over ``kg.effect_estimate_current`` via ``reva.effects.query``. REVA asserts no effect:
every estimate is a number attributed to its source (a publication with citation, locator and verbatim quote; a
REVA estimation run with method, data snapshot and as-of; or an identity) and carries an attributed wording.
``/compare`` sets estimates side by side by comparability key and never pools them. Contract:
``docs/dev/v5_effect_interface.md``. ``origin`` as in ``/v1/kg``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, decode_cursor, encode_cursor, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.effects import query as eq
from reva.effects import vocab as ev

router = APIRouter(prefix="/v1/effects", tags=["effects"], dependencies=[Depends(require_scope("read"))])

OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(
    None,
    description="Rows of this origin only; 'any' = live and test_fixture. Default: live in production, any elsewhere",
)
ID_PATTERN = r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.:-]{1,128}|[a-z0-9][a-z0-9_.-]{0,199})$"
KEY_PATTERN = r"^(pub|run|idn):[A-Za-z0-9_.:-]{3,300}$"
ESTIMATES_WARNING = s.ApiWarning(code="attributed_estimates", message=eq.ESTIMATE_NOTE)
NO_POOLING_WARNING = s.ApiWarning(code="no_pooling", message=eq.COMPARE_NOTE)
RESTRICTED_WARNING = s.ApiWarning(
    code="licence_restricted",
    message="Some estimates come from sources without an open licence (rights.restricted): numbers are quoted "
    "with citation; the source documents are archived for provenance only and are not redistributed.",
)

Measure = Literal[ev.MEASURES]  # type: ignore[valid-type]
Method = Literal[ev.METHODS]  # type: ignore[valid-type]
SourceKind = Literal[ev.SOURCE_KINDS]  # type: ignore[valid-type]
CauseKind = Literal[ev.CAUSE_KINDS]  # type: ignore[valid-type]
EffectKind = Literal[ev.EFFECT_KINDS]  # type: ignore[valid-type]


# ---------------------------------------------------------------------------------------------- models
class EffectRef(s.ApiModel):
    kind: str = Field(description="atlas_concept | variable | event_type | event")
    id: str
    label: str = Field(description="English label; '<id> (label not loaded)' when REVA has none")
    label_fr: str
    label_loaded: bool
    definition: str = Field(description="The variable as the SOURCE defines it, verbatim")
    transform: str = Field(description="log_change, percent_change, pp_change, level_change, gap, occurrence, ...")


class EffectUncertainty(s.ApiModel):
    se: float | None
    ci_low: float | None
    ci_high: float | None
    ci_level: float | None
    interval_kind: str | None
    draws: dict[str, Any] | None
    status: Literal["reported", "partial", "not_reported"]


class EffectHorizon(s.ApiModel):
    kind: str
    value: float | None
    unit: str | None


class EffectSource(s.ApiModel):
    kind: Literal["publication", "reva_run", "identity"]
    source_key: str
    dataset: str
    citation: dict[str, Any] | None = Field(description="Reference verbatim (text, short, doi, url, year, ...)")
    locator: str | None = Field(description="Table / figure / page of the number")
    quote: str | None = Field(description="Verbatim passage containing the number as printed")
    selector: dict[str, Any] | None
    transcription: Literal["machine_readable", "transcribed", "transcribed_double_checked", "computed"]
    raw_sha256: str | None = Field(description="Archived source file the number was read from")
    run: dict[str, Any] | None
    identity: dict[str, Any] | None
    rights: dict[str, Any] | None
    retrieved_at: str | None


class EffectEstimate(s.ApiModel):
    estimate_key: str
    row_id: int
    cause: EffectRef
    effect: EffectRef
    measure: str
    unit: str
    shock: dict[str, Any] | None = Field(description="{size, unit} of the shock a 'response' refers to")
    shock_profile: str = Field(
        description="How the cause moves in the experiment: one_time_impulse, sustained, permanent_level_shift, "
        "state_indicator, contemporaneous_relation, cross_sectional or unspecified (lag profiles are responses to it)"
    )
    sign: Literal["positive", "negative", "zero", "ambiguous"]
    point: float | None
    point_kind: str | None
    as_printed: str | None
    uncertainty: EffectUncertainty
    horizon: EffectHorizon
    irf: list[dict[str, Any]] = Field(description="Lag profile [{h, unit, point, se?, ci_low?, ci_high?}]")
    scope: dict[str, Any]
    method: str
    identification: str | None
    meta: dict[str, Any] | None
    source: EffectSource
    as_of: str = Field(description="Knowledge date: when the estimate became knowable (as-of replays: as_of <= t)")
    applicability: str | None = Field(description="REVA curator's transportability note (REVA's words)")
    source_caveats: str | None = Field(description="Caveats verbatim from the source")
    causal_claim_keys: list[str]
    curation: dict[str, Any]
    wording: str
    wording_fr: str
    origin: Literal["live", "test_fixture"]
    recorded_at: str | None
    load_id: int
    matched_via: str | None = Field(None, description="'variable:<id>' when matched through a curated atlas link")


class EffectEstimatesResponse(s.Envelope):
    data: list[EffectEstimate]


class EffectEstimateResponse(s.Envelope):
    data: EffectEstimate


class EffectGroup(s.ApiModel):
    comparability_key: dict[str, Any]
    n: int
    n_with_point: int
    min: float | None
    max: float | None
    median: float | None = Field(description="Descriptive median of the group's points (not a pooled estimate)")
    signs: list[str]
    methods: list[str]
    source_kinds: list[str]
    countries: list[str]
    estimates: list[EffectEstimate]


class EffectComparison(s.ApiModel):
    cause: str
    effect: str
    n_estimates: int
    truncated: bool
    groups: list[EffectGroup]
    note: str


class EffectComparisonResponse(s.Envelope):
    data: EffectComparison


class EffectVariable(s.ApiModel):
    id: str
    labels: dict[str, str]
    definition: str
    definition_by: Literal["reva"]
    unit: str
    atlas_concepts: list[str] = Field(description="Curated links to atlas concepts measuring the same quantity")
    atlas_note: str | None


class EffectVariablesResponse(s.Envelope):
    data: list[EffectVariable]


# ---------------------------------------------------------------------------------------------- helpers
def _origin(request: Request, origin: str | None) -> str:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return origin


def _env(rows: list[dict[str, Any]], t: Timer, *, extra: list[s.ApiWarning] | None = None) -> dict[str, Any]:
    origin = origin_of(r["origin"] for r in rows)
    restricted = any((r["source"].get("rights") or {}).get("restricted") for r in rows)
    return {
        "context_mode": "evidence",
        "origin": origin,
        "modality": "reported_observation",
        "warnings": [ESTIMATES_WARNING, *(extra or [])]
        + ([RESTRICTED_WARNING] if restricted else [])
        + origin_warnings(origin),
        "cost": t.cost(len(rows)),
    }


def _filters(
    cause_kind: str | None,
    effect_kind: str | None,
    country: str | None,
    method: str | None,
    measure: str | None,
    source_kind: str | None,
    as_of_max: date | None,
    include_variables: bool,
) -> dict[str, Any]:
    return {
        "cause_kind": cause_kind,
        "effect_kind": effect_kind,
        "country": country,
        "method": method,
        "measure": measure,
        "source_kind": source_kind,
        "as_of_max": as_of_max,
        "include_variables": include_variables,
    }


CauseQuery = Query(None, pattern=ID_PATTERN, max_length=200, description="Cause id (atlas concept, variable, ...)")
EffectQuery = Query(None, pattern=ID_PATTERN, max_length=200, description="Effect id (atlas concept or variable)")
CountryQuery = Query(None, pattern=r"^[A-Z]{3}$", description="ISO 3166-1 alpha-3 in the estimate scope (or WLD)")
AsOfQuery = Query(None, description="Only estimates knowable at this date (as_of <= as_of_max)")
VarsQuery = Query(True, description="An atlas concept id also matches variables curated as measuring it")


# ---------------------------------------------------------------------------------------------- endpoints
@router.get("/estimates", response_model=EffectEstimatesResponse, responses={400: {"model": s.Problem}})
def list_effect_estimates(
    request: Request,
    cause: str | None = CauseQuery,
    effect: str | None = EffectQuery,
    cause_kind: CauseKind | None = None,
    effect_kind: EffectKind | None = None,
    country: str | None = CountryQuery,
    method: Method | None = None,
    measure: Measure | None = None,
    source_kind: SourceKind | None = None,
    as_of_max: date | None = AsOfQuery,
    include_variables: bool = VarsQuery,
    limit: int = Query(100, ge=1, le=eq.MAX_LIMIT),
    cursor: str | None = Query(None, max_length=200),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EffectEstimatesResponse:
    """Current effect estimates, newest first (each attributed to its source)."""
    t = Timer()
    after = decode_cursor(cursor, "i")
    with read_conn(request) as conn:
        rows = eq.list_estimates(
            conn,
            cause=cause,
            effect=effect,
            origin=_origin(request, origin),
            limit=limit,
            after_id=after[0] if after else None,
            **_filters(cause_kind, effect_kind, country, method, measure, source_kind, as_of_max, include_variables),
        )
    env = _env(rows, t)
    if len(rows) == limit:
        env["next_cursor"] = encode_cursor([rows[-1]["row_id"]])
    return EffectEstimatesResponse(data=[EffectEstimate(**r) for r in rows], **env)


@router.get(
    "/estimates/{estimate_key}",
    response_model=EffectEstimateResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_effect_estimate(
    request: Request,
    estimate_key: str = Path(..., pattern=KEY_PATTERN, max_length=304),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EffectEstimateResponse:
    """One current estimate by key (``pub:…`` / ``run:…`` / ``idn:…``)."""
    t = Timer()
    with read_conn(request) as conn:
        r = eq.get_estimate(conn, estimate_key, origin=_origin(request, origin))
    if r is None:
        raise NotFound("effect estimate not found (or retracted)", estimate_key=estimate_key)
    return EffectEstimateResponse(data=EffectEstimate(**r), **_env([r], t))


@router.get("/compare", response_model=EffectComparisonResponse, responses={422: {"model": s.Problem}})
def compare_effect_estimates(
    request: Request,
    cause: str = Query(..., pattern=ID_PATTERN, max_length=200),
    effect: str = Query(..., pattern=ID_PATTERN, max_length=200),
    cause_kind: CauseKind | None = None,
    effect_kind: EffectKind | None = None,
    country: str | None = CountryQuery,
    method: Method | None = None,
    measure: Measure | None = None,
    source_kind: SourceKind | None = None,
    as_of_max: date | None = AsOfQuery,
    include_variables: bool = VarsQuery,
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EffectComparisonResponse:
    """Every current estimate of (cause, effect), grouped by comparability key; never pooled."""
    t = Timer()
    with read_conn(request) as conn:
        res = eq.compare_estimates(
            conn,
            cause=cause,
            effect=effect,
            origin=_origin(request, origin),
            **_filters(cause_kind, effect_kind, country, method, measure, source_kind, as_of_max, include_variables),
        )
    rows = [e for g in res["groups"] for e in g["estimates"]]
    extra = [NO_POOLING_WARNING]
    if res["truncated"]:
        extra.append(s.ApiWarning(code="truncated", message=f"limited to {eq.MAX_LIMIT} estimates"))
    return EffectComparisonResponse(data=EffectComparison(**res), **_env(rows, t, extra=extra))


@router.get("/variables", response_model=EffectVariablesResponse)
def list_effect_variables(request: Request) -> EffectVariablesResponse:
    """Effect variables (``registry/effects/variables.yaml``) with their curated atlas links."""
    t = Timer()
    vs = eq.effect_variables()
    env: dict[str, Any] = {
        "context_mode": "registry",
        "origin": "none",
        "modality": "reported_observation",
        "cost": t.cost(len(vs)),
    }
    return EffectVariablesResponse(data=[EffectVariable(**x) for x in vs], **env)
