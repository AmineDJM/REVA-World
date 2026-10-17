"""Causal claims (V3, agent CAUSCORE): causes, effects, "why", chains, consistency, candidates.

Read-only (``read`` scope) over ``kg.causal_*`` via ``reva.causal.graph``. REVA asserts no causation: every
edge is a claim attributed to its source ("according to <source>"), with tier, grade A–E and the rule ids
and reasons that produced it (``registry/causal/grading.yaml``), its verbatim mechanism and temporal check. A
chain is REVA's composition of separately asserted claims; chain grade = weakest hop; ≤ 6 hops. Candidates
(text-mined / model-proposed) are only served by ``/v1/causal/candidates``, labelled unsourced. Every result
is bounded (row caps, time budget, k paths) and says when it was cut. ``origin`` as in ``/v1/kg``.
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
from reva.causal import graph as cg
from reva.causal.vocab import RELATIONS

router = APIRouter(prefix="/v1/causal", tags=["causality"], dependencies=[Depends(require_scope("read"))])

ID_PATTERN = r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
Grade = Literal["A", "B", "C", "D", "E"]
OriginParam = Literal["live", "test_fixture", "any"]
ORIGIN_QUERY = Query(
    None,
    description="Rows of this origin only; 'any' = live and test_fixture. Default: live in production, any elsewhere",
)
CLAIMS_WARNING = s.ApiWarning(code="claims_not_causation", message=cg.CLAIM_NOTE)
CHAIN_WARNING = s.ApiWarning(code="chain_is_composition", message=cg.CHAIN_NOTE)
CANDIDATE_WARNING = s.ApiWarning(code="unsourced_candidates", message=cg.CANDIDATE_NOTE)
COMMUNITY_WARNING = s.ApiWarning(
    code="community_edited", message="Wikidata statements are community-edited and are not verified by REVA."
)


# ---------------------------------------------------------------------------------------------- models
class CausalRef(s.ApiModel):
    id: str
    label: str = Field(description="English label, else French, else the id itself")
    label_fr: str | None
    kind: str | None


class Labels(s.ApiModel):
    en: str
    fr: str


class Reason(s.ApiModel):
    rule_id: str
    en: str | None
    fr: str | None


class CausalSource(s.ApiModel):
    source_key: str
    tier: Literal["T1", "T2", "T3", "T4"] = Field(
        description="T1 expert-coded dataset (codebook); T2 scholarly reference (DOI/ISBN verbatim); "
        "T3 Wikidata statement with references; T4 Wikidata statement without references"
    )
    dataset: str | None
    source_revision: str | None
    statement_key: str | None
    statement_id: str | None
    statement_row_id: int | None
    selector: dict[str, Any]
    raw_sha256: str | None
    references: list[dict[str, Any]] = Field(description="The source statement's own references, verbatim")
    citation: dict[str, Any] | None
    codebook: dict[str, Any] | None
    verification: Literal["community_edited_not_verified", "source_reported_not_verified", "derived"]
    retrieved_at: datetime


class CausalGrade(s.ApiModel):
    grade: Grade
    rules_version: str
    rules_sha256: str
    rules_current: bool = Field(description="The grade was computed with the current grading.yaml")
    rule_ids: list[str]
    reasons: list[Reason]
    temporal_check: Literal["consistent", "overlapping", "inconsistent", "undetermined"]
    temporal: dict[str, Any] = Field(description="The cause and effect start windows used (astronomical years)")
    corroboration: int = Field(description="Independent source groups asserting the same link (itself included)")
    corroborating_claim_keys: list[str]
    upstream_groups: list[Any]
    contested_by: list[str]
    graded_at: datetime


class CausalClaim(s.ApiModel):
    claim_key: str
    row_id: int
    cause: CausalRef
    effect: CausalRef
    relation: str
    relation_label: Labels
    relation_inverse_label: Labels
    family: Literal["cause", "prevention"]
    lane: str
    polarity: Literal["promotes", "inhibits"] | None
    stance: Literal["asserts", "denies"]
    asserted_on: Literal["cause", "effect"] | None
    source: CausalSource
    mechanism: str | None = Field(description="Quoted verbatim from the source; never paraphrased by REVA")
    scope: dict[str, Any]
    qualifiers: dict[str, Any]
    contests: list[str]
    disputed_by: list[dict[str, Any]]
    attribution: dict[str, Any] | None = Field(
        None,
        description="Who Wikidata records as making the claim (P3680 statement supported by), the nature qualifier"
        " (P5102) and the source's doubtful certainty codes; the wording names them (grading.yaml v2)",
    )
    grade: CausalGrade | None = Field(description="Null until the grader has run on this claim row")
    review_status: str = Field(description="unreviewed until a maintainer review (agents cannot record reviews)")
    wording: str
    wording_fr: str
    origin: Literal["live", "test_fixture"]
    recorded_at: datetime
    load_id: int


class ContextEdge(s.ApiModel):
    subject: CausalRef
    property: str
    label: dict[str, str]
    object: CausalRef
    source_key: str
    statement_key: str
    origin: Literal["live", "test_fixture"]
    causal: Literal[False]


class CausalEvent(s.ApiModel):
    id: str
    event: CausalRef
    dates: dict[str, Any] = Field(description="start / end windows from KG dates (astronomical years)")
    causes: dict[str, dict[str, int]] = Field(description="family -> grade (or 'ungraded') -> count")
    effects: dict[str, dict[str, int]]
    context: list[ContextEdge]
    context_truncated: bool
    context_note: str
    candidates: int = Field(description="Number of quarantined candidates (served by /v1/causal/candidates only)")
    candidates_note: str
    note: str
    origins: list[str]


class CausalEventResponse(s.Envelope):
    data: CausalEvent


class CausalClaims(s.ApiModel):
    id: str
    claims: list[CausalClaim]
    truncated: bool
    note: str


class CausalClaimsResponse(s.Envelope):
    data: CausalClaims


class Mechanism(s.ApiModel):
    claim_key: str
    mechanism: str
    source_key: str


class WhyLane(s.ApiModel):
    lane: str
    label: dict[str, str]
    claims: list[CausalClaim]


class CausalWhy(s.ApiModel):
    id: str
    label: str
    lanes: list[WhyLane]
    mechanisms: list[Mechanism]
    dissent: list[CausalClaim]
    truncated: bool
    summary: str
    summary_fr: str
    note: str


class CausalWhyResponse(s.Envelope):
    data: CausalWhy


class SameAsLinkOut(s.ApiModel):
    left: str
    right: str
    statement_key: str = Field(description="The events:same_as KG statement")
    rule: str = Field(description="Rule of registry/events/same_as_rules.yaml")
    independent_sources: bool | None = Field(description="False: one record derived from the other (count once)")
    evidence: dict[str, Any] = Field(description="Shared identifier / provider crosswalk evidence, raw sha256")


class ChainHop(s.ApiModel):
    kind: Literal["claim", "identity"] = Field(
        "claim", description="claim: a causal hop asserted by a source; identity: same event in two sources (same_as)"
    )
    from_: CausalRef = Field(alias="from")
    to: CausalRef
    hop_grade: Grade | None = Field(None, description="Best grade among the claims asserting this hop (claim hops)")
    claims: list[CausalClaim] = Field(default_factory=list)
    relation: str | None = Field(None, description="same_as for an identity hop")
    same_as: list[SameAsLinkOut] = Field(default_factory=list, description="Identity evidence of an identity hop")
    note: str | None = None


class ChainPath(s.ApiModel):
    length: int = Field(description="Number of causal (claim) hops; identity hops are not counted")
    identity_hops: int = Field(0, description="same_as identity hops crossed (not causal)")
    cross_source: bool = Field(False, description="The claim hops are asserted by more than one source")
    chain_grade: Grade = Field(description="Weakest hop")
    weakest_hop: int
    hops: list[ChainHop]
    wording: str
    wording_fr: str


class ChainSearch(s.ApiModel):
    max_hops: int
    min_grade: Grade
    levels_expanded: int
    edges_scanned: int
    ungraded_skipped: int
    below_min_grade_skipped: int
    quarantined_skipped: int = Field(
        0, description="Edges not composed: cycle members, self-loops, attributed or nature-qualified claims"
    )
    class_nodes_skipped: int = Field(0, description="Class items (KG P279 / P31-only targets) not traversed")
    class_nodes_sample: list[str] = Field(default_factory=list, max_length=20)
    node_rule: str = ""
    identity: dict[str, Any] = Field(default_factory=dict, description="same_as links loaded for identity hops")
    truncated: bool
    truncation_reasons: list[str]
    time_budget_ms: int
    elapsed_ms: int


class CausalChain(s.ApiModel):
    from_: CausalRef = Field(alias="from")
    to: CausalRef
    found: bool
    paths: list[ChainPath]
    paths_found: int = 0
    note: str
    note_fr: str
    not_found_reason: str | None
    same_as: list[SameAsLinkOut] = Field(default_factory=list, description="When from and to are the same event")
    search: ChainSearch


class CausalChainResponse(s.Envelope):
    data: CausalChain


class ClaimSample(s.ApiModel):
    count: int
    sample: list[CausalClaim]
    truncated: bool = False


class CycleSample(s.ApiModel):
    size: int
    members: list[CausalRef]
    example_cycle: list[CausalRef]


class Cycles(s.ApiModel):
    count: int
    sample: list[CycleSample]
    edges_examined: int
    truncated: bool
    note: str


class CausalConsistency(s.ApiModel):
    self_loops: ClaimSample
    temporal_violations: ClaimSample
    contested: ClaimSample
    cycles: Cycles
    ungraded_claims: int
    elapsed_ms: int
    note: str


class CausalConsistencyResponse(s.Envelope):
    data: CausalConsistency


class CandidateSource(s.ApiModel):
    source_key: str
    source_revision: str
    selector: dict[str, Any]
    raw_sha256: str
    retrieved_at: datetime


class Extractor(s.ApiModel):
    id: str
    version: str


class CausalCandidate(s.ApiModel):
    candidate_key: str
    row_id: int
    cause: CausalRef
    effect: CausalRef
    relation: str
    relation_label: Labels
    family: Literal["cause", "prevention"]
    polarity: Literal["promotes", "inhibits"] | None
    tier: Literal["T5", "T6"]
    extractor: Extractor
    source: CandidateSource
    source_sentence: str = Field(description="The exact source sentence, verbatim")
    mechanism: str | None
    promotion_status: str = Field(description="not_promoted unless a maintainer review says otherwise")
    label: str
    origin: Literal["live", "test_fixture"]
    recorded_at: datetime


class CausalCandidates(s.ApiModel):
    event: str | None
    candidates: list[CausalCandidate]
    note: str


class CausalCandidatesResponse(s.Envelope):
    data: CausalCandidates


# ---------------------------------------------------------------------------------------------- helpers
def _origin(request: Request, origin: str | None) -> str | None:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _claims_of(obj: Any) -> list[dict[str, Any]]:
    """Every claim dict nested in a result (for envelope origin/warnings)."""
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if "claim_key" in obj and "source" in obj:
            out.append(obj)
        else:
            for v in obj.values():
                out += _claims_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _claims_of(v)
    return out


def _env(
    claims: list[dict[str, Any]], t: Timer, *, extra: list[s.ApiWarning] | None = None, **detail: Any
) -> dict[str, Any]:
    origin = origin_of(c["origin"] for c in claims)
    community = any(c["source"].get("verification") == "community_edited_not_verified" for c in claims)
    return {
        "context_mode": "evidence",
        "origin": origin,
        "modality": "reported_observation",
        "warnings": [CLAIMS_WARNING, *(extra or [])]
        + ([COMMUNITY_WARNING] if community else [])
        + origin_warnings(origin),
        "cost": t.cost(len(claims), **detail),
    }


def _known(conn: Any, qid: str) -> None:
    if not cg.known(conn, qid):
        raise NotFound("event not found (no knowledge-graph entity and no causal claim)", id=qid)


IdPath = Path(..., pattern=ID_PATTERN, max_length=161, description="Wikidata Q-id or provider-scoped key")
RelationQuery = Query(None, max_length=len(RELATIONS), description="Restrict to these relations")


# ---------------------------------------------------------------------------------------------- routes
@router.get("/events/{id}", response_model=CausalEventResponse, responses={404: {"model": s.Problem}})
def get_event(request: Request, id: str = IdPath, origin: OriginParam | None = ORIGIN_QUERY) -> CausalEventResponse:
    t = Timer()
    with read_conn(request) as conn:
        ev = cg.event(conn, id, origin=_origin(request, origin))
    if ev is None:
        raise NotFound("event not found (no knowledge-graph entity and no causal claim)", id=id)
    env = _env([], t)
    env["origin"] = origin_of(ev["origins"])
    env["warnings"] = [CLAIMS_WARNING, *origin_warnings(env["origin"])]
    return CausalEventResponse(data=CausalEvent(**ev), **env)


def _side(
    request: Request,
    fn: Any,
    id: str,
    relation: list[str] | None,
    family: str | None,
    min_grade: str | None,
    limit: int,
    origin: str | None,
) -> CausalClaimsResponse:
    t = Timer()
    with read_conn(request) as conn:
        _known(conn, id)
        res = fn(
            conn,
            id,
            origin=_origin(request, origin),
            relation=relation,
            family=family,
            min_grade=min_grade,
            limit=limit,
        )
    env = _env(res["claims"], t)
    if res["truncated"]:
        env["warnings"].append(s.ApiWarning(code="truncated", message=f"limited to {limit} claims (best grades first)"))
    return CausalClaimsResponse(data=CausalClaims(**res), **env)


@router.get(
    "/events/{id}/causes",
    response_model=CausalClaimsResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_causes(
    request: Request,
    id: str = IdPath,
    relation: list[str] | None = RelationQuery,
    family: Literal["cause", "prevention"] | None = None,
    min_grade: Grade | None = None,
    limit: int = Query(100, ge=1, le=cg.MAX_CLAIMS),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CausalClaimsResponse:
    """Claims whose effect is this event (each attributed to its source; best grades first)."""
    return _side(request, cg.causes, id, relation, family, min_grade, limit, origin)


@router.get(
    "/events/{id}/effects",
    response_model=CausalClaimsResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_effects(
    request: Request,
    id: str = IdPath,
    relation: list[str] | None = RelationQuery,
    family: Literal["cause", "prevention"] | None = None,
    min_grade: Grade | None = None,
    limit: int = Query(100, ge=1, le=cg.MAX_CLAIMS),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CausalClaimsResponse:
    """Claims whose cause is this event."""
    return _side(request, cg.effects, id, relation, family, min_grade, limit, origin)


@router.get(
    "/events/{id}/why",
    response_model=CausalWhyResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_why(
    request: Request, id: str = IdPath, min_grade: Grade | None = None, origin: OriginParam | None = ORIGIN_QUERY
) -> CausalWhyResponse:
    """Causes by lane (immediate / underlying / contributing / general / coded / reaction / prevention),
    mechanisms quoted verbatim, and dissent — every claim attributed to its source."""
    t = Timer()
    with read_conn(request) as conn:
        _known(conn, id)
        res = cg.why(conn, id, origin=_origin(request, origin), min_grade=min_grade)
    env = _env(_claims_of(res["lanes"]) + _claims_of(res["dissent"]), t)
    if res["truncated"]:
        env["warnings"].append(s.ApiWarning(code="truncated", message=f"limited to {cg.MAX_CLAIMS} claims"))
    return CausalWhyResponse(data=CausalWhy(**res), **env)


@router.get(
    "/chain",
    response_model=CausalChainResponse,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_chain(
    request: Request,
    from_: str = Query(..., alias="from", pattern=ID_PATTERN, max_length=161, description="The cause end"),
    to: str = Query(..., pattern=ID_PATTERN, max_length=161, description="The effect end"),
    max_hops: int = Query(4, ge=1, le=cg.MAX_HOPS),
    min_grade: Grade = Query("D", description="Only claims graded at least this are traversed"),
    max_paths: int = Query(5, ge=1, le=cg.MAX_PATHS),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CausalChainResponse:
    """Causal chains from ``from`` (cause) to ``to`` (effect): composition by REVA of separately asserted
    claims; chain grade = weakest hop; only causal edges are traversed."""
    t = Timer()
    with read_conn(request) as conn:
        _known(conn, from_)
        _known(conn, to)
        res = cg.chain(
            conn,
            from_,
            to,
            max_hops=max_hops,
            min_grade=min_grade,
            max_paths=max_paths,
            origin=_origin(request, origin),
        )
    env = _env(
        _claims_of(res["paths"]),
        t,
        extra=[CHAIN_WARNING],
        edges_scanned=res["search"]["edges_scanned"],
        levels_expanded=res["search"]["levels_expanded"],
    )
    if res["search"]["truncated"]:
        env["warnings"].append(
            s.ApiWarning(code="truncated", message="; ".join(res["search"]["truncation_reasons"])[:500])
        )
    return CausalChainResponse(data=CausalChain.model_validate(res), **env)


@router.get("/consistency", response_model=CausalConsistencyResponse, responses={422: {"model": s.Problem}})
def get_consistency(
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Sample size per section"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CausalConsistencyResponse:
    """Self-loops, cycles, temporal violations and contested claims among current claims. Reported, never
    resolved or corrected by REVA."""
    t = Timer()
    with read_conn(request) as conn:
        res = cg.consistency(conn, origin=_origin(request, origin), limit=limit)
    env = _env(_claims_of(res), t)
    if res["cycles"]["truncated"]:
        env["warnings"].append(s.ApiWarning(code="truncated", message="cycle detection was bounded"))
    return CausalConsistencyResponse(data=CausalConsistency(**res), **env)


@router.get("/candidates", response_model=CausalCandidatesResponse, responses={422: {"model": s.Problem}})
def get_candidates(
    request: Request,
    event: str | None = Query(None, pattern=ID_PATTERN, max_length=161),
    limit: int = Query(100, ge=1, le=cg.MAX_CLAIMS),
    cursor: str | None = None,
    origin: OriginParam | None = ORIGIN_QUERY,
) -> CausalCandidatesResponse:
    """Quarantined candidates (text-mined T5 / model-proposed T6): non sourcé — candidats. Never claims,
    never graded, never in other answers."""
    t = Timer()
    cur = decode_cursor(cursor, "i")
    with read_conn(request) as conn:
        res = cg.candidates(
            conn, event_id=event, origin=_origin(request, origin), limit=limit, after_id=cur[0] if cur else None
        )
    org: Any = origin_of(c["origin"] for c in res["candidates"])
    nxt = encode_cursor([res["next_after_id"]]) if res["next_after_id"] is not None else None
    return CausalCandidatesResponse(
        data=CausalCandidates(event=res["event"], candidates=res["candidates"], note=res["note"]),
        context_mode="evidence",
        origin=org,
        modality="metadata",
        warnings=[CANDIDATE_WARNING, *origin_warnings(org)],
        cost=t.cost(len(res["candidates"])),
        next_cursor=nxt,
    )
