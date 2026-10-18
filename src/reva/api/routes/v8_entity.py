"""V8 Fiche — the universal entity page in ONE call (agent V8FICHE).

* ``GET /v1/v8/entity/{kind}/{id}`` — the page bundle of any entity: a KG record of any kind (country by ISO3,
  compound, gene, disease, drug, company, facility, conflict, taxon…), an anchor fiche (``commodity/crude_oil``), a
  forecast question (``question/fq_…``), an effect estimate (``effect/<key>``) or a source (``source/<key>``).
  Identity with authority ids, 3–6 headline numbers chosen per kind, facts, relations by family, atlas series,
  places and dated statements, causal claims in/out (attributed, tier + grade), effect estimates (attributed),
  forecasts (sealed, judged status), "ce qui manque" with reasons, and the sources with their licences.
* ``GET /v1/v8/entity/{kind}/{id}/relations?family=…`` — the next page of one relation family.

Read-only (``read`` scope). Honesty: every value carries its source chip and licence; Missing is typed and never 0;
no relation family is causal; causal claims and effect estimates are attributed to their sources (REVA asserts
neither); forecasts carry their judged status (a pending question is not judged). Origin as in ``/v1/kg``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import AbstractContextManager
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field
from sqlalchemy import Connection

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, decode_cursor, encode_cursor, read_conn
from reva.api.errors import NotFound
from reva.kernel.errors import ValidationProblem
from reva.v8.entity import bundle as B
from reva.v8.entity import kg_part as K
from reva.v8.entity.resolve import NotResolved, resolve

router = APIRouter(prefix="/v1/v8/entity", tags=["v8-fiche"], dependencies=[Depends(require_scope("read"))])

KIND_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,200}$"
FAMILY_PATTERN = r"^[a-z][a-z_]{1,31}$"
OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(
    None, description="Rows of this origin only; 'any' = both. Default: live in production, any elsewhere"
)
NOTE = (
    "Fiche: every value is shown with its source and licence; missing values are typed (never 0); relation families "
    "are not causal; causal claims and effect estimates are attributed to their sources, not asserted by REVA; "
    "forecasts carry their judged status."
)


# ---------------------------------------------------------------------------------------------- models
class V8FicheLicence(s.ApiModel):
    licence: str | None
    licence_url: str | None = None
    attribution: str | None = None
    restricted: bool = False
    non_commercial: bool = False
    share_alike: bool = False
    required_notice: str | None = None


class V8FicheSource(s.ApiModel):
    source: str = Field(description="Source key (KG load source, atlas table, causal/effect source)")
    label: str = Field(description="Short display name")
    dataset: str | None = None
    statement_key: str | None = None
    series: str | None = Field(None, description="Atlas series key <source>.<dataset>.<indicator>")
    property: str | None = None
    raw_sha256: str | None = None
    selector: dict[str, Any] | None = None
    retrieved_at: str | None = None
    period: str | None = None
    licence: V8FicheLicence | None = None
    verification: str | None = Field(
        None,
        description="community_edited_not_verified | source_reported_not_verified | derived | outside_atlas_snapshot "
        "(read from a published table the current atlas snapshot does not pin yet) | reva_grouping | …",
    )
    origin: str | None = None
    href: str | None = None


class V8FicheRef(s.ApiModel):
    kind: str = Field(description="Fiche kind; the UI links with v8Links.entity(kind, id)")
    id: str
    label: str
    loaded: bool = Field(
        True, description="False: the KG names this record but has not loaded it (no label of its own)"
    )


class V8FicheMissing(s.ApiModel):
    code: str
    reason_fr: str


class V8FichePoint(s.ApiModel):
    t: str = Field(description="Period label (2024, 2024-03, 2024-T2, 2014-03-18)")
    x: float = Field(description="Decimal year, for plotting only")
    v: str | None = Field(description="Exact decimal string; null = gap (never 0)")
    projection: bool = False


class V8FicheLink(s.ApiModel):
    table: str | None = None
    params: dict[str, str] | None = None
    url: str | None = None
    section: str | None = None
    family: str | None = None


class V8FicheStat(s.ApiModel):
    key: str
    label_fr: str
    value: str | None = Field(description="Exact decimal string (null when the stat is text, a link or missing)")
    value_text: str | None
    unit: str | None
    period: str | None
    kind: Literal["observed", "projection", "estimate", "derived", "literal", "count", "relation", "judged"]
    ref: V8FicheRef | None
    source: V8FicheSource | None
    missing: V8FicheMissing | None
    points: list[V8FichePoint]
    sub: str | None
    link: V8FicheLink | None


class V8FicheId(s.ApiModel):
    scheme: str
    value: str
    url: str | None
    source: str | None
    property: str | None
    label_fr: str | None
    ref: V8FicheRef | None


class V8FicheDegree(s.ApiModel):
    statements_out: int
    statements_in: int
    capped_at: int


class V8FicheIdentity(s.ApiModel):
    qid: str | None
    kind: str
    kg_kind: str | None
    kind_label_fr: str
    label: str
    label_fr: str | None
    label_en: str | None
    description: str | None
    definition: str | None
    definition_source: V8FicheSource | None
    aliases: list[str]
    ids: list[V8FicheId]
    anchor: bool = Field(description="True: a REVA grouping page (anchor), not a record of a source")
    anchor_note: str | None
    source: V8FicheSource | None
    sitelinks: int | None
    snapshot_loaded: bool
    community_edited: bool
    degree: V8FicheDegree
    iso3: str | None


class V8FicheOther(s.ApiModel):
    value_text: str
    period: str | None


class V8FicheFact(s.ApiModel):
    property: str
    label_fr: str
    value: str | None
    value_text: str
    unit: str | None
    value_lo: str | None
    value_hi: str | None
    url: str | None
    period: str | None
    count: int
    points: list[V8FichePoint]
    long_text: bool
    source: V8FicheSource
    others: list[V8FicheOther]


class V8FicheRelationItem(s.ApiModel):
    ref: V8FicheRef
    reading_fr: str
    property: str
    property_label: str | None
    period: str | None
    share: str | None
    share_unit: str | None
    identity: bool
    via: V8FicheRef | None
    source: V8FicheSource


class V8FichePropertyCount(s.ApiModel):
    pid: str
    label: str
    count: int


class V8FicheRelationGroup(s.ApiModel):
    family: str
    label_fr: str
    group: str
    count: int
    count_is_lower_bound: bool
    count_approx: bool = Field(
        False, description="Hub mode: count read on the indexed statement table (all recorded versions), approximate"
    )
    items: list[V8FicheRelationItem]
    more: bool
    via: V8FicheRef | None
    properties: list[V8FichePropertyCount]


class V8FicheRelatedForm(s.ApiModel):
    ref: V8FicheRef
    relation_fr: str | None
    relations: list[V8FicheRelationGroup]


class V8FicheConsulted(s.ApiModel):
    series: str
    status: str


class V8FicheSeries(s.ApiModel):
    key: str
    label_fr: str
    theme: str | None
    theme_fr: str | None
    unit: str | None
    value: str | None
    value_text: str | None
    period: str | None
    kind: str
    source: V8FicheSource | None
    points: list[V8FichePoint]
    n_points: int
    first: str | None
    last: str | None
    source_breaks: int
    sources_used: list[str]
    link: V8FicheLink | None
    out_of_snapshot: bool = Field(description="Read from a published table the current atlas snapshot does not pin yet")
    missing: V8FicheMissing | None
    consulted: list[V8FicheConsulted]


class V8FicheTopItem(s.ApiModel):
    iso3: str
    value: str | None
    source: V8FicheSource


class V8FicheTop(s.ApiModel):
    key: str
    label_fr: str
    concept: str
    year: int | None
    n_countries: int | None = None
    unit: str | None
    items: list[V8FicheTopItem]


class V8FichePlace(s.ApiModel):
    ref: V8FicheRef
    level: int | None
    level_fr: str | None
    iso3: str | None
    via: str | None
    basis: str
    lat: float | None
    lon: float | None


class V8FichePoint2(s.ApiModel):
    lat: float
    lon: float
    source_key: str | None


class V8FicheGeometry(s.ApiModel):
    geojson: dict[str, Any]
    feature_key: str
    note_fr: str


class V8FicheSpace(s.ApiModel):
    chain: list[V8FichePlace]
    country_iso3: str | None
    point: V8FichePoint2 | None
    geometry: V8FicheGeometry | None


class V8FicheEvent(s.ApiModel):
    date: str | None
    x: float | None
    label_fr: str
    property: str
    ref: V8FicheRef | None
    value_text: str | None
    source: V8FicheSource


class V8FicheClaim(s.ApiModel):
    claim_key: str
    cause: V8FicheRef
    effect: V8FicheRef
    relation_fr: str | None
    wording_fr: str | None = Field(description="Attributed wording ('Selon …'); REVA asserts no causation")
    stance: str | None
    polarity: str | None
    tier: str | None
    tier_fr: str | None
    grade: str | None
    grade_reasons_fr: list[str]
    mechanism: str | None
    attribution_fr: str | None
    contested: bool
    source: V8FicheSource


class V8FicheClaims(s.ApiModel):
    incoming: list[V8FicheClaim]
    outgoing: list[V8FicheClaim]
    n_in: int
    n_out: int
    note_fr: str


class V8FicheEffect(s.ApiModel):
    estimate_key: str
    role: str
    cause_fr: str
    effect_fr: str
    cause_id: str | None
    effect_id: str | None
    wording_fr: str | None
    measure: str | None
    unit: str | None
    point: str | None
    ci_low: str | None
    ci_high: str | None
    uncertainty_status: str | None
    horizon: dict[str, Any] | None
    method: str | None
    countries: list[str]
    source: V8FicheSource


class V8FicheEffects(s.ApiModel):
    items: list[V8FicheEffect]
    n: int
    n_country_scope: int | None
    note_fr: str


class V8FicheForecastRow(s.ApiModel):
    model: str
    label: str
    kind: Literal["official", "ensemble", "candidate", "baseline"]
    kind_fr: str
    description: str | None
    status: str | None
    point: str | None
    probability: str | None
    lo: str | None
    hi: str | None
    level: str | None
    issued_at: str | None
    batch_sha256: str | None
    score: dict[str, str | None] | None


class V8FicheQuestion(s.ApiModel):
    question_id: str
    family: str | None
    label_fr: str | None
    text_fr: str | None
    geo: str | None
    geo_label_fr: str | None
    reference_period: str | None
    type: str | None
    unit: str | None
    threshold: str | None
    last_observed: dict[str, Any] | None
    status: str
    outcome: str | None
    expected_resolution: str | None
    resolution_source: str | None
    forecasts: list[V8FicheForecastRow]
    official_present: bool
    judged_fr: str


class V8FicheForecasts(s.ApiModel):
    questions: list[V8FicheQuestion]
    n_total: int
    n_pending: int
    n_resolved: int
    families: list[str]
    note_fr: str


class V8FicheMissingItem(s.ApiModel):
    section: str
    what_fr: str
    reason_code: str
    reason_fr: str


class V8FicheSourceSummary(s.ApiModel):
    source: str
    label: str
    n_values: int
    licences: list[str]
    restricted: bool
    non_commercial: bool
    verification: str | None


class V8FicheCanonical(s.ApiModel):
    kind: str
    id: str


class V8FicheMeta(s.ApiModel):
    timings_ms: dict[str, float]
    errors: list[dict[str, str]]
    cached: bool
    served_ms: float | None = None
    generated_at: str | None
    atlas_snapshot: str | None
    atlas_cutoff: str | None
    data_version: list[str] | None
    warming: list[str] = Field(
        default_factory=list, description="Sections still being indexed by this process (first access)"
    )


class V8FicheBundle(s.ApiModel):
    canonical: V8FicheCanonical
    resolved_via: str
    identity: V8FicheIdentity | None
    headline: list[V8FicheStat]
    facts: list[V8FicheFact]
    relations: list[V8FicheRelationGroup]
    related_forms: list[V8FicheRelatedForm]
    series: list[V8FicheSeries]
    tops: list[V8FicheTop]
    space: V8FicheSpace
    time: list[V8FicheEvent]
    claims: V8FicheClaims
    effects: V8FicheEffects
    forecasts: V8FicheForecasts
    missing: list[V8FicheMissingItem]
    sources: list[V8FicheSourceSummary]
    notes: list[str]
    meta: V8FicheMeta


class V8FicheBundleResponse(s.Envelope):
    data: V8FicheBundle


class V8FicheRelationPage(s.ApiModel):
    family: str
    label_fr: str
    items: list[V8FicheRelationItem]


class V8FicheRelationPageResponse(s.Envelope):
    data: V8FicheRelationPage


# ---------------------------------------------------------------------------------------------- helpers
_VALID_LOCK = threading.Lock()
_VALID: OrderedDict[tuple[str, str, str], V8FicheBundle] = OrderedDict()


def _validated(data: dict[str, Any]) -> V8FicheBundle:
    """Validated bundle; a cache hit reuses the model validated when the bundle was built (only meta changes)."""
    key = (data["canonical"]["kind"], data["canonical"]["id"], str(data["meta"]["generated_at"]))
    with _VALID_LOCK:
        hit = _VALID.get(key)
    if hit is not None:
        meta = hit.meta.model_copy(
            update={"cached": data["meta"]["cached"], "served_ms": data["meta"].get("served_ms")}
        )
        return hit.model_copy(update={"meta": meta})
    model = V8FicheBundle.model_validate(data)
    with _VALID_LOCK:
        _VALID[key] = model
        while len(_VALID) > 128:
            _VALID.popitem(last=False)
    return model


def _origin(request: Request, origin: str | None) -> str | None:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _conn_factory(request: Request) -> Any:
    def f() -> AbstractContextManager[Connection]:
        return read_conn(request)

    return f


def _atlas_factory(request: Request, origin: str | None) -> Any:
    def f() -> Any:
        from reva.api.routes.atlas import _query

        return _query(request, origin, None)

    return f


def _store_factory(request: Request) -> Any:
    def f() -> Any:
        from reva.api.routes.atlas import _store

        return _store(request)

    return f


def _not_found(e: NotResolved, kind: str, ident: str) -> NotFound:
    return NotFound(
        f"no fiche for {kind}/{ident}: {e}",
        kind=kind[:64],
        id=ident[:200],
        **{k: str(v)[:400] for k, v in e.detail.items()},
    )


# ---------------------------------------------------------------------------------------------- routes
@router.get(
    "/{kind}/{ident}",
    response_model=V8FicheBundleResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_entity_bundle(
    request: Request,
    kind: str = Path(..., pattern=KIND_PATTERN, description="Fiche kind: country, compound, gene, disease, drug, "
                     "legal_entity (company), facility, armed_conflict, taxon, commodity, question, effect, source, any KG kind"),
    ident: str = Path(..., pattern=ID_PATTERN, description="ISO3 for countries, KG id (Q…, prefix:local), anchor id, "
                      "question id, estimate key or source key"),
    origin: OriginParam | None = ORIGIN_QUERY,
    fresh: bool = Query(False, description="Bypass the process cache (measurement, debugging)"),
) -> V8FicheBundleResponse:  # fmt: skip
    t = Timer()
    org = _origin(request, origin)
    try:
        data = B.build(
            _conn_factory(request), _atlas_factory(request, org), _store_factory(request), kind, ident,
            origin=org, use_cache=not fresh,
        )  # fmt: skip
    except NotResolved as e:
        raise _not_found(e, kind, ident) from None
    warnings: list[s.ApiWarning] = [s.ApiWarning(code="fiche_honesty", message=NOTE)]
    if data["identity"] and data["identity"].get("community_edited"):
        warnings.append(
            s.ApiWarning(
                code="community_edited", message="Wikidata statements are community-edited and not verified by REVA."
            )
        )
    if data["identity"] and data["identity"].get("anchor"):
        warnings.append(
            s.ApiWarning(
                code="anchor_page",
                message="REVA anchor page: a grouping made by REVA; every value keeps its own source.",
            )
        )
    if any(x["restricted"] for x in data["sources"]):
        lic = sorted({lc for x in data["sources"] if x["restricted"] for lc in x["licences"]})
        warnings.append(
            s.ApiWarning(code="restricted_licence", message="Restricted licence(s): " + "; ".join(lic)[:400])
        )
    if data["meta"]["errors"]:
        warnings.append(
            s.ApiWarning(
                code="partial",
                message="Some sections could not be read: " + ", ".join(e["section"] for e in data["meta"]["errors"]),
            )
        )
    rows = len(data["facts"]) + sum(len(g["items"]) for g in data["relations"]) + len(data["series"])
    ident_src = (data["identity"] or {}).get("source") or {}
    env: dict[str, Any] = {
        "context_mode": "evidence",
        "origin": org or ident_src.get("origin") or "none",
        "modality": "reported_observation",
        "warnings": warnings,
        "cost": t.cost(rows, cached=data["meta"]["cached"]),
    }
    return V8FicheBundleResponse(data=_validated(data), **env)


@router.get(
    "/{kind}/{ident}/relations",
    response_model=V8FicheRelationPageResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_entity_relations(
    request: Request,
    kind: str = Path(..., pattern=KIND_PATTERN),
    ident: str = Path(..., pattern=ID_PATTERN),
    family: str = Query(..., pattern=FAMILY_PATTERN, description="Relation family (registry/kg/relations_v4.yaml)"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    origin: OriginParam | None = ORIGIN_QUERY,
) -> V8FicheRelationPageResponse:
    t = Timer()
    org = _origin(request, origin)
    cur = decode_cursor(cursor, "i")
    with read_conn(request) as conn:
        try:
            res = resolve(conn, kind, ident)
        except NotResolved as e:
            raise _not_found(e, kind, ident) from None
        if not res.qid:
            raise ValidationProblem("this fiche has no KG record to page relations of", kind=res.kind, id=res.id)
        try:
            page = K.relation_page(conn, res.qid, family, org, limit=limit, after_id=cur[0] if cur else None)
        except ValueError as e:
            raise ValidationProblem(str(e), family=family) from None
    nxt = encode_cursor([page["next_after_id"]]) if page["next_after_id"] is not None else None
    env: dict[str, Any] = {
        "next_cursor": nxt,
        "context_mode": "evidence",
        "origin": org or "none",
        "modality": "reported_observation",
        "warnings": [s.ApiWarning(code="families_not_causal", message="A relation family is not causal.")],
        "cost": t.cost(len(page["items"])),
    }
    return V8FicheRelationPageResponse(data=V8FicheRelationPage.model_validate(page), **env)


def warm(app: Any) -> str:
    """Start-up warm-up (reva.api.warmup): start the Fiche's forecast-registry index in the background."""
    from sqlalchemy import text

    from reva.api.routes.atlas import _store
    from reva.api.warmup import request_for
    from reva.v8.entity import forecast_part as F

    req = request_for(app)
    with app.state.engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        F.warm(conn, _store(req), _origin(req, None))
    return "fiche forecast index started"
