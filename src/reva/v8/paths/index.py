"""In-memory adjacency index for V8 Liens (agent V8LIENS).

What is indexed (read-only, from the dev/production database):

* **statement edges** — every current ``kg.statement`` row whose property is mapped to a V4 relation family
  (``registry/kg/relations_v4.yaml`` + ``relations_v4.d/*``) and whose object is an entity. No family is causal.
  Properties of ``registry/links/hubs.yaml`` ``excluded_properties`` (P31 instance of, P166 award…) are flagged and
  only traversed when their family is asked for explicitly.
* **causal-claim edges** — current ``kg.causal_claim`` rows asserted (stance ``asserts``) by their source, with
  tier and current grade; relations a chain never composes (``chain_excluded``: coded outcomes…), chain-quarantined
  claims (cycle members, attributed statements) and self-loops are left out, as in ``/v1/causal/chain``.
* **effect-estimate edges** — current ``kg.effect_estimate`` rows: cause → effect, between effect-variable nodes
  (``effvar:<id>``), atlas concepts (``atlas:<concept>``, the same node id as the KG's measured_by objects), event
  types (``evtype:<id>``) or events (KG ids). An effect estimated for exactly one country is the node
  ``<effect>@<ISO3>`` ("consumer prices — France").
* **declared bridges** — estimate scope (``<effect>@<ISO3>`` ↔ the country's QID from the reviewed
  ``registry/entities/country_qids.json``), curated variable → atlas concept links (``registry/effects/
  variables.yaml``), event type → verified Wikidata class (``registry/events/taxonomy.yaml``). Identifier links
  only, never names.

The index is a CSR adjacency (numpy) over interned node ids; one statement is one edge traversable both ways, read
in the traversal direction through its family (``part of`` one way, ``has part`` the other).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

EDGE_STATEMENT = 0
EDGE_CLAIM = 1
EDGE_ESTIMATE = 2
EDGE_SCOPE = 3
EDGE_BRIDGE = 4
EDGE_KINDS = ("statement", "causal_claim", "effect_estimate", "estimate_scope", "declared_bridge")
NO_FAMILY = -1
GRADES = ("A", "B", "C", "D", "E")
UNGRADED = 5

#: Path cost of a causal-claim hop by grade (a statement hop of an informative family costs 1.0): with
#: ``include_causal`` a chain of well-graded claims ranks before a chain of plain statements.
GRADE_COST = {"A": 0.35, "B": 0.5, "C": 0.75, "D": 1.1, "E": 1.6, None: 1.6}
#: Walking a claim or an estimate against its cause → effect direction ("B has cause A") costs more.
AGAINST_CAUSAL_DIRECTION = 0.75
ESTIMATE_COST = 0.6
ESTIMATE_NO_UNCERTAINTY = 0.25
SCOPE_COST = 0.9
BRIDGE_COST = 1.2

#: Index format version (bump when the arrays change; the on-disk cache is keyed by it).
FORMAT = "v8paths-1"


@dataclass(frozen=True)
class StatementRow:
    row_id: int
    subject: str
    pid: str
    object: str
    fixture: bool = False


@dataclass(frozen=True)
class ClaimRow:
    row_id: int
    cause: str
    effect: str
    relation: str
    family: str  # cause | prevention
    grade: str | None
    tier: str
    source_key: str
    fixture: bool = False


@dataclass(frozen=True)
class EstimateRow:
    row_id: int
    estimate_key: str
    cause_kind: str
    cause_id: str
    effect_kind: str
    effect_id: str
    countries: tuple[str, ...]
    uncertainty_status: str
    source_key: str
    fixture: bool = False


@dataclass(frozen=True)
class Bridge:
    """A declared identifier link between a virtual node and a KG node (or two virtual nodes)."""

    a: str
    b: str
    basis: str  # registry file / rule that declares it
    label_fr: str
    label_en: str
    inverse_fr: str
    inverse_en: str
    detail: str | None = None


@dataclass
class Registries:
    """The declarative inputs of the index (families, hubs, effect variables, event types, ISO3 → QID)."""

    family_ids: list[str]
    family_cost: dict[str, float]
    edge_family: dict[str, tuple[str | None, str | None]]  # pid -> (reading out, reading in)
    excluded_properties: frozenset[str]
    blocked: frozenset[str]
    penalised: Mapping[str, float]
    degree_threshold: int
    degree_per_doubling: float
    variable_atlas: Mapping[str, Sequence[str]] = field(default_factory=dict)
    event_type_classes: Mapping[str, Sequence[str]] = field(default_factory=dict)
    iso3_qid: Mapping[str, str] = field(default_factory=dict)
    relations: Sequence[str] = ()
    signature: str = ""


def load_registries() -> Registries:
    """Read every registry the index depends on; ``signature`` changes when any of them changes."""
    from reva.effects.variables import load_variables
    from reva.events.taxonomy import load_taxonomy as load_event_taxonomy
    from reva.kg.families import RELATIONS_DIR, RELATIONS_FILE, load_registry
    from reva.kg.query import COUNTRY_QIDS, country_qid_registry
    from reva.links.taxonomy import HUBS_FILE, load_hubs

    reg = load_registry()
    hubs = load_hubs()
    edge_family: dict[str, tuple[str | None, str | None]] = {}
    for pid in reg.mappings:
        f_out, f_in = reg.edge_family(pid, "out"), reg.edge_family(pid, "in")
        edge_family[pid] = (f_out.family if f_out else None, f_in.family if f_in else None)
    variables = load_variables()
    var_atlas = {v.id: tuple(v.atlas_concepts) for v in variables.items.values() if v.atlas_concepts}
    try:
        etax = load_event_taxonomy()
        ev_classes = {t.id: tuple(t.wikidata_classes) for t in etax.types.values() if t.wikidata_classes}
    except Exception:  # pragma: no cover - a broken taxonomy only removes the event-type bridges
        ev_classes = {}
    from reva.causal.vocab import RELATIONS

    files = [RELATIONS_FILE, *sorted(Path(RELATIONS_DIR).glob("*.yaml")), HUBS_FILE, COUNTRY_QIDS]
    try:
        from reva.effects.variables import VARIABLES_PATH
        from reva.events.taxonomy import TAXONOMY_PATH

        files += [VARIABLES_PATH, TAXONOMY_PATH]
    except ImportError:  # pragma: no cover
        pass
    h = hashlib.sha256(FORMAT.encode())
    for f in files:
        try:
            h.update(Path(f).read_bytes())
        except OSError:
            h.update(b"-")
    return Registries(
        family_ids=list(reg.families),
        family_cost={fid: float(f.path_cost) for fid, f in reg.families.items()},
        edge_family=edge_family,
        excluded_properties=frozenset(hubs.excluded_properties),
        blocked=frozenset(hubs.blocked),
        penalised=dict(hubs.penalised),
        degree_threshold=int(hubs.degree_threshold),
        degree_per_doubling=float(hubs.degree_per_doubling),
        variable_atlas=var_atlas,
        event_type_classes=ev_classes,
        iso3_qid=country_qid_registry(),
        relations=tuple(RELATIONS),
        signature=h.hexdigest()[:16],
    )


# ---------------------------------------------------------------------------------------------- virtual nodes
def estimate_node(kind: str, id_: str, iso3: str | None = None) -> str:
    """Node id of an estimate's cause or effect: ``effvar:<variable>``, ``atlas:<concept>``, ``evtype:<type>`` or the
    event's own KG id; ``@<ISO3>`` names the one country an effect was estimated for."""
    base = {"variable": f"effvar:{id_}", "atlas_concept": f"atlas:{id_}", "event_type": f"evtype:{id_}"}.get(kind, id_)
    return f"{base}@{iso3}" if iso3 else base


def split_virtual(node: str) -> tuple[str, str, str | None] | None:
    """``effvar:prices.consumer_level@FRA`` → (``variable``, ``prices.consumer_level``, ``FRA``); None for KG ids
    (``atlas:<concept>`` without scope is both a KG measured_by node and an effect node: kind ``atlas_concept``)."""
    base, _, iso = node.partition("@")
    iso3 = iso or None
    for prefix, kind in (("effvar:", "variable"), ("evtype:", "event_type"), ("atlas:", "atlas_concept")):
        if base.startswith(prefix):
            return kind, base[len(prefix) :], iso3
    return None


# ---------------------------------------------------------------------------------------------- index
@dataclass
class PathIndex:
    node_ids: list[str]
    node_index: dict[str, int]
    e_src: np.ndarray
    e_dst: np.ndarray
    e_kind: np.ndarray
    e_ref: np.ndarray  # statement row id / claim row id / estimate row id / scope or bridge ordinal
    e_pid: np.ndarray  # statements: index into pids; claims: index into relations; else -1
    e_fixture: np.ndarray
    e_fam_fwd: np.ndarray  # family index of the reading src -> dst (statements), else NO_FAMILY
    e_fam_rev: np.ndarray  # family index of the reading dst -> src
    e_cost_fwd: np.ndarray
    e_cost_rev: np.ndarray
    e_excluded: np.ndarray  # property in hubs.yaml excluded_properties (opt-in by naming its family)
    e_grade: np.ndarray  # claims: 0..4 = A..E, 5 ungraded; else -1
    indptr: np.ndarray
    adj_edge: np.ndarray
    adj_fwd: np.ndarray
    adj_nbr: np.ndarray
    degree: np.ndarray
    penalty: np.ndarray
    blocked: np.ndarray
    pids: list[str]
    families: list[str]
    relations: list[str]
    scopes: list[dict[str, Any]]  # EDGE_SCOPE refs: {node, iso3, qid, estimates: [row ids]}
    bridges: list[Bridge]
    meta: dict[str, Any]

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    def idx(self, node: str) -> int | None:
        return self.node_index.get(node)

    def family_index(self, fam: str) -> int:
        return self.families.index(fam)

    def summary(self) -> dict[str, Any]:
        kinds = np.bincount(self.e_kind.astype(np.int64), minlength=len(EDGE_KINDS)) if len(self.e_kind) else []
        return {
            **self.meta,
            "nodes": self.n_nodes,
            "edges": {EDGE_KINDS[i]: int(kinds[i]) for i in range(len(EDGE_KINDS))} if len(self.e_kind) else {},
        }


def _penalty(deg: np.ndarray, reg: Registries) -> np.ndarray:
    pen = np.zeros(len(deg), dtype=np.float32)
    thr = reg.degree_threshold
    if thr > 0 and reg.degree_per_doubling > 0:
        big = deg > thr
        pen[big] = reg.degree_per_doubling * (np.log2(deg[big].astype(np.float64)) - math.log2(thr))
    return pen


def build_index(
    statements: Iterable[StatementRow],
    claims: Iterable[ClaimRow],
    estimates: Iterable[EstimateRow],
    reg: Registries,
    *,
    meta: Mapping[str, Any] | None = None,
) -> PathIndex:
    t0 = time.perf_counter()
    node_index: dict[str, int] = {}
    node_ids: list[str] = []

    def nid(x: str) -> int:
        i = node_index.get(x)
        if i is None:
            i = len(node_ids)
            node_index[x] = i
            node_ids.append(x)
        return i

    fam_ix = {f: i for i, f in enumerate(reg.family_ids)}
    pids: list[str] = []
    pid_ix: dict[str, int] = {}
    pid_info: list[tuple[int, int, float, float, bool]] = []  # fam_fwd, fam_rev, cost_fwd, cost_rev, excluded
    rel_ix = {r: i for i, r in enumerate(reg.relations)}

    src: list[int] = []
    dst: list[int] = []
    kind: list[int] = []
    ref: list[int] = []
    pidl: list[int] = []
    fix: list[bool] = []
    ffw: list[int] = []
    frv: list[int] = []
    cfw: list[float] = []
    crv: list[float] = []
    exc: list[bool] = []
    grd: list[int] = []

    def add(
        a: str,
        b: str,
        k: int,
        r: int,
        p: int,
        fixture: bool,
        fam: tuple[int, int],
        cost: tuple[float, float],
        excluded: bool = False,
        grade: int = -1,
    ) -> None:
        src.append(nid(a))
        dst.append(nid(b))
        kind.append(k)
        ref.append(r)
        pidl.append(p)
        fix.append(fixture)
        ffw.append(fam[0])
        frv.append(fam[1])
        cfw.append(cost[0])
        crv.append(cost[1])
        exc.append(excluded)
        grd.append(grade)

    n_st = 0
    for s in statements:
        if s.subject == s.object:
            continue
        pi = pid_ix.get(s.pid)
        if pi is None:
            ef = reg.edge_family.get(s.pid)
            if ef is None:
                continue  # not mapped to any family: not indexed
            fo, fi = ef
            pi = len(pids)
            pid_ix[s.pid] = pi
            pids.append(s.pid)
            pid_info.append(
                (
                    fam_ix.get(fo, NO_FAMILY) if fo else NO_FAMILY,
                    fam_ix.get(fi, NO_FAMILY) if fi else NO_FAMILY,
                    reg.family_cost.get(fo or "", 2.0),
                    reg.family_cost.get(fi or "", 2.0),
                    s.pid in reg.excluded_properties,
                )
            )
        fo_i, fi_i, co, ci, ex = pid_info[pi]
        add(s.subject, s.object, EDGE_STATEMENT, s.row_id, pi, s.fixture, (fo_i, fi_i), (co, ci), ex)
        n_st += 1

    n_cl = 0
    for c in claims:
        if c.cause == c.effect:
            continue
        cost = GRADE_COST.get(c.grade, GRADE_COST[None])
        g = GRADES.index(c.grade) if c.grade in GRADES else UNGRADED
        add(
            c.cause,
            c.effect,
            EDGE_CLAIM,
            c.row_id,
            rel_ix.get(c.relation, -1),
            c.fixture,
            (NO_FAMILY, NO_FAMILY),
            (cost, cost + AGAINST_CAUSAL_DIRECTION),
            grade=g,
        )
        n_cl += 1

    scopes: list[dict[str, Any]] = []
    scope_ix: dict[tuple[str, str], int] = {}
    n_es = 0
    for e in estimates:
        countries = [c for c in e.countries if c and c != "WLD"]
        iso = countries[0] if len(countries) == 1 else None
        a = estimate_node(e.cause_kind, e.cause_id)
        b = estimate_node(e.effect_kind, e.effect_id, iso if e.effect_kind in ("variable", "atlas_concept") else None)
        if a == b:
            continue
        cost = ESTIMATE_COST + (ESTIMATE_NO_UNCERTAINTY if e.uncertainty_status == "not_reported" else 0.0)
        add(a, b, EDGE_ESTIMATE, e.row_id, -1, e.fixture, (NO_FAMILY, NO_FAMILY), (cost, cost + AGAINST_CAUSAL_DIRECTION))
        n_es += 1
        if iso and b.endswith("@" + iso):
            qid = reg.iso3_qid.get(iso)
            if qid is None:
                continue  # no reviewed QID for this code: the scope stays on the estimate, no bridge
            key = (b, iso)
            si = scope_ix.get(key)
            if si is None:
                si = len(scopes)
                scope_ix[key] = si
                scopes.append({"node": b, "iso3": iso, "qid": qid, "estimates": []})
                add(b, qid, EDGE_SCOPE, si, -1, e.fixture, (NO_FAMILY, NO_FAMILY), (SCOPE_COST, SCOPE_COST))
            scopes[si]["estimates"].append(e.row_id)

    bridges: list[Bridge] = []
    present = set(node_index)
    for var, concepts in sorted(reg.variable_atlas.items()):
        vnode = estimate_node("variable", var)
        if vnode not in present:
            continue
        for c in concepts:
            bridges.append(
                Bridge(
                    vnode,
                    f"atlas:{c}",
                    "registry/effects/variables.yaml atlas_concepts",
                    "est mesurée par (concept atlas, curation REVA)",
                    "is measured by (atlas concept, REVA curation)",
                    "mesure (variable d'effet, curation REVA)",
                    "measures (effect variable, REVA curation)",
                )
            )
    for et, classes in sorted(reg.event_type_classes.items()):
        tnode = estimate_node("event_type", et)
        if tnode not in present:
            continue
        for q in classes:
            bridges.append(
                Bridge(
                    tnode,
                    q,
                    "registry/events/taxonomy.yaml wikidata_classes (verified)",
                    "correspond à la classe Wikidata",
                    "corresponds to the Wikidata class",
                    "est la classe Wikidata du type d'événement",
                    "is the Wikidata class of the event type",
                )
            )
    for i, br in enumerate(bridges):
        add(br.a, br.b, EDGE_BRIDGE, i, -1, False, (NO_FAMILY, NO_FAMILY), (BRIDGE_COST, BRIDGE_COST))

    n = len(node_ids)
    e_src = np.asarray(src, dtype=np.int32)
    e_dst = np.asarray(dst, dtype=np.int32)
    m = len(e_src)
    owner = np.concatenate([e_src, e_dst])
    order = np.argsort(owner, kind="stable")
    adj_edge = np.concatenate([np.arange(m, dtype=np.int32), np.arange(m, dtype=np.int32)])[order]
    adj_fwd = np.concatenate([np.ones(m, dtype=bool), np.zeros(m, dtype=bool)])[order]
    adj_nbr = np.concatenate([e_dst, e_src])[order]
    counts = np.bincount(owner, minlength=n) if m else np.zeros(n, dtype=np.int64)
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(counts, out=indptr[1:])
    degree = counts.astype(np.int32)
    penalty = _penalty(degree, reg)
    blocked = np.zeros(n, dtype=bool)
    for q, p in reg.penalised.items():
        i = node_index.get(q)
        if i is not None:
            penalty[i] += p
    for q in reg.blocked:
        i = node_index.get(q)
        if i is not None:
            blocked[i] = True
    info = {
        "format": FORMAT,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_seconds": None,
        "registry_signature": reg.signature,
        "statements": n_st,
        "causal_claims": n_cl,
        "effect_estimates": n_es,
        "estimate_scopes": len(scopes),
        "declared_bridges": len(bridges),
        **dict(meta or {}),
    }
    idx = PathIndex(
        node_ids=node_ids,
        node_index=node_index,
        e_src=e_src,
        e_dst=e_dst,
        e_kind=np.asarray(kind, dtype=np.int8),
        e_ref=np.asarray(ref, dtype=np.int64),
        e_pid=np.asarray(pidl, dtype=np.int16),
        e_fixture=np.asarray(fix, dtype=bool),
        e_fam_fwd=np.asarray(ffw, dtype=np.int16),
        e_fam_rev=np.asarray(frv, dtype=np.int16),
        e_cost_fwd=np.asarray(cfw, dtype=np.float32),
        e_cost_rev=np.asarray(crv, dtype=np.float32),
        e_excluded=np.asarray(exc, dtype=bool),
        e_grade=np.asarray(grd, dtype=np.int8),
        indptr=indptr,
        adj_edge=adj_edge,
        adj_fwd=adj_fwd,
        adj_nbr=adj_nbr,
        degree=degree,
        penalty=penalty,
        blocked=blocked,
        pids=pids,
        families=list(reg.family_ids),
        relations=list(reg.relations),
        scopes=scopes,
        bridges=bridges,
        meta=info,
    )
    idx.meta["build_seconds"] = round(time.perf_counter() - t0, 3)
    return idx


# ---------------------------------------------------------------------------------------------- database
STATEMENT_SQL = """SELECT s.id, s.subject_qid, s.object_qid, s.origin, s.statement_key, s.property_pid
                     FROM kg.statement s
                    WHERE s.property_pid = ANY(:p) AND s.object_qid IS NOT NULL AND s.subject_qid <> s.object_qid"""
CLAIM_SQL = """SELECT DISTINCT ON (c.id) c.id, c.cause_id, c.effect_id, c.relation, c.family, c.source_tier,
                      c.source_key, c.origin, g.grade,
                      COALESCE(g.rule_ids && CAST(:qf AS text[]), false) AS quarantined
                 FROM kg.causal_claim_current c
                 LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                WHERE c.stance = 'asserts' AND c.cause_id <> c.effect_id
                  AND c.relation <> ALL(CAST(:nx AS text[]))
                ORDER BY c.id, g.id DESC"""
ESTIMATE_SQL = """SELECT e.id, e.estimate_key, e.cause_kind, e.cause_id, e.effect_kind, e.effect_id,
                         e.scope -> 'countries' AS countries, e.uncertainty_status, e.source_key, e.origin
                    FROM kg.effect_estimate_current e"""
KEY_SQL = """SELECT (SELECT max(id) FROM kg.statement), (SELECT count(*) FROM kg.statement_retraction),
                    (SELECT max(id) FROM kg.causal_claim), (SELECT count(*) FROM kg.causal_claim_retraction),
                    (SELECT max(id) FROM kg.causal_grade), (SELECT max(id) FROM kg.effect_estimate),
                    (SELECT count(*) FROM kg.effect_estimate_retraction)"""


def data_key(conn: Any, reg: Registries) -> str:
    """Cheap fingerprint of the indexed data (max row ids, retraction counts) and of the registries."""
    from sqlalchemy import text

    row = conn.execute(text(KEY_SQL)).one()
    return hashlib.sha256(json.dumps([*map(str, row), reg.signature]).encode()).hexdigest()[:20]


def read_statements(conn: Any, pids: Sequence[str], batch: int = 12) -> list[StatementRow]:
    """Current statements of the mapped properties, read per small group of properties (index scans, each well
    under the statement timeout). Current = the newest row of its (statement_key, origin) and not retracted, as
    ``kg.statement_current`` defines it; computed here because the view's anti-join over 5 M rows is slow."""
    from sqlalchemy import text

    retracted = {int(r) for (r,) in conn.execute(text("SELECT statement_row_id FROM kg.statement_retraction"))}
    out: list[StatementRow] = []
    for i in range(0, len(pids), batch):
        group = list(pids[i : i + batch])
        newest: dict[tuple[str, str], tuple[int, str, str, str]] = {}
        for rid, subj, obj, origin, key, pid in conn.execute(text(STATEMENT_SQL), {"p": group}):
            k = (key, origin)
            cur = newest.get(k)
            if cur is None or rid > cur[0]:
                newest[k] = (int(rid), subj, obj, pid)
        for (_key, origin), (rid, subj, obj, pid) in newest.items():
            if rid in retracted:
                continue
            out.append(StatementRow(rid, subj, pid, obj, origin == "test_fixture"))
    out.sort(key=lambda s: s.row_id)
    return out


def read_claims(conn: Any) -> list[ClaimRow]:
    from sqlalchemy import text

    from reva.causal import grading as gr
    from reva.causal.vocab import vocabulary

    rows = conn.execute(
        text(CLAIM_SQL), {"qf": list(gr.CHAIN_QUARANTINE_FACTS), "nx": sorted(vocabulary().chain_excluded)}
    ).mappings()
    return [
        ClaimRow(
            int(r["id"]),
            r["cause_id"],
            r["effect_id"],
            r["relation"],
            r["family"],
            r["grade"],
            r["source_tier"],
            r["source_key"],
            r["origin"] == "test_fixture",
        )
        for r in rows
        if not r["quarantined"]
    ]


def read_estimates(conn: Any) -> list[EstimateRow]:
    from sqlalchemy import text

    out = []
    for r in conn.execute(text(ESTIMATE_SQL)).mappings():
        countries = r["countries"] if isinstance(r["countries"], list) else []
        out.append(
            EstimateRow(
                int(r["id"]),
                r["estimate_key"],
                r["cause_kind"],
                r["cause_id"],
                r["effect_kind"],
                r["effect_id"],
                tuple(str(c) for c in countries),
                r["uncertainty_status"],
                r["source_key"],
                r["origin"] == "test_fixture",
            )
        )
    return out


def build_from_db(conn: Any, reg: Registries | None = None) -> PathIndex:
    from sqlalchemy import text

    reg = reg or load_registries()
    t0 = time.perf_counter()
    conn.exec_driver_sql("SET LOCAL statement_timeout = '300s'")
    conn.exec_driver_sql("SET LOCAL jit = off")
    key = data_key(conn, reg)
    st = read_statements(conn, sorted(reg.edge_family))
    cl = read_claims(conn)
    es = read_estimates(conn)
    read_s = time.perf_counter() - t0
    _ = text
    idx = build_index(st, cl, es, reg, meta={"data_key": key, "read_seconds": round(read_s, 3)})
    idx.meta["total_seconds"] = round(time.perf_counter() - t0, 3)
    return idx


# ---------------------------------------------------------------------------------------------- disk cache
_ARRAYS = (
    "e_src",
    "e_dst",
    "e_kind",
    "e_ref",
    "e_pid",
    "e_fixture",
    "e_fam_fwd",
    "e_fam_rev",
    "e_cost_fwd",
    "e_cost_rev",
    "e_excluded",
    "e_grade",
    "indptr",
    "adj_edge",
    "adj_fwd",
    "adj_nbr",
    "degree",
    "penalty",
    "blocked",
)


def save(idx: PathIndex, directory: Path) -> Path:
    """Write the index as one ``.npz`` (arrays + a JSON header); older cache files in ``directory`` are removed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"index-{idx.meta.get('data_key', 'nokey')}.npz"
    header = {
        "node_ids": "\n".join(idx.node_ids),
        "pids": idx.pids,
        "families": idx.families,
        "relations": idx.relations,
        "scopes": idx.scopes,
        "bridges": [b.__dict__ for b in idx.bridges],
        "meta": idx.meta,
    }
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, header=np.frombuffer(json.dumps(header).encode(), dtype=np.uint8), **{a: getattr(idx, a) for a in _ARRAYS})
    tmp.replace(path)
    for old in directory.glob("index-*.npz"):
        if old != path:
            old.unlink(missing_ok=True)
    return path


def load(path: Path) -> PathIndex:
    with np.load(path) as z:
        header = json.loads(bytes(z["header"]).decode())
        arrays = {a: z[a] for a in _ARRAYS}
    node_ids = header["node_ids"].split("\n") if header["node_ids"] else []
    return PathIndex(
        node_ids=node_ids,
        node_index={q: i for i, q in enumerate(node_ids)},
        pids=header["pids"],
        families=header["families"],
        relations=header["relations"],
        scopes=header["scopes"],
        bridges=[Bridge(**b) for b in header["bridges"]],
        meta=header["meta"],
        **arrays,
    )
