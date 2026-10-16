"""Executes a validated :class:`~reva.nlqa.plan.QueryPlan` against the adapter ports.

Every record the executor reads becomes an *evidence item* with a stable id (``E1``, ``E2``…)
and full provenance; the composer may only cite these. Nothing is computed that a source does
not report, except:

* **ranking** within one source, one year and one unit (never across sources, years or units —
  a concept whose currency is named per country is never ranked —, never with missing values,
  which are excluded and counted; with no year asked, never over a projection year);
* a bounded **breadth-first path search** over KG neighbours (``nlqa.bfs``) when the LINKS path
  engine is absent — each hop is still a sourced statement.

A backend that is absent or fails gives ``status="unavailable"`` (``capability_unavailable``);
an empty result gives ``status="empty"`` with an *absence* evidence item naming what was
consulted. Neither is ever filled with a guess.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.nlqa.catalog import QaCatalog, fold
from reva.nlqa.plan import PlanOp, QueryPlan
from reva.nlqa.ports import (
    AtlasCell,
    AtlasResult,
    Backends,
    CapabilityUnavailable,
    CountryLink,
    Entity,
    EntityMatch,
    KgPath,
    KgStatement,
    PairLinkGroup,
    PairLinks,
    PathHop,
    is_projection,
    per_country_unit,
)

log = logging.getLogger("reva.nlqa.executor")

MAX_STATEMENTS = 25
MAX_TIMELINE = 30
MAX_PATHS = 3
MAX_SERIES_POINTS = 60
#: ``atlas_value`` with ``country=ALL``: one year only, at most this many cells as evidence (SEC-1).
MAX_ALL_CELLS = 300
#: Wall-clock budget of one question's execution; operations started after it are not run (SEC-1).
TIME_BUDGET_S = 45.0
#: Bounded BFS (fallback path search): neighbour calls and nodes per frontier.
BFS_MAX_CALLS = 60
BFS_MAX_FRONTIER = 150
_MATCH_RANK = {"label_exact": 0, "alias_exact": 1, "label_prefix": 2, "search": 3, "fuzzy": 4}


_WD_QID = re.compile(r"^Q[1-9][0-9]*$")


def wikidata_urls(qid: str, pid: str | None = None, revision: Any = None) -> dict[str, str]:
    """Wikidata page URL (and the revision permalink when the revision id is numeric); empty for
    provider-scoped keys (``ucdp:conflict:333``), which are not Wikidata items."""
    if not _WD_QID.match(str(qid)):
        return {}
    out = {"url": f"https://www.wikidata.org/wiki/{qid}" + (f"#{pid}" if pid and pid.startswith("P") else "")}
    if revision is not None and str(revision).isdigit():
        out["revision_url"] = f"https://www.wikidata.org/w/index.php?title={qid}&oldid={revision}"
    return out


class Evidence:
    """Evidence registry: deduplicated items with ids ``E1``, ``E2``… in insertion order."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._keys: dict[tuple[Any, ...], str] = {}

    def _add(self, key: tuple[Any, ...], item: dict[str, Any]) -> str:
        if key in self._keys:
            return self._keys[key]
        eid = f"E{len(self.items) + 1}"
        self._keys[key] = eid
        self.items.append({"id": eid, **item})
        return eid

    def get(self, eid: str) -> dict[str, Any]:
        return self.items[int(eid[1:]) - 1]

    def statement(self, s: KgStatement) -> str:
        return self._add(
            ("kg", s.statement_id),
            {
                "kind": "kg_statement",
                "statement_id": s.statement_id,
                "subject": {"qid": s.subject_qid, "labels": dict(s.subject_labels)},
                "property": {"id": s.property, "labels": dict(s.property_labels)},
                "object": (
                    {"qid": s.object_qid, "labels": dict(s.object_labels)}
                    if s.object_qid
                    else {"literal": s.literal, "unit": s.literal_unit}
                ),
                "start": s.start.display() if s.start else None,
                "end": s.end.display() if s.end else None,
                "point_in_time": s.point_in_time.display() if s.point_in_time else None,
                "rank": s.rank,
                "causal_assertion": s.causal,
                "qualifiers": dict(s.qualifiers),
                "references": [dict(r) for r in s.references][:10],
                "citation": {
                    "source": s.source_key,
                    "statement_id": s.statement_id,
                    **wikidata_urls(s.subject_qid, s.property, s.source_revision),
                    "revision": s.source_revision,
                    "retrieved_at": s.retrieved_at,
                    "raw_sha256": s.raw_sha256,
                    "origin": s.origin,
                    "status": "community_edited" if s.source_key.startswith("wikidata") else "source_reported",
                },
            },
        )

    def cell(self, c: AtlasCell) -> str:
        return self._add(
            ("atlas", c.source, c.dataset, c.indicator, c.iso3, c.period_start, c.period_end, c.selector),
            {
                "kind": "atlas_cell",
                "concept_id": c.concept_id,
                "iso3": c.iso3,
                "entity_qid": c.entity_qid,
                "period_start": c.period_start,
                "period_end": c.period_end,
                "value": c.value,
                "value_text": c.value_text,
                "unit": c.unit,
                "flags": dict(c.flags),
                "citation": {
                    "source": c.source,
                    "dataset": c.dataset,
                    "indicator": c.indicator,
                    "raw_sha256": c.raw_sha256,
                    "selector": c.selector,
                    **{k: v for k, v in c.provenance.items() if k not in ("raw_sha256", "selector")},
                    "status": "source_reported",
                },
            },
        )

    def entity(self, e: Entity, *, query: str, match: str, alternatives: Sequence[Entity] = ()) -> str:
        return self._add(
            ("entity", e.qid, query),
            {
                "kind": "entity_identification",
                "qid": e.qid,
                "labels": dict(e.labels),
                "descriptions": dict(e.descriptions),
                "entity_kind": e.kind,
                "iso3": e.iso3,
                "query": query,
                "match": match,
                "alternatives": [{"qid": a.qid, "labels": dict(a.labels)} for a in alternatives][:5],
                "citation": {
                    "source": e.source,
                    **wikidata_urls(e.qid, None, e.source_revision),
                    "revision": e.source_revision,
                    "retrieved_at": e.retrieved_at,
                    "status": "community_edited",
                },
            },
        )

    def hop(self, h: PathHop) -> list[str]:
        """Citations of one path hop: its statement, or a derived link and every record it rests on."""
        if h.statement is not None:
            return [self.statement(h.statement)]
        lk = h.link
        assert lk is not None
        cited = [self.statement(s) for s in lk.statements] + [self.cell(c) for c in lk.cells]
        eid = self._add(
            ("derived_hop", lk.link_type, lk.subject_qid, lk.object_qid, lk.period, tuple(cited)),
            {
                "kind": "link",
                "link_kind": lk.link_type,
                "a": lk.subject_qid,
                "b": lk.object_qid,
                "period": lk.period,
                "derived_by": lk.derived_by,
                "supporting_evidence": cited,
                "citation": {"source": lk.derived_by, "status": "derived_link"},
            },
        )
        return [eid, *cited]

    def link(self, link: CountryLink, cited: Sequence[str]) -> str:
        extra: dict[str, Any] = {}
        if link.facet:
            extra["facet"] = link.facet
        if link.link_type == "unga_vote_agreement" and link.basis:
            # a computed index: the counts it rests on travel with it (share = n_identical / n_common)
            extra["basis"] = {
                k: link.basis[k]
                for k in ("index", "formula", "n_common", "n_identical", "issue", "grain", "roll_call_years", "caveat")
                if k in link.basis
            }
        return self._add(
            ("link", link.kind, link.link_type, link.a_iso3, link.b_iso3, link.period, link.facet, tuple(cited)),
            {
                "kind": "link",
                "link_kind": link.kind,
                "link_type": link.link_type,
                "a": link.a_iso3,
                "b": link.b_iso3,
                "period": link.period,
                "weight": link.weight,
                "unit": link.unit,
                "derived_by": link.derived_by,
                "supporting_evidence": list(cited),
                "citation": {"source": link.source or link.derived_by, "status": "derived_link"},
                **extra,
            },
        )

    def absence(self, what: str, consulted: Sequence[str], **details: Any) -> str:
        return self._add(
            ("absence", what, tuple(consulted), tuple(sorted((k, str(v)) for k, v in details.items()))),
            {"kind": "absence", "what": what, "sources_consulted": list(consulted), **details},
        )


@dataclass
class Resolved:
    """Outcome of one ``entity_lookup``."""

    text: str
    kind: str | None
    entity: Entity | None = None
    iso3: str | None = None
    match: str | None = None
    alternatives: list[Entity] = field(default_factory=list)
    evidence_id: str | None = None
    consulted: list[str] = field(default_factory=list)
    status: str = "unresolved"  # resolved | unresolved | unavailable


@dataclass
class OpResult:
    op: PlanOp
    status: str  # ok | empty | unavailable | unresolved | error
    data: dict[str, Any] = field(default_factory=dict)
    consulted: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class Execution:
    plan: QueryPlan
    results: list[OpResult]
    lookups: dict[str, Resolved]
    evidence: Evidence
    country_labels: dict[str, dict[str, str]]
    warnings: list[str] = field(default_factory=list)


def _dec(v: str | None) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


class RankRefused(Exception):
    """A ranking REVA refuses to compute (values not comparable, or only projections without a year)."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class Executor:
    def __init__(self, backends: Backends, catalog: QaCatalog, *, time_budget_s: float | None = TIME_BUDGET_S) -> None:
        self.b = backends
        self.catalog = catalog
        self.time_budget_s = time_budget_s

    # ------------------------------------------------------------------ entry point
    def run(self, plan: QueryPlan) -> Execution:
        ev = Evidence()
        lookups: dict[str, Resolved] = {}
        results: list[OpResult] = []
        warnings: list[str] = []
        deadline = None if self.time_budget_s is None else time.monotonic() + self.time_budget_s
        for op in plan.operations:
            if deadline is not None and time.monotonic() > deadline:
                if op.op == "entity_lookup":
                    lookups[op.id] = Resolved(text=op.args["text"], kind=op.args.get("kind"), status="unavailable")
                    lookups[op.id].evidence_id = ev.absence(
                        "entity", ["time budget exceeded"], query=op.args["text"], kind=op.args.get("kind") or "any"
                    )
                else:
                    results.append(OpResult(op, "error", message="time_budget_exceeded"))
                if "time_budget_exceeded" not in warnings:
                    warnings.append("time_budget_exceeded")
                continue
            if op.op == "entity_lookup":
                lookups[op.id] = self._lookup(op, plan.language, ev, warnings)
                continue
            try:
                results.append(self._dispatch(op, lookups, ev))
            except CapabilityUnavailable as e:
                results.append(
                    OpResult(op, "unavailable", message=e.message, data={"backend": e.details.get("backend")})
                )
            except Exception as e:  # a backend bug must not become an invented answer
                log.warning("nlqa operation %s failed: %s", op.op, type(e).__name__)
                results.append(OpResult(op, "error", message=f"{type(e).__name__}"))
        labels = self._country_labels(results, lookups)
        return Execution(plan, results, lookups, ev, labels, warnings)

    # ------------------------------------------------------------------ entity resolution
    def _lookup(self, op: PlanOp, lang: str, ev: Evidence, warnings: list[str]) -> Resolved:
        text, kind = op.args["text"], op.args.get("kind")
        r = Resolved(text=text, kind=kind)
        iso3 = self._crosswalk_iso3(text)
        if iso3:
            r.iso3 = iso3
        matches: list[EntityMatch] = []
        if self.b.kg is not None:
            r.consulted.append(f"REVA KG ({self.b.kg.name})")
            try:
                matches = self.b.kg.search_entities(text, kind=kind, lang=lang, limit=10)
            except CapabilityUnavailable:
                r.consulted[-1] += " — unavailable"
                matches = []
        if not matches and self.b.search is not None:
            r.consulted.append(self.b.search.name)
            try:
                matches = self.b.search.search(text, lang=lang, limit=5)
            except CapabilityUnavailable:
                r.consulted[-1] += " — unavailable"
                matches = []
        if matches:
            best, alts = _choose(matches, kind)
            r.entity, r.match = best.entity, best.match
            r.alternatives = alts
            r.iso3 = r.iso3 or best.entity.iso3
            r.status = "resolved"
            r.evidence_id = ev.entity(best.entity, query=text, match=best.match, alternatives=alts)
            if alts:
                warnings.append(f"ambiguous_entity: {op.id} has {len(alts)} other candidate(s) with the same match")
        elif r.iso3:
            r.status = "resolved"  # a country by its crosswalk name: atlas operations can run
            r.match = "crosswalk_label"
            r.consulted.append("registry/entities/country_crosswalk.json")
        else:
            if self.b.kg is None and self.b.search is None:
                r.status = "unavailable"
            r.evidence_id = ev.absence(
                "entity", r.consulted or ["no entity index available"], query=text, kind=kind or "any"
            )
        return r

    def _crosswalk_iso3(self, text: str) -> str | None:
        t = fold(text)
        if len(text) == 3 and text.isupper() and text in self.catalog.countries:
            return text
        for c in self.catalog.countries.values():
            if any(fold(n) == t for n in c.names):
                return c.iso3
        return None

    def _entity(self, ref: str, lookups: dict[str, Resolved]) -> Resolved:
        r = lookups.get(ref)
        if r is None:
            raise CapabilityUnavailable("unknown lookup", backend="plan")
        return r

    def _iso3(self, country: str, lookups: dict[str, Resolved]) -> str | None:
        if country in lookups:
            return lookups[country].iso3
        return country

    # ------------------------------------------------------------------ operations
    def _dispatch(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        a = op.args
        if op.op in ("kg_neighbours", "kg_timeline"):
            r = self._entity(a["entity"], lookups)
            if r.entity is None:
                return OpResult(op, "unresolved", data={"lookup": a["entity"]})
            kg = self._need_kg()
            if op.op == "kg_neighbours":
                stmts = kg.neighbours(
                    r.entity.qid, properties=a["properties"] or None, as_of=a["as_of"], limit=MAX_STATEMENTS
                )
            else:
                stmts = kg.timeline(r.entity.qid, from_year=a["from_year"], to_year=a["to_year"], limit=MAX_TIMELINE)
                stmts = sorted((s for s in stmts if s.when() is not None), key=lambda s: s.when().sort_key())  # type: ignore[union-attr]
            stmts = [s for s in stmts if s.rank != "deprecated"]
            consulted = [f"REVA KG ({kg.name})"]
            if not stmts:
                eid = ev.absence(op.op, consulted, qid=r.entity.qid, properties=",".join(a.get("properties") or []))
                return OpResult(op, "empty", data={"absence": eid, "entity": r.entity}, consulted=consulted)
            return OpResult(
                op,
                "ok",
                data={"entity": r.entity, "statements": [(s, ev.statement(s)) for s in stmts]},
                consulted=consulted,
            )
        if op.op == "kg_path":
            ra, rb = self._entity(a["from_entity"], lookups), self._entity(a["to_entity"], lookups)
            if ra.entity is None or rb.entity is None:
                return OpResult(
                    op, "unresolved", data={"lookup": a["from_entity"] if ra.entity is None else a["to_entity"]}
                )
            paths, consulted = self._paths(ra.entity.qid, rb.entity.qid, a["max_hops"], a["as_of"])
            data: dict[str, Any] = {"from": ra.entity, "to": rb.entity, "max_hops": a["max_hops"]}
            if not paths:
                data["absence"] = ev.absence(
                    "path", consulted, from_qid=ra.entity.qid, to_qid=rb.entity.qid, max_hops=a["max_hops"]
                )
                return OpResult(op, "empty", data=data, consulted=consulted)
            data["paths"] = [(p, [(h, ev.hop(h)) for h in p.hops]) for p in paths]
            return OpResult(op, "ok", data=data, consulted=consulted)
        if op.op in ("atlas_value", "atlas_series", "atlas_rank"):
            return self._atlas(op, lookups, ev)
        if op.op == "links":
            iso3 = self._iso3(a["country"], lookups)
            if not iso3:
                return OpResult(op, "unresolved", data={"lookup": a["country"]})
            if self.b.links is None:
                raise CapabilityUnavailable("the interconnection layer (LINKS) is not available", backend="links")
            links = self.b.links.links(iso3, a["kinds"] or None, a["limit"])
            consulted = [self.b.links.name]
            if not links:
                eid = ev.absence("links", consulted, iso3=iso3, kinds=",".join(a["kinds"]))
                return OpResult(op, "empty", data={"absence": eid, "iso3": iso3}, consulted=consulted)
            rows = []
            for lk in links:
                cited = [ev.statement(s) for s in lk.statements] + [ev.cell(c) for c in lk.cells]
                rows.append((lk, ev.link(lk, cited), cited))
            return OpResult(op, "ok", data={"iso3": iso3, "links": rows}, consulted=consulted)
        if op.op == "links_pair":
            return self._links_pair(op, lookups, ev)
        raise CapabilityUnavailable(f"operation {op.op} has no executor", backend="plan")

    def _links_pair(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        a = op.args
        ia, ib = self._iso3(a["country"], lookups), self._iso3(a["other_country"], lookups)
        if not ia or not ib:
            return OpResult(op, "unresolved", data={"lookup": a["country"] if not ia else a["other_country"]})
        if ia == ib:
            return OpResult(op, "unresolved", data={"lookup": a["other_country"]})
        pair = getattr(self.b.links, "pair", None) if self.b.links is not None else None
        if not callable(pair):
            raise CapabilityUnavailable("the pair view of the interconnection layer is not available", backend="links")
        assert self.b.links is not None
        res: PairLinks = pair(ia, ib, a["kinds"] or None, a["from_year"], a["to_year"])
        consulted = [f"{self.b.links.name} (pair view{', ' + res.store if res.store else ''})"]
        data: dict[str, Any] = {"a": ia, "b": ib, "pair": res}
        if res.kinds_without_links:
            data["absent"] = ev.absence(
                "links_pair_kinds", consulted, a=ia, b=ib, kinds=",".join(res.kinds_without_links)
            )
        if not res.groups and not res.predecessor_links:
            data["absence"] = data.get("absent") or ev.absence(
                "links_pair", consulted, a=ia, b=ib, kinds=",".join(a["kinds"])
            )
            return OpResult(op, "empty", data=data, consulted=consulted)

        def cite(lk: CountryLink) -> tuple[CountryLink, str, list[str]]:
            cited = [ev.statement(s) for s in lk.statements] + [ev.cell(c) for c in lk.cells]
            return lk, ev.link(lk, cited), cited

        data["groups"] = [(g, [cite(lk) for lk in pair_links_shown(g)]) for g in res.groups]
        data["predecessors"] = [(p, cite(p.link), [cite(c) for c in p.succession]) for p in res.predecessor_links]
        return OpResult(op, "ok", data=data, consulted=consulted)

    def _need_kg(self) -> Any:
        if self.b.kg is None:
            raise CapabilityUnavailable("the knowledge graph (KG) is not available on this server", backend="kg")
        return self.b.kg

    # ------------------------------------------------------------------ paths
    def _paths(self, a: str, b: str, max_hops: int, as_of: str | None) -> tuple[list[KgPath], list[str]]:
        if self.b.paths is not None:
            paths = self.b.paths.paths(a, b, max_hops=max_hops, as_of=as_of, limit=MAX_PATHS)
            return [p for p in paths if p.hops and len(p.hops) <= max_hops][:MAX_PATHS], [self.b.paths.name]
        kg = self._need_kg()
        return bfs_paths(kg.neighbours, a, b, max_hops=max_hops, as_of=as_of), [
            f"REVA KG ({kg.name}), bounded search nlqa.bfs (≤ {BFS_MAX_CALLS} neighbour queries)"
        ]

    # ------------------------------------------------------------------ atlas
    def _atlas(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        a = op.args
        if self.b.atlas is None:
            raise CapabilityUnavailable("the atlas is not available on this server", backend="atlas")
        atlas = self.b.atlas
        concept = self.catalog.concepts[a["concept"]]
        order = list(concept.source_order)
        data: dict[str, Any] = {"concept": concept}
        if op.op == "atlas_rank":
            res = atlas.values(concept.id, None, a["year"])
            consulted = _consulted(res, order, atlas.name)
            try:
                ranked = rank_cells(
                    res.cells,
                    order,
                    a["year"],
                    a["order"],
                    a["limit"],
                    comparable=concept.comparable_across_countries,
                )
            except RankRefused as r:
                data.update(reason=r.reason, **r.details)
                details: dict[str, Any] = {"concept": concept.id, "year": a["year"] or "latest"}
                details.update(
                    {k: ",".join(map(str, v)) if isinstance(v, list | tuple) else v for k, v in r.details.items()}
                )
                data["absence"] = ev.absence(f"atlas_rank_refused:{r.reason}", consulted, **details)
                return OpResult(op, "refused", data=data, consulted=consulted)
            if ranked is None:
                data["absence"] = ev.absence("atlas_rank", consulted, concept=concept.id, year=a["year"] or "latest")
                return OpResult(op, "empty", data=data, consulted=consulted)
            source, year, rows, n_with_value, n_non_numeric = ranked
            data.update(
                source=source,
                year=year,
                n_with_value=n_with_value,
                n_non_numeric=n_non_numeric,
                rows=[(c, ev.cell(c)) for c in rows],
                other_sources=sorted({_src(c) for c in res.cells} - {source}),
                n_projection=sum(1 for c in rows if is_projection(c.flags)),
                year_asked=a["year"] is not None,
            )
            return OpResult(op, "ok", data=data, consulted=consulted)
        country = a["country"]
        iso3 = self._iso3(country, lookups)
        if not iso3:
            return OpResult(op, "unresolved", data={"lookup": country, **data})
        data["iso3"] = iso3
        if op.op == "atlas_series":
            res = atlas.series(concept.id, iso3, a["from_year"], a["to_year"])
            consulted = _consulted(res, order, atlas.name)
            groups = _by_source(res.cells, order)
            if not groups:
                data["absence"] = ev.absence(
                    "atlas_series",
                    consulted,
                    concept=concept.id,
                    iso3=iso3,
                    from_year=a["from_year"],
                    to_year=a["to_year"],
                )
                return OpResult(op, "empty", data=data, consulted=consulted)
            data["groups"] = [
                (
                    src,
                    [(c, ev.cell(c)) for c in sorted(cells, key=lambda c: c.period_start)[-MAX_SERIES_POINTS:]],
                    len(cells),
                )
                for src, cells in groups
            ]
            return OpResult(op, "ok", data=data, consulted=consulted)
        # atlas_value
        res = atlas.values(concept.id, None if iso3 == "ALL" else [iso3], a["year"])
        consulted = _consulted(res, order, atlas.name)
        groups = _by_source(res.cells, order)
        if iso3 == "ALL":
            if not groups:
                data["absence"] = ev.absence(
                    "atlas_value", consulted, concept=concept.id, iso3="ALL", year=a["year"] or "latest"
                )
                return OpResult(op, "empty", data=data, consulted=consulted)
            src, cells = groups[0]
            all_year: int | None = a["year"] if a["year"] is not None else latest_actual_year(cells)
            if all_year is None:  # only projections and no year asked: the latest year is never a projection
                data["absence"] = ev.absence(
                    "atlas_value", consulted, concept=concept.id, iso3="ALL", year="latest", only_projections=True
                )
                data["only_projections"] = True
                return OpResult(op, "empty", data=data, consulted=consulted)
            chosen = sorted((c for c in cells if c.year == all_year), key=lambda c: c.iso3)
            data.update(
                all_countries=True,
                source=src,
                year=all_year,
                year_asked=a["year"] is not None,
                n=len({c.iso3 for c in chosen}),
                units=sorted({c.unit or "" for c in chosen}),
                n_projection=sum(1 for c in chosen if is_projection(c.flags)),
                truncated=len(chosen) > MAX_ALL_CELLS,
                cells=[(c, ev.cell(c)) for c in chosen[:MAX_ALL_CELLS]],
                other_sources=[s for s, _ in groups[1:]],
            )
            return OpResult(op, "ok", data=data, consulted=consulted)
        if not groups:
            data["absence"] = ev.absence(
                "atlas_value", consulted, concept=concept.id, iso3=iso3, year=a["year"] or "latest"
            )
            if a["year"] is not None:  # say what the latest available value is, labelled as such
                latest = _by_source(atlas.values(concept.id, [iso3], None).cells, order)
                if latest:
                    data["latest_instead"] = [(src, [(c, ev.cell(c)) for c in cells]) for src, cells in latest[:3]]
            return OpResult(op, "empty", data=data, consulted=consulted)
        data["groups"] = [(src, [(c, ev.cell(c)) for c in cells]) for src, cells in groups[:3]]
        data["more_sources"] = max(0, len(groups) - 3)
        return OpResult(op, "ok", data=data, consulted=consulted)

    # ------------------------------------------------------------------ labels
    def _country_labels(self, results: list[OpResult], lookups: dict[str, Resolved]) -> dict[str, dict[str, str]]:
        """Country names for the answer: the resolved KG entity's labels, else KG labels by ISO3 (when the
        adapter offers them), else the crosswalk label."""
        out: dict[str, dict[str, str]] = {}
        for r in lookups.values():
            if r.iso3 and r.entity is not None and r.entity.labels:
                out[r.iso3] = dict(r.entity.labels)
        wanted: set[str] = set()
        for res in results:
            for key in ("rows", "cells"):
                for c, _ in res.data.get(key) or []:
                    wanted.add(c.iso3)
            if res.data.get("iso3") and res.data["iso3"] != "ALL":
                wanted.add(res.data["iso3"])
            if res.op.op == "links_pair":
                wanted.update(x for x in (res.data.get("a"), res.data.get("b")) if x)
            for lk, _, _ in res.data.get("links") or []:
                wanted.update((lk.a_iso3, lk.b_iso3))
        missing = sorted(wanted - set(out))
        fetch = getattr(self.b.kg, "labels_for_iso3", None) if self.b.kg is not None else None
        if missing and callable(fetch):
            try:
                out.update({k: dict(v) for k, v in fetch(missing).items()})
            except CapabilityUnavailable:
                pass
        for iso3 in wanted:
            out.setdefault(iso3, {"en": self.catalog.country_label(iso3)})
        return out


#: ``links_pair``: links of one type kept as evidence for the answer (the earliest and latest per direction and
#: facet, plus every link of an undated or interval type up to this many)
MAX_PAIR_LINKS_PER_TYPE = 12
#: vote agreement: decade links of these issues are kept (all roll calls first; per-year links are left to the UI)
PAIR_VOTE_ISSUES = ("issue=all", "issue=co")


def pair_links_shown(g: PairLinkGroup) -> list[CountryLink]:
    """The links of a pair group the answer cites: a series kind (one value per year) keeps its first and last
    value per direction and facet (the default facet only when present); vote agreement keeps its decade values
    (issues :data:`PAIR_VOTE_ISSUES`); other kinds keep up to :data:`MAX_PAIR_LINKS_PER_TYPE` links."""
    links = list(g.links)
    if g.link_type == "unga_vote_agreement":
        dec = [lk for lk in links if str(lk.basis.get("grain", "decade")) == "decade"]
        return [lk for lk in dec if (lk.facet or "issue=all") in PAIR_VOTE_ISSUES][:24]
    series = [lk for lk in links if lk.weight is not None and _single_year(lk.period)]
    if series and len(series) == len(links):
        facets = {lk.facet for lk in links}
        default = "section=TOTAL" if "section=TOTAL" in facets else None
        if default is not None:
            links = [lk for lk in links if lk.facet == default]
        out: list[CountryLink] = []
        keys = dict.fromkeys((lk.a_iso3, lk.b_iso3, lk.facet) for lk in links)
        for key in list(keys)[:6]:
            same = sorted((lk for lk in links if (lk.a_iso3, lk.b_iso3, lk.facet) == key), key=lambda x: str(x.period))
            out += [same[0]] + ([same[-1]] if len(same) > 1 else [])
        return out
    return links[:MAX_PAIR_LINKS_PER_TYPE]


def _single_year(period: str | None) -> bool:
    return bool(period) and re.fullmatch(r"-?\d{1,4}", str(period)) is not None


def _choose(matches: list[EntityMatch], kind: str | None) -> tuple[EntityMatch, list[Entity]]:
    def key(m: EntityMatch) -> tuple[int, int, int]:
        kind_ok = 0 if (kind is None or m.entity.kind is None or m.entity.kind == kind) else 1
        return (kind_ok, _MATCH_RANK.get(m.match, 9), -int(m.entity.sitelinks or 0))

    ordered = sorted(matches, key=key)
    best = ordered[0]
    alts = [m.entity for m in ordered[1:] if key(m)[:2] == key(best)[:2] and m.entity.qid != best.entity.qid]
    return best, alts[:5]


def _src(c: AtlasCell) -> str:
    """The series key ``<source>.<dataset>.<indicator>`` (the catalog's ``source_order`` unit)."""
    return f"{c.source}.{c.dataset}.{c.indicator}"


def _source_rank(src: str, order: Sequence[str]) -> int:
    for i, o in enumerate(order):
        if src == o:
            return i
    return len(order)


def _by_source(cells: Sequence[AtlasCell], order: Sequence[str]) -> list[tuple[str, list[AtlasCell]]]:
    groups: dict[str, list[AtlasCell]] = {}
    for c in cells:
        groups.setdefault(_src(c), []).append(c)
    return sorted(groups.items(), key=lambda kv: (_source_rank(kv[0], order), kv[0]))


def _consulted(res: AtlasResult, order: Sequence[str], backend: str) -> list[str]:
    names = list(dict.fromkeys([*res.sources_consulted, *order, *(_src(c) for c in res.cells)]))
    return names or [backend]


#: "Latest" year for a ranking: the most recent year whose coverage in the chosen source is at least this
#: share of that source's best-covered year (a ranking over a handful of early reporters is not shown).
#: Only actual (non-projection) cells count: a default year is never a projection year.
RANK_LATEST_COVERAGE = 0.8


def latest_actual_year(cells: Sequence[AtlasCell]) -> int | None:
    """The most recent year whose count of numeric, non-projection cells is at least
    :data:`RANK_LATEST_COVERAGE` of the best-covered year's; ``None`` when there is no actual cell."""
    by_year: dict[int, int] = {}
    for c in cells:
        if c.year is not None and _dec(c.value) is not None and not is_projection(c.flags):
            by_year[c.year] = by_year.get(c.year, 0) + 1
    if not by_year:
        return None
    best = max(by_year.values())
    return max(y for y, n in by_year.items() if n >= RANK_LATEST_COVERAGE * best)


def rank_cells(
    cells: Sequence[AtlasCell],
    order: Sequence[str],
    year: int | None,
    direction: str,
    limit: int,
    *,
    comparable: bool = True,
) -> tuple[str, int, list[AtlasCell], int, int] | None:
    """Rank within ONE series, ONE year and ONE unit: the first series (declared order) that has numeric
    values; the requested year, else that series' latest *actual* year (:func:`latest_actual_year`).
    Missing values are never ranked.

    Raises :class:`RankRefused` when the values are not comparable — the concept's unit names a currency
    per country (``comparable=False``), or the cells of the year carry different units (``units_differ``)
    or a per-country unit such as "current LCU" — and when, with no year asked, the source has only
    projections (``only_projections``)."""
    if not comparable:
        units = sorted({c.unit for c in cells if c.unit and _dec(c.value) is not None})
        raise RankRefused("unit_named_per_country", units=units)
    only_projections = False
    for src, group in _by_source(cells, order):
        if year is not None:
            y: int | None = year
        else:
            y = latest_actual_year(group)
            if y is None:
                only_projections = only_projections or any(
                    _dec(c.value) is not None and is_projection(c.flags) for c in group
                )
                continue
        same_year = [c for c in group if c.year == y]
        numeric = [(c, _dec(c.value)) for c in same_year]
        ok = [(c, d) for c, d in numeric if d is not None]
        if not ok:
            continue
        units = sorted({c.unit or "" for c, _ in ok})
        if len(units) > 1:
            raise RankRefused("units_differ", units=units, series=src, year=y)
        if per_country_unit(units[0]):
            raise RankRefused("unit_named_per_country", units=units, series=src, year=y)
        ok.sort(key=lambda cd: (cd[1], cd[0].iso3), reverse=direction == "desc")
        assert y is not None
        return src, y, [c for c, _ in ok[:limit]], len(ok), len(numeric) - len(ok)
    if only_projections:
        raise RankRefused("only_projections")
    return None


def bfs_paths(
    neighbours: Callable[..., list[KgStatement]], a: str, b: str, *, max_hops: int, as_of: str | None
) -> list[KgPath]:
    """Bounded breadth-first search from ``a`` to ``b`` over sourced statements (both directions).

    Shortest paths first, at most :data:`MAX_PATHS`; at most :data:`BFS_MAX_CALLS` neighbour queries and
    :data:`BFS_MAX_FRONTIER` nodes per level (bounded cost: a miss says "not found within the bound")."""
    calls = 0
    parents: dict[str, list[tuple[str, PathHop]]] = {a: []}
    depth = {a: 0}
    frontier = [a]
    found_at: int | None = None
    for level in range(1, max_hops + 1):
        nxt: list[str] = []
        for node in frontier[:BFS_MAX_FRONTIER]:
            if calls >= BFS_MAX_CALLS:
                break
            calls += 1
            for s in neighbours(node, properties=None, as_of=as_of, limit=200):
                if s.object_qid is None or s.rank == "deprecated":
                    continue
                hop = PathHop(s, forward=s.subject_qid == node)
                other = hop.to_qid
                if other == node:
                    continue
                if other not in depth:
                    depth[other] = level
                    parents[other] = []
                    nxt.append(other)
                if depth[other] == level and len(parents[other]) < MAX_PATHS:
                    parents[other].append((node, hop))
        if b in depth:
            found_at = level
            break
        frontier = nxt
        if not frontier:
            break
    if found_at is None:
        return []
    out: list[KgPath] = []

    def walk(node: str, acc: list[PathHop]) -> None:
        if len(out) >= MAX_PATHS:
            return
        if node == a:
            out.append(KgPath(tuple(reversed(acc)), engine="nlqa.bfs"))
            return
        for parent, hop in parents.get(node, []):
            walk(parent, [*acc, hop])

    walk(b, [])
    return out
