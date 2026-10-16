"""World knowledge graph (V3, agent KGCORE): entities, neighbours, timelines, label search and paths.

Read-only (``read`` scope) over ``kg.*`` via ``reva.kg.query``. Every statement carries its provenance
(source, statement id, source revision, raw artifact sha256, retrieval and recording times, rank, its own
references). Wikidata statements are labelled community-edited and not verified. A path says "connected
via"; causal wording appears only for has cause / has effect statements, attributed to their source.
All results are bounded; limits and truncation are reported.

Origin (review V3, KG-3): ``kg.statement_current`` holds ``live`` and ``test_fixture`` rows side by side.
Every route takes ``origin`` (``live`` | ``test_fixture`` | ``any``); the default is ``live`` when
``REVA_ENV=production`` and ``any`` elsewhere, and a response that serves fixture rows (alone or mixed with
live ones) carries the ``fixture_data`` warning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, decode_cursor, encode_cursor, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.kernel.errors import ValidationProblem
from reva.kg import query as kq

router = APIRouter(prefix="/v1/kg", tags=["knowledge-graph"], dependencies=[Depends(require_scope("read"))])

QID_PATTERN = r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
PID_PATTERN = r"^(P[1-9][0-9]{0,9}|[a-z][a-z0-9_]{1,31}:[a-z][a-z0-9_]{0,63})$"
TIME_PATTERN = r"^-?[0-9]{1,7}(-[0-9]{2}(-[0-9]{2})?)?$"

COMMUNITY_WARNING = s.ApiWarning(code="community_edited", message=kq.COMMUNITY_NOTE)
DERIVED_WARNING = s.ApiWarning(
    code="derived_statements",
    message="Some statements are derived (computed by REVA from sourced inputs, e.g. a spatial overlay), not "
    "reported by a source; their qualifiers give the method, inputs and caveats.",
)
PATH_WARNING = s.ApiWarning(code="path_not_causal", message=kq.PATH_NOTE)


# ---------------------------------------------------------------------------------------------- models
class KgRef(s.ApiModel):
    qid: str
    label: str = Field(description="English label, else French, else the id itself")
    kind: str | None = Field(None, description="Entity kind of the current snapshot (null: no snapshot loaded)")


class KgTimeOut(s.ApiModel):
    time: str = Field(description="Source time string, astronomical year numbering (year 0 = 1 BCE)")
    precision: int = Field(description="Wikibase precision: 9 year, 10 month, 11 day; < 9 coarser")


class KgPropertyOut(s.ApiModel):
    pid: str
    label_en: str | None
    label_fr: str | None
    reva_predicate: str | None
    causal_assertion: bool = Field(description="True only for has cause / has effect (a source's assertion)")


class KgProvenance(s.ApiModel):
    source_key: str
    statement_id: str | None
    statement_key: str
    source_revision: str | None
    raw_sha256: str | None
    retrieved_at: datetime
    recorded_at: datetime
    load_id: int
    origin: Literal["live", "test_fixture"]
    rank: Literal["preferred", "normal"]
    references: list[dict[str, Any]]
    verification: Literal["community_edited_not_verified", "source_reported_not_verified", "derived"] = Field(
        description="community_edited_not_verified: Wikidata; derived: computed from sourced inputs (the "
        "qualifiers declare the derivation, its inputs and caveats), not reported by a source; else "
        "source_reported_not_verified. Nothing is verified by REVA"
    )
    derivation: str | None = Field(None, description="For derived statements: the declared derivation")
    derivation_method: str | None = Field(None, description="For derived statements: the method id and version")


class KgStatementOut(s.ApiModel):
    row_id: int
    subject: KgRef
    property: KgPropertyOut
    object: KgRef | None
    value: dict[str, Any] | None = Field(description="Typed literal (quantities as decimal strings)")
    start: KgTimeOut | None
    end: KgTimeOut | None
    point_in_time: KgTimeOut | None
    time_calendars: dict[str, str] | None
    valid_from: str | None = Field(
        description="Derived inclusive start date used for filtering, ISO 8601 with astronomical years "
        "(-0043-03-15 = 15 March 44 BCE; null: open)"
    )
    valid_to: str | None = Field(
        description="Derived inclusive end date used for filtering, astronomical years (null: open)"
    )
    qualifiers: dict[str, Any]
    provenance: KgProvenance
    direction: Literal["out", "in"] | None = None


class KgEntityProvenance(s.ApiModel):
    source_key: str
    source_revision: str | None
    content_sha256: str
    retrieved_at: datetime
    recorded_at: datetime
    load_id: int
    raw_sha256s: list[str]
    origin: Literal["live", "test_fixture"]
    verification: Literal["community_edited_not_verified", "source_reported_not_verified"]


class KgEntityOut(s.ApiModel):
    qid: str
    snapshot: int | None = Field(description="Snapshot row id; null when the entity is only a statement endpoint")
    kind: str | None
    labels: dict[str, str]
    descriptions: dict[str, str]
    aliases: dict[str, list[str]]
    sitelinks: int | None
    provenance: KgEntityProvenance | None
    statements_out: int
    statements_in: int
    degree_capped_at: int


class KgEntityResponse(s.Envelope):
    data: KgEntityOut


class KgNeighbours(s.ApiModel):
    qid: str
    label: str
    edges: list[KgStatementOut]


class KgNeighboursResponse(s.Envelope):
    data: KgNeighbours


class KgTimelineEvent(KgStatementOut):
    sort_date: str | None = Field(description="Date the event is sorted by, ISO 8601 with astronomical years")
    dated_by: Literal["validity_qualifiers", "time_value"]


class KgTimeline(s.ApiModel):
    qid: str
    label: str
    events: list[KgTimelineEvent]
    truncated: bool


class KgTimelineResponse(s.Envelope):
    data: KgTimeline


class KgSearchMatch(s.ApiModel):
    lang: str
    label: str
    alias: bool


class KgSearchHit(s.ApiModel):
    qid: str
    kind: str | None
    sitelinks: int | None
    origin: Literal["live", "test_fixture"]
    match: Literal["exact", "prefix", "substring", "iso3_registry", "iso3_statement"]
    matched: list[KgSearchMatch]
    labels: dict[str, str]
    rank: Literal["preferred", "normal"] | None = Field(
        None, description="iso3 mode, from KG statements: rank of the ISO alpha-3 statement"
    )


class KgSearch(s.ApiModel):
    query: str
    mode: Literal["prefix", "substring", "iso3"]
    results: list[KgSearchHit]
    truncated: bool = Field(False, description="More entities match than the limit")
    ambiguous: bool = Field(False, description="iso3 mode: several entities hold this code (none is chosen)")
    iso3_source: str | None = Field(
        None, description="iso3 mode: the reviewed registry file, or the KG's ISO alpha-3 (P298) statements"
    )


class KgSearchResponse(s.Envelope):
    data: KgSearch


class KgHop(KgStatementOut):
    from_: KgRef = Field(alias="from")
    to: KgRef
    direction: Literal["forward", "inverse"]  # type: ignore[assignment]
    relation_label_en: str
    relation_label_fr: str | None
    dated: bool
    time_status: Literal["dated", "undated", "within_window"]
    wording: str
    causal_assertion: dict[str, Any] | None = Field(
        description="Set only for has cause / has effect: who asserts the causal relation"
    )


class KgPath(s.ApiModel):
    length: int
    hops: list[KgHop]
    wording: str
    origins: list[str]


class KgHub(s.ApiModel):
    qid: str
    label: str
    degree_at_least: int


class KgPathSearch(s.ApiModel):
    max_hops: int
    hub_limit: int
    allowed_properties: list[str] | None
    excluded_properties: list[str]
    time_window: dict[str, Any] | None
    levels_expanded: int
    nodes_visited: int
    edges_scanned: int
    hubs_skipped: list[KgHub]
    hubs_skipped_count: int
    truncated: bool
    truncation_reasons: list[str]


class KgPathResult(s.ApiModel):
    from_: str = Field(alias="from")
    to: str
    from_label: str
    to_label: str
    found: bool
    paths: list[KgPath]
    note: str
    search: KgPathSearch
    not_found_reason: str | None = None
    community_note: str | None = None


class KgPathResponse(s.Envelope):
    data: KgPathResult


# ---------------------------------------------------------------------------------------------- helpers
OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(
    None,
    description="Rows of this origin only; 'any' = live and test_fixture. Default: live in production, any elsewhere",
)


def _origin(request: Request, origin: str | None) -> str | None:
    """The origin filter for kg.query (None = both origins)."""
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _window(valid_from: str | None, valid_to: str | None, include_undated: bool) -> kq.TimeWindow | None:
    return kq.window(valid_from, valid_to, include_undated=include_undated)


def _envelope_kwargs(
    origins: list[str], community: bool, t: Timer, rows: int, *, derived: bool = False, **detail: Any
) -> dict[str, Any]:
    origin = origin_of(origins)
    return {
        "context_mode": "evidence",
        "origin": origin,
        "modality": "reported_observation",
        "warnings": ([COMMUNITY_WARNING] if community else [])
        + ([DERIVED_WARNING] if derived else [])
        + origin_warnings(origin),
        "cost": t.cost(rows, **detail),
    }


def _community(statements: list[dict[str, Any]]) -> bool:
    return any(x["provenance"]["verification"] == "community_edited_not_verified" for x in statements)


def _derived(statements: list[dict[str, Any]]) -> bool:
    return any(x["provenance"]["verification"] == "derived" for x in statements)


def _known_qid(conn: Any, qid: str) -> None:
    if not kq.exists(conn, qid):
        raise NotFound("entity not found in the knowledge graph", qid=qid)


# ---------------------------------------------------------------------------------------------- routes
QidPath = Path(..., pattern=QID_PATTERN, max_length=161, description="Wikidata Q-id or provider-scoped key")


@router.get("/entities/{qid}", response_model=KgEntityResponse, responses={404: {"model": s.Problem}})
def get_entity(request: Request, qid: str = QidPath, origin: OriginParam | None = ORIGIN_QUERY) -> KgEntityResponse:
    t = Timer()
    with read_conn(request) as conn:
        ent = kq.entity(conn, qid, origin=_origin(request, origin))
    if ent is None:
        raise NotFound("entity not found in the knowledge graph", qid=qid)
    prov = ent["provenance"]
    origins = [prov["origin"]] if prov else []
    community = bool(prov and prov["verification"] == "community_edited_not_verified")
    return KgEntityResponse(data=KgEntityOut(**ent), **_envelope_kwargs(origins, community, t, 1))


@router.get(
    "/entities/{qid}/neighbours",
    response_model=KgNeighboursResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_neighbours(
    request: Request,
    qid: str = QidPath,
    direction: Literal["out", "in", "both"] = "both",
    property: list[str] | None = Query(None, max_length=50, description="Restrict to these property ids"),
    kind: str | None = Query(None, pattern=r"^[a-z][a-z0-9_]{0,63}$", description="Neighbour entity kind"),
    valid_from: str | None = Query(None, pattern=TIME_PATTERN, description="YYYY, YYYY-MM or YYYY-MM-DD"),
    valid_to: str | None = Query(None, pattern=TIME_PATTERN),
    include_undated: bool = True,
    include_literals: bool = Query(False, description="Also return literal-valued outgoing statements"),
    limit: int = Query(100, ge=1, le=kq.MAX_NEIGHBOURS),
    cursor: str | None = None,
    origin: OriginParam | None = ORIGIN_QUERY,
) -> KgNeighboursResponse:
    t = Timer()
    _pids(property)
    cur = decode_cursor(cursor, "i")
    w = _window(valid_from, valid_to, include_undated)
    with read_conn(request) as conn:
        _known_qid(conn, qid)
        res = kq.neighbours(
            conn,
            qid,
            direction=direction,
            properties=property,
            kind=kind,
            time_window=w,
            include_literals=include_literals,
            limit=limit,
            after_id=cur[0] if cur else None,
            origin=_origin(request, origin),
        )
    nxt = encode_cursor([res["next_after_id"]]) if res["next_after_id"] is not None else None
    return KgNeighboursResponse(
        data=KgNeighbours(qid=res["qid"], label=res["label"], edges=res["edges"]),
        next_cursor=nxt,
        **_envelope_kwargs(
            res["origins"], _community(res["edges"]), t, len(res["edges"]), derived=_derived(res["edges"])
        ),
    )


@router.get(
    "/entities/{qid}/timeline",
    response_model=KgTimelineResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_timeline(
    request: Request,
    qid: str = QidPath,
    include_incoming: bool = True,
    valid_from: str | None = Query(None, pattern=TIME_PATTERN),
    valid_to: str | None = Query(None, pattern=TIME_PATTERN),
    limit: int = Query(500, ge=1, le=kq.MAX_TIMELINE),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> KgTimelineResponse:
    t = Timer()
    w = _window(valid_from, valid_to, False)
    with read_conn(request) as conn:
        _known_qid(conn, qid)
        res = kq.timeline(
            conn, qid, include_incoming=include_incoming, time_window=w, limit=limit, origin=_origin(request, origin)
        )
    env = _envelope_kwargs(
        res["origins"], _community(res["events"]), t, len(res["events"]), derived=_derived(res["events"])
    )
    if res["truncated"]:
        env["warnings"].append(s.ApiWarning(code="truncated", message=f"timeline limited to {limit} events"))
    return KgTimelineResponse(
        data=KgTimeline(qid=res["qid"], label=res["label"], events=res["events"], truncated=res["truncated"]), **env
    )


@router.get("/search", response_model=KgSearchResponse, responses={422: {"model": s.Problem}})
def search(
    request: Request,
    q: str | None = Query(None, min_length=1, max_length=200, description="Label or alias text (case-insensitive)"),
    iso3: str | None = Query(
        None,
        pattern=r"^[A-Z]{3}$",
        description="ISO 3166-1 alpha-3 code → entity (registry/entities/country_qids.json, else the KG's P298 "
        "statements; several holders are all returned with ambiguous=true). Exclusive with q",
    ),
    lang: list[str] = Query(["fr", "en"], max_length=10),
    kind: str | None = Query(None, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
    limit: int = Query(20, ge=1, le=kq.MAX_SEARCH),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> KgSearchResponse:
    t = Timer()
    if (q is None) == (iso3 is None):
        raise ValidationProblem("give exactly one of q or iso3")
    org = _origin(request, origin)
    with read_conn(request) as conn:
        if iso3 is not None:
            res = kq.iso3_lookup(conn, iso3, origin=org, limit=limit)
        else:
            res = kq.search(conn, q or "", langs=lang, kind=kind, limit=limit, origin=org)
    found = origin_of(h["origin"] for h in res["results"])
    warnings = origin_warnings(found)
    if res.get("truncated"):
        warnings.append(s.ApiWarning(code="truncated", message=f"more entities match; first {limit} returned"))
    if res.get("ambiguous"):
        warnings.append(
            s.ApiWarning(
                code="ambiguous",
                message=f"{len(res['results'])} entities hold ISO code {iso3}; none is chosen by REVA",
            )
        )
    return KgSearchResponse(
        data=KgSearch(**res),
        context_mode="registry",
        origin=found,
        modality="metadata",
        warnings=warnings,
        cost=t.cost(len(res["results"])),
    )


@router.get(
    "/path",
    response_model=KgPathResponse,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_path(
    request: Request,
    from_: str = Query(..., alias="from", pattern=QID_PATTERN, max_length=161),
    to: str = Query(..., pattern=QID_PATTERN, max_length=161),
    max_hops: int = Query(4, ge=1, le=kq.MAX_HOPS),
    property: list[str] | None = Query(None, max_length=50, description="Allowed hop properties"),
    valid_from: str | None = Query(None, pattern=TIME_PATTERN),
    valid_to: str | None = Query(None, pattern=TIME_PATTERN),
    include_undated: bool = True,
    hub_limit: int = Query(kq.DEFAULT_HUB_LIMIT, ge=1, le=kq.MAX_HUB_LIMIT),
    allow_hub: list[str] | None = Query(None, max_length=20, description="Hub entities that may be traversed"),
    max_paths: int = Query(5, ge=1, le=kq.MAX_PATHS),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> KgPathResponse:
    t = Timer()
    _pids(property)
    if from_ == to:
        raise ValidationProblem("from and to are the same entity")
    w = _window(valid_from, valid_to, include_undated)
    with read_conn(request) as conn:
        _known_qid(conn, from_)
        _known_qid(conn, to)
        res = kq.paths(
            conn,
            from_,
            to,
            max_hops=max_hops,
            allowed_properties=property,
            time_window=w,
            hub_limit=hub_limit,
            allow_hubs=allow_hub or (),
            max_paths=max_paths,
            origin=_origin(request, origin),
        )
    hops = [h for p in res["paths"] for h in p["hops"]]
    env = _envelope_kwargs(
        sorted({h["provenance"]["origin"] for h in hops}),
        _community(hops),
        t,
        len(hops),
        derived=_derived(hops),
        edges_scanned=res["search"]["edges_scanned"],
        levels_expanded=res["search"]["levels_expanded"],
    )
    env["warnings"].insert(0, PATH_WARNING)
    if res["search"]["truncated"]:
        env["warnings"].append(
            s.ApiWarning(code="truncated", message="; ".join(res["search"]["truncation_reasons"])[:500])
        )
    return KgPathResponse(data=KgPathResult.model_validate(res), **env)


def _pids(pids: list[str] | None) -> None:
    import re

    for p in pids or ():
        if not re.match(PID_PATTERN, p):
            raise ValidationProblem("invalid property id", property=p[:64])
