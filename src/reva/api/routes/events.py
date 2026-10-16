"""Event view (V3 causality, agent EVENTS): event cards, event listing and the event taxonomy.

Routes: ``GET /v1/events/{id}`` (event card), ``GET /v1/events/search?type&from&to&country`` (listing; the bare
``GET /v1/events`` path belongs to the V1 world-occurrence listing in ``routes/world.py``), ``GET /v1/events/types``.

Read-only (``read`` scope) over ``kg.*`` via ``reva.events.view``. An event card gives the event's types (with the
``P31``/``P279`` chain or dataset kind that produced each), its time interval with precision, places, participants
with roles and sides, structure (part of / has part, follows / followed by), source-asserted causal statements
(attributed to their source, never REVA's) and evidence-based ``same_as`` links. Every item cites its statement.
Participation, place, time and structure are context, not causation. All results are bounded.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, decode_cursor, encode_cursor, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.events import view as ev
from reva.events.taxonomy import load_taxonomy, taxonomy_summary
from reva.kernel.errors import ValidationProblem
from reva.kg import query as kq

router = APIRouter(prefix="/v1/events", tags=["events"], dependencies=[Depends(require_scope("read"))])

ID_PATTERN = r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
TIME_PATTERN = r"^-?[0-9]{1,7}(-[0-9]{2}(-[0-9]{2})?)?$"
TYPE_PATTERN = r"^[a-z][a-z0-9_]{1,47}$"

CONTEXT_WARNING = s.ApiWarning(code="context_not_causal", message=ev.CONTEXT_NOTE)
COMMUNITY_WARNING = s.ApiWarning(code="community_edited", message=kq.COMMUNITY_NOTE)
DERIVED_WARNING = s.ApiWarning(
    code="derived_event",
    message="This event is derived by REVA from coded dataset periods (a status change), not a source-reported "
    "event record; the derivation and its inputs are given.",
)


# ---------------------------------------------------------------------------------------------- models
class EventRef(s.ApiModel):
    id: str
    label: str = Field(description="English label, else French, else the id")
    label_fr: str | None = None
    kind: str | None = None


class TypeRef(s.ApiModel):
    id: str
    label_en: str
    label_fr: str
    parent: str | None


class RoleRef(s.ApiModel):
    id: str
    label_en: str
    label_fr: str


class Provenance(s.ApiModel):
    statement_key: str
    property: str
    source_key: str
    source_revision: str | None
    raw_sha256: str | None
    rank: Literal["preferred", "normal"]
    origin: Literal["live", "test_fixture"]
    verification: Literal["community_edited_not_verified", "source_reported_not_verified", "derived"]
    derivation: str | None


class TypeMatch(s.OpenModel):
    type: str
    rank: str
    chain: list[dict[str, Any]] = Field(
        description="P279 hops from the P31 class to the taxonomy class (via kg statement or recorded lookup)"
    )
    chain_length: int
    statement: Provenance | None


class Classification(s.ApiModel):
    is_event: bool
    basis: str | None = Field(
        description="wikidata_p31, dataset_kind, wikidata_p31+dataset_kind or derived_status_change"
    )
    primary: list[TypeRef]
    secondary: list[TypeRef]
    rank_rule: str
    matches: list[TypeMatch]
    p31_unmatched: list[str] = Field(description="P31 classes that reach no taxonomy class in recorded data")
    class_search_truncated: bool


class TimeValue(s.OpenModel):
    time: str | None
    precision: int | None
    lo: str | None = Field(description="Inclusive lower bound of the value at its precision (astronomical years)")
    hi: str | None
    provenance: Provenance


class Interval(s.ApiModel):
    start: TimeValue | None
    end: TimeValue | None
    point_in_time: TimeValue | None
    inception: TimeValue | None
    dissolved: TimeValue | None
    episode_end: TimeValue | None
    earliest: str | None = Field(description="Earliest date of the event (ISO 8601, astronomical years)")
    latest: str | None
    basis: str = Field(description="source_dates, derived_from_participation_spells, derived_status_change or undated")
    consistency: Literal["consistent", "inconsistent", "undetermined"]
    alternatives: list[dict[str, Any]] = Field(description="Other stated values (never silently dropped)")
    conflicting_values: bool
    notes: list[str]


class Place(s.ApiModel):
    relation: str
    place: EventRef | None
    value: dict[str, Any] | None
    valid_from: str | None
    valid_to: str | None
    provenance: Provenance


class ParticipantStatement(Provenance):
    point_in_time: dict[str, Any] | None
    coded: dict[str, Any] | None = Field(description="Dataset-coded qualifiers verbatim (side, outcome, ...)")


class Participant(s.ApiModel):
    participant: EventRef | None
    value: dict[str, Any] | None
    role: RoleRef
    side: str | None = Field(description="Side as coded by the source (UCDP a/b, COW 1/2), else null")
    property: str
    direction: Literal["out", "in"]
    role_qualifiers: list[EventRef | None]
    valid_from: str | None
    valid_to: str | None
    statement_count: int
    statements: list[ParticipantStatement]


class StructureItem(s.ApiModel):
    relation: str
    event: EventRef | None
    direction: Literal["out", "in"]
    stated_on: str
    valid_from: str | None
    valid_to: str | None
    provenance: Provenance


class CausalStatement(s.ApiModel):
    subject: EventRef | None
    object: EventRef | None
    other: EventRef | None
    direction: Literal["out", "in"]
    property: str
    property_label_en: str | None
    property_label_fr: str | None
    asserted_by: str
    wording: str
    provenance: Provenance


class SameAs(s.ApiModel):
    event: EventRef | None
    evidence: dict[str, Any] | None
    provenance: Provenance


class Coded(s.ApiModel):
    property: str
    value: dict[str, Any] | None
    object: EventRef | None
    valid_from: str | None
    valid_to: str | None
    qualifiers: dict[str, Any]
    provenance: Provenance


class DatasetKindOut(s.ApiModel):
    id_prefix: str
    source_key: str
    label_en: str
    note: str


class EventCard(s.ApiModel):
    id: str
    label: str
    labels: dict[str, str]
    descriptions: dict[str, str]
    kind: str | None
    dataset_kind: DatasetKindOut | None
    types: Classification
    interval: Interval
    places: list[Place]
    participants: list[Participant]
    structure: list[StructureItem]
    causal_statements: list[CausalStatement]
    same_as: list[SameAs]
    coded: list[Coded]
    sources: list[str]
    entity_provenance: dict[str, Any] | None
    derived: dict[str, Any] | None = None
    truncated: bool
    notes: list[str]
    view_version: str
    taxonomy_sha256: str


class EventCardResponse(s.Envelope):
    data: EventCard


class EventSummary(s.ApiModel):
    id: str
    label: str
    label_fr: str | None = None
    types: list[TypeRef]
    earliest: str | None
    latest: str | None
    basis: str | None


class EventList(s.ApiModel):
    items: list[EventSummary]
    types: list[str] = Field(description="Requested types and their sub-types")
    classes_considered: int = Field(description="Wikidata classes whose recorded P279 chain reaches those types")
    class_closure_truncated: bool
    country: str | None
    involving: bool
    window: dict[str, Any] | None


class EventListResponse(s.Envelope):
    data: EventList


class EventTypeOut(s.ApiModel):
    id: str
    label_en: str
    label_fr: str
    parent: str | None
    depth: int
    wikidata_classes: list[str]
    roles: list[RoleRef]
    description: str
    dataset_kinds: list[str]


class EventTypes(s.ApiModel):
    types: list[EventTypeOut]
    taxonomy_sha256: str


class EventTypesResponse(s.Envelope):
    data: EventTypes


# ---------------------------------------------------------------------------------------------- helpers
OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(None, description="Rows of this origin only; default: live in production, any elsewhere")


def _origin(request: Request, origin: str | None) -> str | None:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _card_origins(card: dict[str, Any]) -> list[str]:
    out = [p["provenance"]["origin"] for p in card["places"]]
    out += [st["origin"] for g in card["participants"] for st in g["statements"]]
    out += [m["statement"]["origin"] for m in card["types"]["matches"] if m.get("statement")]
    if card.get("entity_provenance"):
        out.append(card["entity_provenance"]["origin"])
    return out


def _card_community(card: dict[str, Any]) -> bool:
    return any(src == "wikidata" or src.startswith("wikidata.") for src in card["sources"]) or bool(
        card.get("entity_provenance") and kq.is_community_edited(card["entity_provenance"].get("source_key"))
    )


# ---------------------------------------------------------------------------------------------- routes
@router.get("/types", response_model=EventTypesResponse)
def get_types(request: Request) -> EventTypesResponse:
    t = Timer()
    tax = load_taxonomy()
    items = taxonomy_summary(tax)
    return EventTypesResponse(
        data=EventTypes(types=items, taxonomy_sha256=tax.sha256),
        context_mode="registry",
        origin="none",
        modality="reported_observation",
        cost=t.cost(len(items)),
    )


@router.get("/search", response_model=EventListResponse, responses={422: {"model": s.Problem}})
def list_events(
    request: Request,
    type: list[str] | None = Query(None, max_length=20, description="Event type ids (sub-types included)"),
    from_: str | None = Query(None, alias="from", pattern=TIME_PATTERN, description="YYYY, YYYY-MM or YYYY-MM-DD"),
    to: str | None = Query(None, pattern=TIME_PATTERN),
    country: str | None = Query(
        None,
        pattern=r"^([A-Z]{3}|Q[1-9][0-9]{0,11})$",
        description="ISO 3166-1 alpha-3 or Wikidata Q-id of a place of the event",
    ),
    involving: bool = Query(False, description="Also match events where the country is a participant"),
    include_undated: bool = False,
    limit: int = Query(50, ge=1, le=ev.MAX_LIST),
    cursor: str | None = None,
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EventListResponse:
    t = Timer()
    for ty in type or []:
        if not re.match(TYPE_PATTERN, ty):
            raise ValidationProblem("invalid event type id", type=ty[:48])
    cur = decode_cursor(cursor, "ss") if cursor else None
    cur_t: tuple[str | None, str] | None = None
    if cur is not None:
        cur_t = (cur[0] or None, cur[1])
    with read_conn(request) as conn:
        res = ev.list_events(
            conn,
            types=type,
            start=from_,
            end=to,
            country=country,
            involving=involving,
            include_undated=include_undated,
            limit=limit,
            cursor=cur_t,
            origin=_origin(request, origin),
        )
    nxt = encode_cursor([res["next"][0] or "", res["next"][1]]) if res["next"] else None
    data = {k: v for k, v in res.items() if k not in ("next", "origins")}
    org = origin_of(res["origins"])
    return EventListResponse(
        data=EventList(**data),
        next_cursor=nxt,
        context_mode="evidence",
        origin=org,
        modality="reported_observation",
        warnings=[CONTEXT_WARNING, *origin_warnings(org)],
        cost=t.cost(len(res["items"])),
    )


@router.get(
    "/{event_id}", response_model=EventCardResponse, responses={404: {"model": s.Problem}, 422: {"model": s.Problem}}
)
def get_event(
    request: Request,
    event_id: str = Path(..., pattern=ID_PATTERN, max_length=161, description="Wikidata Q-id or provider key"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EventCardResponse:
    t = Timer()
    with read_conn(request) as conn:
        card = ev.event_card(conn, event_id, origin=_origin(request, origin))
    if card is None:
        raise NotFound("not an event in the knowledge graph (unknown id, or no taxonomy type)", id=event_id)
    org = origin_of(_card_origins(card))
    warnings = [CONTEXT_WARNING] + ([COMMUNITY_WARNING] if _card_community(card) else [])
    warnings += [DERIVED_WARNING] if card.get("derived") else []
    if card["truncated"]:
        warnings.append(s.ApiWarning(code="truncated", message="statement or participant lists were bounded"))
    return EventCardResponse(
        data=EventCard(**card),
        context_mode="evidence",
        origin=org,
        modality="reported_observation",
        warnings=warnings + origin_warnings(org),
        cost=t.cost(1),
    )
