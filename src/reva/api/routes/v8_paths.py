"""V8 Liens (agent V8LIENS): how anything connects to anything.

* ``GET /v1/v8/paths`` — the k cheapest, diverse paths between two nodes over the KG relation families
  (``registry/kg/relations_v4.yaml`` + ``relations_v4.d/*``; no family is causal) and, with
  ``include_causal=true``, attributed causal claims (tier, grade, source) and published effect estimates (size,
  unit, source) as distinct edge types. Each hop: edge family or claim/estimate, direction, source + licence,
  evidence strength, one sentence. Ranking: cheapest first (well-graded claim chains, then short paths through
  informative families; hubs such as "Earth"/"human" blocked or penalised by degree).
* ``GET /v1/v8/neighbourhood`` (alias ``/v1/v8/paths/neighbourhood``) — the ego graph of one node.
* ``GET /v1/v8/paths/nodes`` — node search for the two pickers (KG entities + effect variables / atlas concepts /
  event types the index knows), with each node's number of indexed links.
* ``GET /v1/v8/paths/status`` — index state.

Honesty: a path is a set of documented links, never a causal proof; causal hops are claims by their sources with
grades; estimates are their authors'. Read-only, ``read`` scope.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, origin_of, origin_warnings, read_conn
from reva.api.errors import ApiProblem, NotFound
from reva.kernel.errors import ValidationProblem
from reva.v8.paths import render as rd
from reva.v8.paths import search as se
from reva.v8.paths.index import EDGE_STATEMENT, PathIndex, split_virtual
from reva.v8.paths.service import HOLDER, IndexBuilding

router = APIRouter(prefix="/v1/v8", tags=["v8-liens"], dependencies=[Depends(require_scope("read"))])

NODE_PATTERN = r"^([A-Z]{3}|Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,160}(@[A-Z]{3})?)$"
FAMILY_PATTERN = re.compile(r"^[a-z][a-z_]{1,31}$")
OriginParam = Literal["live", "test_fixture", "any"]

PATH_WARNING = s.ApiWarning(code="path_not_causal", message=rd.NOTE_EN)
CLAIM_WARNING = s.ApiWarning(
    code="claims_not_causation",
    message="Causal hops are claims made by the named sources, graded A-E (registry/causal/grading.yaml); "
    "estimate hops are estimates published by their authors. REVA does not establish causation.",
)
COMMUNITY_WARNING = s.ApiWarning(
    code="community_edited", message="Wikidata statements are community-edited and are not verified by REVA."
)


# ---------------------------------------------------------------------------------------------- models
class NodeRef(s.ApiModel):
    id: str
    label: str = Field(description="French label, else English; '<id> (libellé non chargé)' when the KG has none")
    label_en: str
    label_loaded: bool
    kind: str | None
    kind_label: str | None
    virtual: bool = Field(description="Effect variable / atlas concept / event type node (not a KG entity)")
    scope_iso3: str | None = Field(description="Virtual node scoped to one country (an effect estimated for it)")
    fiche: dict[str, str] | None = Field(description="{kind, id} for v8Links.entity (country: ISO3)")
    degree: int | None = Field(description="Indexed links of the node (all edge types)")


class HopOut(s.ApiModel):
    from_: str = Field(alias="from")
    to: str
    kind: Literal["statement", "causal_claim", "effect_estimate", "estimate_scope", "declared_bridge"]
    direction: Literal["forward", "inverse"] = Field(
        description="forward: the hop follows the record's subject→object (claims: cause→effect) direction"
    )
    cost: float
    node_penalty: float
    alternatives: int = Field(description="Other allowed edges between the same two nodes")
    family: dict[str, Any] | None = Field(description="Statement hops: V4 family read in path order (never causal)")
    property: dict[str, Any] | None = Field(description="Statement hops: the source's own property")
    relation: dict[str, str] | None = Field(description="Relation label in path order {fr, en}")
    sentence: dict[str, str] = Field(description="'A — relation → B' {fr, en}")
    source: dict[str, Any] | None
    licence: dict[str, Any] | None = Field(description="Licence of the hop's source; null = unknown, never open")
    strength: dict[str, Any] | None = Field(
        description="Evidence strength: documented statement (verification), causal claim (grade, tier), "
        "estimate (method, uncertainty), declared scope / identifier link"
    )
    dated: dict[str, Any] | None
    detail: dict[str, Any] | None
    origin: Literal["live", "test_fixture"]


class PathOut(s.ApiModel):
    rank: int
    cost: float
    length: int
    kind: Literal["documented", "mixed", "causal"]
    nodes: list[str]
    hops: list[HopOut]
    weakest_grade: str | None = Field(description="Weakest grade among causal-claim hops (chain rule)")
    restricted_licences: list[str]
    unknown_licences: int
    variants: dict[str, Any] | None = Field(
        description="Paths identical but for one intermediate node: count (lower bound), position, examples"
    )


class SearchInfo(s.ApiModel):
    max_hops: int
    k: int
    families: list[str] | None
    include_causal: bool
    hub_limit: int
    levels: int
    entries_scanned: int
    nodes_reached: int
    candidates: int
    hubs_not_expanded: list[NodeRef]
    hubs_not_expanded_count: int
    truncated: bool
    truncation_reasons: list[str]
    search_ms: float


class PathsData(s.ApiModel):
    from_: NodeRef = Field(alias="from")
    to: NodeRef
    found: bool
    paths: list[PathOut]
    nodes: dict[str, NodeRef]
    search: SearchInfo
    not_found_reason: str | None
    note: dict[str, str]
    index: dict[str, Any]


class PathsResponse(s.Envelope):
    data: PathsData


class EgoEdge(s.ApiModel):
    hop: HopOut
    group: str


class EgoData(s.ApiModel):
    center: NodeRef
    edges: list[EgoEdge]
    nodes: dict[str, NodeRef]
    groups: dict[str, int] = Field(description="Total neighbours per group (family or edge kind) before the limit")
    note: dict[str, str]


class EgoResponse(s.Envelope):
    data: EgoData


class NodeHit(s.ApiModel):
    node: NodeRef
    match: str
    indexed: bool = Field(description="False: the node has no indexed link (it cannot be a path end)")


class NodesData(s.ApiModel):
    query: str
    hits: list[NodeHit]


class NodesResponse(s.Envelope):
    data: NodesData


class StatusResponse(s.Envelope):
    data: dict[str, Any]


# ---------------------------------------------------------------------------------------------- helpers
def _connect(request: Request) -> Any:
    from reva.adapters.postgres.engine import make_engine

    settings = getattr(request.app.state, "settings", None)

    class _Ctx:
        def __enter__(self) -> Any:
            self.eng = make_engine("api", settings)
            self.conn = self.eng.connect()
            return self.conn

        def __exit__(self, *exc: Any) -> None:
            self.conn.close()
            self.eng.dispose()

    return _Ctx()


def _index(request: Request, conn: Any) -> PathIndex:
    try:
        return HOLDER.get(conn, lambda: _connect(request))
    except IndexBuilding as e:
        raise ApiProblem(503, "paths_index_building", f"{e}; retry in a few seconds") from e


def _origin(request: Request, origin: str | None) -> str | None:
    if origin is None:
        prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
        origin = "live" if prod else "any"
    return None if origin == "any" else origin


def _families(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    out = sorted({f.strip() for x in raw for f in x.split(",") if f.strip()})
    for f in out:
        if not FAMILY_PATTERN.match(f):
            raise ValidationProblem("invalid relation family id", family=f[:64])
    return out or None


def _resolve(idx: PathIndex, conn: Any, ref: str) -> int:
    from reva.kg.query import country_qid_registry, exists

    node = ref
    if re.fullmatch(r"[A-Z]{3}", ref):
        q = country_qid_registry().get(ref)
        if q is None:
            raise NotFound("no reviewed Wikidata QID for this ISO3 code", iso3=ref)
        node = q
    i = idx.idx(node)
    if i is None:
        known = split_virtual(node) is None and exists(conn, node)
        raise NotFound(
            "this entity exists but has no link the path index covers (no family-mapped statement, claim or estimate)"
            if known
            else "node not found in the path index",
            id=node,
        )
    return i


def _envelope(t: Timer, rows: int, hops: list[dict[str, Any]], **detail: Any) -> dict[str, Any]:
    origin = origin_of(h["origin"] for h in hops) if hops else "none"
    warnings = [PATH_WARNING]
    if any(h["kind"] in ("causal_claim", "effect_estimate") for h in hops):
        warnings.append(CLAIM_WARNING)
    if any((h.get("strength") or {}).get("verification") == "community_edited_not_verified" for h in hops):
        warnings.append(COMMUNITY_WARNING)
    restricted = sorted({h["licence"]["licence"] for h in hops if h.get("licence") and h["licence"].get("restricted")})
    if restricted:
        warnings.append(
            s.ApiWarning(code="licence_restricted", message=("Restricted licence(s): " + "; ".join(restricted))[:500])
        )
    warnings += origin_warnings(origin)
    return {
        "context_mode": "evidence",
        "origin": origin,
        "modality": "reported_observation",
        "warnings": warnings,
        "cost": t.cost(rows, **detail),
    }


# ---------------------------------------------------------------------------------------------- routes
@router.get(
    "/paths",
    response_model=PathsResponse,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}, 503: {"model": s.Problem}},
)
def get_paths(
    request: Request,
    from_: str = Query(..., alias="from", pattern=NODE_PATTERN, max_length=200, description="KG id, ISO3 or node id"),
    to: str = Query(..., pattern=NODE_PATTERN, max_length=200),
    families: list[str] | None = Query(
        None, max_length=30, description="Allowed relation families (comma-separated or repeated); default all"
    ),
    max_hops: int = Query(4, ge=1, le=se.MAX_HOPS),
    include_causal: bool = Query(False, description="Also traverse attributed causal claims and effect estimates"),
    k: int = Query(5, ge=1, le=se.MAX_K, description="Number of paths"),
    hub_limit: int = Query(se.DEFAULT_HUB_LIMIT, ge=10, le=se.MAX_HUB_LIMIT),
    origin: OriginParam | None = Query(None),
) -> PathsResponse:
    t = Timer()
    fams = _families(families)
    with read_conn(request) as conn:
        idx = _index(request, conn)
        try:
            flt = se.EdgeFilter(se.family_filter(idx, fams), include_causal, _origin(request, origin))
        except se.PathSearchError as e:
            raise ValidationProblem(str(e), known=idx.families) from e
        src, dst = _resolve(idx, conn, from_), _resolve(idx, conn, to)
        if src == dst:
            raise ValidationProblem("from and to are the same node")
        paths, st = se.find_paths(idx, src, dst, max_hops=max_hops, k=k, flt=flt, hub_limit=hub_limit)
        ids = {idx.node_ids[n] for p in paths for n in p.nodes} | {idx.node_ids[src], idx.node_ids[dst]}
        ids |= {idx.node_ids[n] for p in paths for n in p.variant_examples}
        hubs = list(st.hubs_not_expanded)[:20]
        ids |= {idx.node_ids[n] for n in hubs}
        nodes = rd.node_refs(conn, idx, ids)
        det = rd.HopDetails(conn, idx, [h.edge for p in paths for h in p.hops])
        out = [rd.render_path(idx, p, i + 1, nodes, det, flt) for i, p in enumerate(paths)]
    hops = [h for p in out for h in p["hops"]]
    reason = None
    if not out:
        reason = (
            f"aucun chemin de {max_hops} liens au plus avec ces filtres"
            + (" (recherche bornée : un chemin peut exister au-delà)" if st.truncated else "")
            + (f" ; {len(st.hubs_not_expanded)} nœud(s) très connecté(s) non traversé(s)" if st.hubs_not_expanded else "")
            + ("" if include_causal else " ; essayez d'inclure les affirmations causales attribuées")
        )
    data = PathsData.model_validate(
        {
            "from": nodes[idx.node_ids[src]],
            "to": nodes[idx.node_ids[dst]],
            "found": bool(out),
            "paths": out,
            "nodes": nodes,
            "search": {
                "max_hops": max_hops,
                "k": k,
                "families": fams,
                "include_causal": include_causal,
                "hub_limit": hub_limit,
                "levels": st.levels,
                "entries_scanned": st.entries_scanned,
                "nodes_reached": st.nodes_reached,
                "candidates": st.candidates,
                "hubs_not_expanded": [nodes[idx.node_ids[n]] for n in hubs],
                "hubs_not_expanded_count": len(st.hubs_not_expanded),
                "truncated": st.truncated,
                "truncation_reasons": st.truncation_reasons,
                "search_ms": st.elapsed_ms,
            },
            "not_found_reason": reason,
            "note": {"fr": rd.NOTE_FR, "en": rd.NOTE_EN},
            "index": idx.summary(),
        }
    )
    env = _envelope(t, len(hops), hops, search_ms=st.elapsed_ms, entries_scanned=st.entries_scanned)
    if st.truncated:
        env["warnings"].append(s.ApiWarning(code="truncated", message="; ".join(st.truncation_reasons)[:500]))
    return PathsResponse(data=data, **env)


def _ego(
    request: Request,
    id: str,
    families: list[str] | None,
    limit: int,
    include_causal: bool,
    origin: str | None,
) -> EgoResponse:
    t = Timer()
    fams = _families(families)
    with read_conn(request) as conn:
        idx = _index(request, conn)
        try:
            flt = se.EdgeFilter(se.family_filter(idx, fams), include_causal, _origin(request, origin))
        except se.PathSearchError as e:
            raise ValidationProblem(str(e), known=idx.families) from e
        c = _resolve(idx, conn, id)
        entries, totals = se.neighbourhood(idx, c, flt=flt, limit=limit)
        hops = [se.Hop(c, x.nbr, x.edge, x.rec_fwd, 0.0, 0.0) for x in entries]
        nodes = rd.node_refs(conn, idx, {idx.node_ids[c], *(idx.node_ids[x.nbr] for x in entries)})
        det = rd.HopDetails(conn, idx, [x.edge for x in entries])
        rendered = [rd.render_hop(idx, h, nodes, det, flt) for h in hops]
    data = EgoData.model_validate(
        {
            "center": nodes[idx.node_ids[c]],
            "edges": [{"hop": h, "group": x.group} for h, x in zip(rendered, entries, strict=True)],
            "nodes": nodes,
            "groups": totals,
            "note": {"fr": rd.NOTE_FR, "en": rd.NOTE_EN},
        }
    )
    return EgoResponse(data=data, **_envelope(t, len(rendered), rendered))


@router.get(
    "/neighbourhood",
    response_model=EgoResponse,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}, 503: {"model": s.Problem}},
)
def get_neighbourhood(
    request: Request,
    id: str = Query(..., pattern=NODE_PATTERN, max_length=200),
    families: list[str] | None = Query(None, max_length=30),
    limit: int = Query(30, ge=1, le=200),
    include_causal: bool = Query(True),
    origin: OriginParam | None = Query(None),
) -> EgoResponse:
    return _ego(request, id, families, limit, include_causal, origin)


@router.get(
    "/paths/neighbourhood",
    response_model=EgoResponse,
    response_model_by_alias=True,
    include_in_schema=False,
)
def get_paths_neighbourhood(
    request: Request,
    id: str = Query(..., pattern=NODE_PATTERN, max_length=200),
    families: list[str] | None = Query(None, max_length=30),
    limit: int = Query(30, ge=1, le=200),
    include_causal: bool = Query(True),
    origin: OriginParam | None = Query(None),
) -> EgoResponse:
    return _ego(request, id, families, limit, include_causal, origin)


def _norm(x: str) -> str:
    x = unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", x).strip()


_VIRTUAL_CACHE: dict[str, list[tuple[str, str, str]]] = {}


def _virtual_nodes(conn: Any, idx: PathIndex) -> list[tuple[str, str, str]]:
    key = str(idx.meta.get("data_key")) + str(idx.meta.get("built_at"))
    got = _VIRTUAL_CACHE.get(key)
    if got is None:
        ids = []
        for q in idx.node_ids:
            if split_virtual(q) is None:
                continue
            if q.startswith("atlas:") and "@" not in q and not _is_effect_node(idx, q):
                continue  # a KG measured_by atlas node only: found by the KG search
            ids.append(q)
        refs = rd.node_refs(conn, idx, ids)
        got = [(q, _norm(refs[q]["label"]), _norm(refs[q]["label_en"])) for q in ids]
        _VIRTUAL_CACHE.clear()
        _VIRTUAL_CACHE[key] = got
    return got


def _is_effect_node(idx: PathIndex, q: str) -> bool:
    i = idx.idx(q)
    if i is None:
        return False
    sl = slice(int(idx.indptr[i]), int(idx.indptr[i + 1]))
    return bool((idx.e_kind[idx.adj_edge[sl]] != EDGE_STATEMENT).any())


@router.get("/paths/nodes", response_model=NodesResponse, responses={422: {"model": s.Problem}, 503: {"model": s.Problem}})
def get_nodes(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(12, ge=1, le=50),
) -> NodesResponse:
    """Node search for the pickers: an id / ISO3 typed as is, effect variables / atlas concepts / event types of
    the index (accent-insensitive substring), then KG label search (``reva.kg.query.search``)."""
    from reva.kg.query import country_qid_registry
    from reva.kg.query import search as kg_search

    t = Timer()
    qs = q.strip()
    with read_conn(request) as conn:
        idx = _index(request, conn)
        found: list[tuple[str, str]] = []
        direct = country_qid_registry().get(qs.upper()) if re.fullmatch(r"[A-Za-z]{3}", qs) else None
        if direct:
            found.append((direct, "iso3"))
        if re.fullmatch(NODE_PATTERN, qs) and idx.idx(qs) is not None:
            found.append((qs, "id"))
        nq = _norm(qs)
        if len(nq) >= 2:
            for node, fr, en in _virtual_nodes(conn, idx):
                if nq in fr or nq in en or nq in _norm(node):
                    found.append((node, "virtual"))
        if len(qs) >= 2:
            res = kg_search(conn, qs, langs=("fr", "en"), limit=limit)
            found += [(h["qid"], h["match"]) for h in res["results"]]
        seen: set[str] = set()
        uniq = []
        for node, m in found:
            if node not in seen:
                seen.add(node)
                uniq.append((node, m))
        uniq = uniq[: limit * 2]
        refs = rd.node_refs(conn, idx, [n for n, _ in uniq])
    hits = [
        {"node": refs[n], "match": m, "indexed": idx.idx(n) is not None}
        for n, m in sorted(uniq, key=lambda x: (idx.idx(x[0]) is None, 0 if x[1] in ("iso3", "id") else 1))
    ][:limit]
    return NodesResponse(
        data=NodesData.model_validate({"query": qs, "hits": hits}),
        context_mode="registry",
        origin="none",
        modality="metadata",
        warnings=[],
        cost=t.cost(len(hits)),
    )


@router.get("/paths/status", response_model=StatusResponse)
def get_status(request: Request, warm: bool = Query(False, description="Start loading/building the index")) -> StatusResponse:
    t = Timer()
    if warm:
        with read_conn(request) as conn:
            try:
                HOLDER.get(conn, lambda: _connect(request), wait_s=0.01)
            except IndexBuilding:
                pass
    return StatusResponse(
        data=HOLDER.status(), context_mode="operations", origin="none", modality="metadata", warnings=[], cost=t.cost(0)
    )
