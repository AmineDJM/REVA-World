"""Real adapters behind :mod:`reva.nlqa.ports`, wired lazily (absent backend → ``None``).

* :class:`KgDbAdapter` / :class:`KgPathAdapter` — KGCORE's read API :mod:`reva.kg.query`
  (``search``, ``neighbours``, ``timeline``, ``paths`` over ``kg.statement_current``), through
  the API's read-only engine (``reva_api`` has SELECT on schema ``kg``).
* :class:`AtlasQueryAdapter` — ATLASCORE's :class:`reva.atlas.query.AtlasQuery` (DuckDB over the
  latest recorded atlas snapshot) and :mod:`reva.atlas.catalog`.
* :class:`LinksAdapter` — the LINKS layer's :class:`reva.links.model.Link` records, through a
  ``links_for(node, kinds, limit)`` callable when LINKS exposes one.
* :class:`WikidataSearchAdapter` — optional live ``wbsearchentities`` fallback for entity
  identification (labelled as such), through an injected budget-metered GET only; **not wired** until
  WDHARVEST's metered client offers it, and
  bounded per process; it identifies, it never supplies statements.

Every adapter converts backend failures into :class:`~reva.nlqa.ports.CapabilityUnavailable`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from reva.kernel.errors import RevaError
from reva.nlqa.ports import (
    AtlasCell,
    AtlasResult,
    Backends,
    CapabilityUnavailable,
    Concept,
    CountryLink,
    DerivedLinkHop,
    Entity,
    EntityMatch,
    KgPath,
    KgStatement,
    PairLinkGroup,
    PairLinks,
    PathHop,
    PredecessorLink,
    TimeValue,
    is_projection,
    pick_label,
)

log = logging.getLogger("reva.nlqa.adapters")


# --------------------------------------------------------------------------- KG
def _time(t: Mapping[str, Any] | None) -> TimeValue | None:
    if not t or not t.get("time"):
        return None
    return TimeValue(str(t["time"]), int(t.get("precision") or 11))


def literal_text(value: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """A KG typed literal as display text (+ unit): quantities keep their decimal string."""
    if not value:
        return None, None
    t = value.get("type")
    if t == "quantity":
        unit = value.get("unit")
        unit = None if unit in (None, "1", "http://www.wikidata.org/entity/Q199") else str(unit).rsplit("/", 1)[-1]
        return str(value.get("amount", "")).lstrip("+"), unit
    if t == "time":
        tv = _time(value)
        return (tv.display() if tv else None), None
    if t == "monolingualtext":
        return str(value.get("text", "")), None
    for k in ("value", "text", "id", "string"):
        if isinstance(value.get(k), str):
            return value[k], None
    return json.dumps({k: v for k, v in value.items() if k != "type"}, ensure_ascii=False)[:200], None


def statement_from_kg(e: Mapping[str, Any], labels: Mapping[str, Mapping[str, str]] | None = None) -> KgStatement:
    """Convert a statement dict of :mod:`reva.kg.query` (``_statement_out``) into a port record."""
    prov = e.get("provenance") or {}
    prop = e.get("property") or {}
    subj = e.get("subject") or {}
    obj = e.get("object")
    labels = labels or {}

    def lab(node: Mapping[str, Any] | None) -> dict[str, str]:
        if not node:
            return {}
        out = dict(labels.get(node.get("qid", ""), {}))
        if node.get("label") and node.get("label") != node.get("qid"):
            out.setdefault("en", str(node["label"]))
        return out

    plabels = {k: str(prop[f"label_{k}"]) for k in ("en", "fr") if prop.get(f"label_{k}")}
    lit, unit = literal_text(e.get("value")) if obj is None else (None, None)
    return KgStatement(
        statement_id=str(prov.get("statement_id") or prov.get("statement_key") or e.get("row_id")),
        subject_qid=str(subj.get("qid")),
        property=str(prop.get("pid")),
        subject_labels=lab(subj),
        property_labels=plabels,
        object_qid=str(obj["qid"]) if obj else None,
        object_labels=lab(obj),
        literal=lit,
        literal_unit=unit,
        start=_time(e.get("start")),
        end=_time(e.get("end")),
        point_in_time=_time(e.get("point_in_time")),
        rank=str(prov.get("rank") or "normal"),
        qualifiers=e.get("qualifiers") or {},
        references=list(prov.get("references") or [])[:10],
        source_key=str(prov.get("source_key") or "wikidata"),
        source_revision=str(prov["source_revision"]) if prov.get("source_revision") is not None else None,
        retrieved_at=str(prov["retrieved_at"]) if prov.get("retrieved_at") is not None else None,
        raw_sha256=prov.get("raw_sha256"),
        origin=prov.get("origin"),
    )


_MATCH = {
    ("exact", False): "label_exact",
    ("exact", True): "alias_exact",
    ("prefix", False): "label_prefix",
    ("prefix", True): "label_prefix",
}


class KgDbAdapter:
    """:class:`~reva.nlqa.ports.KgPort` on :mod:`reva.kg.query` (read-only connection per call)."""

    name = "reva.kg.query"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._ok: bool | None = None

    @contextmanager
    def _conn(self) -> Any:
        try:
            with self.engine.connect() as conn:
                if self._ok is None:
                    self._ok = bool(
                        conn.execute(text("SELECT to_regclass('kg.statement_current') IS NOT NULL")).scalar_one()
                    )
                if not self._ok:
                    raise CapabilityUnavailable("the KG tables (migration 0009_kg) are absent", backend="kg")
                yield conn
        except SQLAlchemyError as e:
            raise CapabilityUnavailable(f"KG query failed: {type(e).__name__}", backend="kg") from None

    def _kq(self) -> Any:
        try:
            from reva.kg import query as kq
        except ImportError:
            raise CapabilityUnavailable("reva.kg.query is not installed", backend="kg") from None
        return kq

    def search_entities(self, text_: str, *, kind: str | None, lang: str, limit: int) -> list[EntityMatch]:
        kq = self._kq()
        langs = (lang, "en", "fr") if lang not in ("en", "fr") else ((lang, "fr") if lang == "en" else (lang, "en"))
        try:
            with self._conn() as conn:
                res = kq.search(conn, text_, langs=tuple(dict.fromkeys(langs)), kind=None, limit=limit)
                hits = res.get("results") or []
                iso = self._iso3_of(conn, [h["qid"] for h in hits])
        except RevaError as e:
            if isinstance(e, CapabilityUnavailable):
                raise
            return []  # invalid search text for the KG (length): no match
        out = []
        for h in hits:
            matched = h.get("matched") or []
            alias_only = bool(matched) and all(m.get("alias") for m in matched)
            match = _MATCH.get((h.get("match"), alias_only), "fuzzy")
            out.append(
                EntityMatch(
                    Entity(
                        qid=h["qid"],
                        labels=h.get("labels") or {},
                        kind=h.get("kind"),
                        sitelinks=int(h.get("sitelinks") or 0),
                        iso3=iso.get(h["qid"]),
                        source="kg",
                    ),
                    match,
                    str(matched[0].get("label")) if matched else "",
                )
            )
        return out

    @staticmethod
    def _iso3_of(conn: Any, qids: Sequence[str]) -> dict[str, str]:
        if not qids:
            return {}
        rows = conn.execute(
            text("""SELECT subject_qid, value->>'value' AS v FROM kg.statement_current
                     WHERE property_pid = 'P298' AND subject_qid = ANY(:q) AND value IS NOT NULL"""),
            {"q": list(qids)},
        ).all()
        return {q: v for q, v in rows if isinstance(v, str) and re.fullmatch(r"[A-Z]{3}", v)}

    def labels_for_iso3(self, iso3s: Sequence[str]) -> dict[str, dict[str, str]]:
        """KG labels per ISO3: the reviewed registry's QID for the code, else the only KG item holding the
        code (P298). A code held by several items gets no KG label (the crosswalk label is used): the
        answer never names one of them silently (review V3, LINKS-2)."""
        kq = self._kq()
        registry = kq.country_qid_registry() if hasattr(kq, "country_qid_registry") else {}
        with self._conn() as conn:
            rows = conn.execute(
                text("""SELECT DISTINCT value->>'value' AS iso3, subject_qid FROM kg.statement_current
                         WHERE property_pid = 'P298' AND value->>'value' = ANY(:i)"""),
                {"i": list(iso3s)},
            ).all()
            holders: dict[str, set[str]] = {}
            for i, q in rows:
                holders.setdefault(i, set()).add(q)
            qid_of = {i: registry[i] for i in iso3s if i in registry}
            for i, qs in holders.items():
                if i not in qid_of and len(qs) == 1:
                    qid_of[i] = next(iter(qs))
            labels = kq.labels_for(conn, set(qid_of.values()))
        return {iso3: dict(labels[qid]) for iso3, qid in qid_of.items() if labels.get(qid)}

    def entity(self, qid: str) -> Entity | None:
        kq = self._kq()
        with self._conn() as conn:
            e = kq.entity(conn, qid)
        if e is None:
            return None
        prov = e.get("provenance") or {}
        return Entity(
            qid=qid,
            labels=e.get("labels") or {},
            descriptions=e.get("descriptions") or {},
            kind=e.get("kind"),
            sitelinks=int(e.get("sitelinks") or 0),
            source="kg",
            source_revision=prov.get("source_revision"),
            retrieved_at=str(prov["retrieved_at"]) if prov.get("retrieved_at") else None,
        )

    def neighbours(
        self, qid: str, *, properties: Sequence[str] | None, as_of: str | None, limit: int
    ) -> list[KgStatement]:
        kq = self._kq()
        with self._conn() as conn:
            w = kq.window(as_of, as_of) if as_of else None
            res = kq.neighbours(
                conn, qid, direction="both", properties=properties, time_window=w, include_literals=True, limit=limit
            )
            labels = kq.labels_for(
                conn,
                {qid}
                | {x["object"]["qid"] for x in res["edges"] if x.get("object")}
                | {x["subject"]["qid"] for x in res["edges"]},
            )
        return [statement_from_kg(x, labels) for x in res["edges"]]

    def timeline(self, qid: str, *, from_year: int | None, to_year: int | None, limit: int) -> list[KgStatement]:
        kq = self._kq()
        with self._conn() as conn:
            w = kq.window(None if from_year is None else str(from_year), None if to_year is None else str(to_year))
            res = kq.timeline(conn, qid, time_window=w, limit=limit)
            labels = kq.labels_for(
                conn,
                {qid}
                | {x["object"]["qid"] for x in res["events"] if x.get("object")}
                | {x["subject"]["qid"] for x in res["events"]},
            )
        return [statement_from_kg(x, labels) for x in res["events"]]


class KgPathAdapter:
    """:class:`~reva.nlqa.ports.PathPort` on KGCORE's bounded bidirectional search ``reva.kg.query.paths``
    ("instance of"/"subclass of" excluded, hubs skipped and reported by that engine)."""

    name = "reva.kg.query.paths"

    def __init__(self, kg: KgDbAdapter) -> None:
        self.kg = kg

    def paths(self, from_qid: str, to_qid: str, *, max_hops: int, as_of: str | None, limit: int) -> list[KgPath]:
        kq = self.kg._kq()
        with self.kg._conn() as conn:
            w = kq.window(as_of, as_of) if as_of else None
            res = kq.paths(conn, from_qid, to_qid, max_hops=max_hops, time_window=w, max_paths=limit)
            qids = {q for p in res.get("paths", []) for h in p["hops"] for q in (h["from"]["qid"], h["to"]["qid"])}
            labels = kq.labels_for(conn, qids)
        out = []
        for p in res.get("paths", []):
            hops = tuple(
                PathHop(statement_from_kg(h, labels), forward=h.get("direction") != "inverse") for h in p["hops"]
            )
            out.append(KgPath(hops, engine=self.name))
        return out


# --------------------------------------------------------------------------- atlas
def _atlas_cell(c: Mapping[str, Any], concept_id: str, snapshot: str | None) -> AtlasCell:
    prov = c.get("provenance") or {}
    sel = prov.get("selector")
    return AtlasCell(
        source=str(c["source"]),
        dataset=str(c["dataset"]),
        indicator=str(c["indicator"]),
        iso3=str(c.get("iso3") or ""),
        period_start=str(c["period_start"]),
        period_end=str(c["period_end"]),
        value=c.get("value"),
        value_text=c.get("value_text"),
        unit=c.get("unit"),
        concept_id=concept_id,
        entity_qid=c.get("entity_qid"),
        flags=c.get("flags") or {},
        raw_sha256=prov.get("raw_sha256"),
        selector=json.dumps(sel, sort_keys=True, ensure_ascii=False) if isinstance(sel, Mapping) else sel,
        provenance={
            k: v
            for k, v in {
                "table_artifact_sha256": prov.get("table_artifact_sha256"),
                "dataset_key": prov.get("dataset_key"),
                "atlas_snapshot_sha256": snapshot,
                "provider_geo": c.get("provider_geo"),
            }.items()
            if v
        },
    )


def _latest_key(c: AtlasCell) -> tuple[bool, int]:
    """Order for "the latest value": any actual cell before any projection, then by year."""
    return (not is_projection(c.flags), c.year if c.year is not None else -(10**6))


class AtlasQueryAdapter:
    """:class:`~reva.nlqa.ports.AtlasPort` on :class:`reva.atlas.query.AtlasQuery` + the atlas catalog."""

    def __init__(self, query: Any, catalog: Any) -> None:
        self.q = query
        self.catalog = catalog
        sha = getattr(getattr(query, "snapshot", None), "sha256", None)
        self.snapshot = sha
        self.name = f"reva.atlas snapshot {sha[:12]}" if sha else "reva.atlas"

    def concepts(self) -> list[Concept]:
        return [
            Concept(
                id=c.id,
                labels=dict(c.labels),
                unit=c.unit,
                domain=c.domain,
                definition=c.definition[:300],
                source_order=tuple(s.key for s in c.source_order),
                review_status="provider_declared_not_human_reviewed",
                rankable=getattr(c, "rankable", None) if isinstance(getattr(c, "rankable", None), bool) else None,
            )
            for c in self.catalog.concepts.values()
        ]

    def _declared(self, concept_id: str) -> tuple[str, ...]:
        c = self.catalog.concepts.get(concept_id)
        return tuple(s.key for s in c.source_order) if c else ()

    def _per_source(self, concept_id: str, iso3: str) -> list[AtlasCell]:
        try:
            s = self.q.series(self.catalog, concept_id, iso3)
        except RevaError as e:
            raise CapabilityUnavailable(f"atlas query failed: {e.code}", backend="atlas") from None
        cells = []
        for entry in s.get("per_source") or []:
            for c in entry.get("cells") or []:
                if c.get("year") is None or (c.get("value") is None and c.get("value_text") is None):
                    continue
                cells.append(_atlas_cell({**c, "iso3": iso3}, concept_id, self.snapshot))
        return cells

    def values(self, concept_id: str, iso3s: Sequence[str] | None, year: int | None) -> AtlasResult:
        declared = self._declared(concept_id)
        if iso3s is None:
            try:
                rows = self.q.best_cells(self.catalog, [concept_id], year=year)
            except RevaError as e:
                raise CapabilityUnavailable(f"atlas query failed: {e.code}", backend="atlas") from None
            return AtlasResult(tuple(_atlas_cell(c, concept_id, self.snapshot) for c in rows), declared)
        out: list[AtlasCell] = []
        for iso3 in iso3s:
            cells = self._per_source(concept_id, iso3)
            if year is not None:
                out.extend(c for c in cells if c.year == year)
            else:  # latest cell per series — the latest actual one; a projection only when nothing else exists
                latest: dict[str, AtlasCell] = {}
                for c in cells:
                    k = f"{c.source}.{c.dataset}.{c.indicator}"
                    if k not in latest or _latest_key(c) > _latest_key(latest[k]):
                        latest[k] = c
                out.extend(latest.values())
        return AtlasResult(tuple(out), declared)

    def series(self, concept_id: str, iso3: str, from_year: int | None, to_year: int | None) -> AtlasResult:
        cells = [
            c
            for c in self._per_source(concept_id, iso3)
            if (from_year is None or (c.year or 0) >= from_year) and (to_year is None or (c.year or 0) <= to_year)
        ]
        return AtlasResult(tuple(cells), self._declared(concept_id))


# --------------------------------------------------------------------------- links (LINKS layer)
#: user-level link kinds (the plan's ``link_kinds``) → LINKS link type ids (registry/links/link_types.yaml)
LINK_GROUPS: dict[str, tuple[str, ...]] = {
    "border": ("border_wikidata", "land_border", "land_border_cshapes", "maritime_neighbour"),
    "shared_language": (
        "shared_official_language",
        "shared_language_used",
        "shared_language_family",
        "shared_language_glottolog",
    ),
    "colonial": ("colony_of", "former_colony_of", "colonial_rule_cshapes"),
    "succession": ("successor_of",),
    "membership": ("co_member",),
    "alliance": ("alliance_co_member",),
    "treaty": ("co_signatory",),
    "conflict": ("conflict_co_participant", "conflict_same_side", "conflict_opposing_sides"),
    "diplomatic": ("diplomatic_relation", "unga_vote_agreement"),
    "trade": ("bilateral_trade", "bilateral_trade_imports", "bilateral_trade_baci"),
    "migration": ("migrant_stock",),
    "shared_institution": ("shared_head_of_state", "shared_currency", "shared_legal_family"),
    "peacekeeping": ("peacekeeping_contributor_to_host",),
    "historical_polity": ("shared_polity_territory",),
}


def link_kind(link_type: str) -> str:
    """The user-level group of a LINKS link type id (unknown ids keep their own name)."""
    for kind, types in LINK_GROUPS.items():
        if link_type in types:
            return kind
    return link_type


def _partial(v: Any) -> TimeValue | None:
    if v is None:
        return None
    iso = v.iso() if hasattr(v, "iso") else str(v)
    return TimeValue(iso, int(getattr(v, "precision", 9) or 9))


def statement_from_links(s: Any, labels: Mapping[str, Mapping[str, str]], props: Mapping[str, Any]) -> KgStatement:
    """A LINKS ``Stmt`` (duck-typed: ``pid`` or ``property``) → port record."""
    pid = str(getattr(s, "pid", None) or getattr(s, "property", ""))
    p = props.get(pid, {})
    obj = getattr(s, "object", None)
    return KgStatement(
        statement_id=str(s.statement_id),
        subject_qid=str(s.subject),
        property=pid,
        subject_labels=dict(labels.get(s.subject, {})),
        property_labels={k: str(p[f"label_{k}"]) for k in ("en", "fr") if p.get(f"label_{k}")},
        object_qid=str(obj) if obj else None,
        object_labels=dict(labels.get(obj, {})) if obj else {},
        literal=None if obj else getattr(s, "value", None),
        start=_partial(getattr(s, "start", None)),
        end=_partial(getattr(s, "end", None)),
        point_in_time=_partial(getattr(s, "point", None)),
        rank=str(getattr(s, "rank", "normal")),
        references=tuple(getattr(s, "references", ()) or ())[:10],
        source_key=str(getattr(s, "source_key", "wikidata")),
        source_revision=getattr(s, "source_revision", None),
        retrieved_at=getattr(s, "retrieved_at", None),
        raw_sha256=getattr(s, "raw_sha256", None),
        origin=getattr(s, "origin", None),
    )


def _evidence_records(
    evidence: Iterable[Any], labels: Mapping[str, Mapping[str, str]], props: Mapping[str, Any]
) -> tuple[tuple[KgStatement, ...], tuple[AtlasCell, ...]]:
    stmts, cells = [], []
    for ev in evidence:
        get = ev.get if isinstance(ev, Mapping) else lambda k, _e=ev: getattr(_e, k, None)
        if get("kind") == "kg_statement":
            pid = str(get("pid") or get("property") or "")
            obj = get("object")
            is_item = isinstance(obj, str) and (obj.startswith("Q") or ":" in obj)
            p = props.get(pid, {})
            stmts.append(
                KgStatement(
                    statement_id=str(get("statement_id")),
                    subject_qid=str(get("subject")),
                    property=pid,
                    subject_labels=dict(labels.get(str(get("subject")), {})),
                    property_labels={k: str(p[f"label_{k}"]) for k in ("en", "fr") if p.get(f"label_{k}")},
                    object_qid=obj if is_item else None,
                    object_labels=dict(labels.get(obj, {})) if is_item else {},
                    literal=None if is_item else obj,
                    source_key=str(get("source") or "wikidata"),
                    source_revision=get("source_revision"),
                    raw_sha256=get("raw_sha256"),
                    retrieved_at=get("retrieved_at"),
                    origin=get("origin"),
                )
            )
        elif get("kind") == "atlas_row":
            cells.append(
                AtlasCell(
                    source=str(get("source")),
                    dataset="",
                    indicator=str(get("pid") or get("property") or ""),
                    iso3=str(get("subject") or ""),
                    period_start="",
                    period_end="",
                    raw_sha256=get("raw_sha256"),
                    selector=get("selector"),
                )
            )
    return tuple(stmts), tuple(cells)


class LinksPathAdapter:
    """:class:`~reva.nlqa.ports.PathPort` on the LINKS path engine (``reva.links.service.path_between``:
    weighted k-shortest simple paths over KG statements + derived links, hubs penalised/blocked)."""

    name = "reva.links.paths"

    def __init__(self, kg: KgDbAdapter) -> None:
        self.kg = kg

    def paths(self, from_qid: str, to_qid: str, *, max_hops: int, as_of: str | None, limit: int) -> list[KgPath]:
        try:
            from reva.kg import query as kq
            from reva.links.service import path_between
        except ImportError:
            raise CapabilityUnavailable("reva.links is not installed", backend="links") from None
        year = int(as_of.lstrip("-").split("-")[0]) * (-1 if as_of.startswith("-") else 1) if as_of else None
        try:
            with self.kg._conn() as conn:
                res = path_between(conn, from_qid, to_qid, max_hops=min(max_hops, 6), as_of=year, k=max(1, limit))
                pids = {h.edge.type_id for p in res.paths for h in p.hops if h.edge.stmt is not None}
                props = kq.property_info(conn, pids)
        except CapabilityUnavailable:
            raise
        except Exception as e:  # LINKS is a separate layer: its failure is typed, never guessed around
            raise CapabilityUnavailable(f"path engine failed: {type(e).__name__}", backend="links") from None
        labels = res.labels or {}
        out = []
        for p in res.paths:
            hops = []
            for h in p.hops:
                e = h.edge
                if e.stmt is not None:
                    hops.append(PathHop(statement_from_links(e.stmt, labels, props), forward=bool(e.forward)))
                else:
                    lk = e.link
                    st, cells = _evidence_records(lk.evidence, labels, props)
                    t = getattr(lk, "temporal", None)
                    hops.append(
                        PathHop(
                            link=DerivedLinkHop(
                                link_type=str(lk.link_type),
                                subject_qid=lk.subject,
                                object_qid=lk.object,
                                subject_labels=dict(labels.get(lk.subject, {})),
                                object_labels=dict(labels.get(lk.object, {})),
                                statements=st,
                                cells=cells,
                                period=(t.label() or None) if t is not None else None,
                            ),
                            forward=bool(e.forward),
                        )
                    )
            out.append(KgPath(tuple(hops), engine=self.name))
        return out


class LinksAdapter:
    """:class:`~reva.nlqa.ports.LinksPort` on ``reva.links.service.links_for_country`` (derived from the KG
    neighbourhood of the country; atlas dyadic links when LINKS is given an atlas provider)."""

    name = "reva.links"

    def __init__(
        self,
        kg: KgDbAdapter,
        *,
        atlas_factory: Callable[[str | None], Any] | None = None,
        live_only: bool = False,
    ) -> None:
        self.kg = kg
        #: atlas snapshot sha (None = latest) -> AtlasQuery or None: the pair view re-reads full histories
        self.atlas_factory = atlas_factory
        self.live_only = live_only

    def pair(
        self, a: str, b: str, kinds: Sequence[str] | None, from_year: int | None, to_year: int | None
    ) -> PairLinks:
        """Every link between two countries (``reva.links.service.pair_view``): store + atlas snapshot re-read,
        predecessor-state links with their succession chain. Records keep their evidence for citation."""
        try:
            from reva.kg import query as kq
            from reva.links.service import UnknownCountry, UnknownLinkKind, pair_view
        except ImportError:
            raise CapabilityUnavailable("reva.links is not installed", backend="links") from None
        types = sorted({t for k in kinds for t in LINK_GROUPS.get(k, (k,))}) if kinds else None
        try:
            with self.kg._conn() as conn:
                try:
                    d = pair_view(
                        conn,
                        a,
                        b,
                        kinds=types,
                        from_year=from_year,
                        to_year=to_year,
                        atlas=self.atlas_factory,
                        live_only=self.live_only,
                    )
                except UnknownCountry:
                    return PairLinks(a, b, ())
                except UnknownLinkKind as e:
                    raise CapabilityUnavailable(
                        f"link type {e} is not in the LINKS taxonomy", backend="links"
                    ) from None
                pids = {
                    str(ev.get("pid") or "")
                    for k in d["kinds"].values()
                    for lk in k["links"]
                    for ev in lk.get("evidence", [])
                    if ev.get("kind") == "kg_statement"
                }
                props = kq.property_info(conn, {p for p in pids if p})
        except CapabilityUnavailable:
            raise
        except Exception as e:
            raise CapabilityUnavailable(f"pair links query failed: {type(e).__name__}", backend="links") from None
        labels = d.get("labels") or {}
        groups = []
        for t, k in sorted(d["kinds"].items(), key=lambda kv: (link_kind(kv[0]), kv[0])):
            groups.append(
                PairLinkGroup(
                    link_type=t,
                    kind=link_kind(t),
                    count=int(k.get("count") or 0),
                    first_year=k.get("first_year"),
                    last_year=k.get("last_year"),
                    links=tuple(_pair_link(lk, labels, props) for lk in k["links"]),
                    labels={x: str(v) for x, v in (k.get("labels") or {}).items()},
                    directed=bool(k.get("directed")),
                )
            )
        preds = tuple(
            PredecessorLink(
                country=str(p["country"]),
                predecessor_of=str(p["predecessor_of"]),
                link=_pair_link(p["link"], labels, props),
                succession=tuple(_pair_link(c, labels, props) for c in p.get("succession") or []),
            )
            for p in d.get("predecessor_links") or []
        )
        store = d.get("store") or {}
        try:
            from reva.links.taxonomy import load_taxonomy

            tax = load_taxonomy().link_types
            type_labels = {
                t: dict(tax[t].labels) for t in (*d["kinds"], *(d.get("kinds_without_links") or ())) if t in tax
            }
        except Exception:  # labels are cosmetic: ids are shown instead
            type_labels = {}
        return PairLinks(
            a,
            b,
            tuple(groups),
            kinds_without_links=tuple(d.get("kinds_without_links") or ()),
            predecessor_links=preds,
            truncated=tuple(d.get("truncated") or ()),
            node_labels={n: dict(v) for n, v in labels.items()},
            store=(
                f"materialisation {store.get('materialisation_id') or store.get('id')}"
                if store
                else "on demand (no materialised link store)"
            ),
            type_labels=type_labels,
        )

    def links(self, iso3: str, kinds: Sequence[str] | None, limit: int) -> list[CountryLink]:
        try:
            from reva.kg import query as kq
            from reva.links.paths import KgNeighbourhood
            from reva.links.service import AmbiguousCountry, UnknownCountry, links_for_country
        except ImportError:
            raise CapabilityUnavailable("reva.links is not installed", backend="links") from None
        types = sorted({t for k in kinds for t in LINK_GROUPS.get(k, (k,))}) if kinds else None
        try:
            with self.kg._conn() as conn:
                try:
                    res = links_for_country(conn, iso3, link_types=types)
                except UnknownCountry:
                    return []
                except AmbiguousCountry as e:
                    raise CapabilityUnavailable(
                        f"ISO3 {iso3} is held by several KG items ({', '.join(e.candidates[:5])}); none is chosen",
                        backend="links",
                    ) from None
                iso_by_qid = {q: i for i, q in KgNeighbourhood(conn).iso3_to_qid().items()}
                pids = {
                    str(ev.get("pid") or ev.get("property")) for lk in res["links"] for ev in lk.get("evidence", [])
                }
                props = kq.property_info(conn, {p for p in pids if p and p != "None"})
        except CapabilityUnavailable:
            raise
        except Exception as e:
            raise CapabilityUnavailable(f"links query failed: {type(e).__name__}", backend="links") from None
        labels = res.get("labels") or {}
        out = []
        for lk in res["links"]:
            a, b = iso_by_qid.get(lk["subject"]), iso_by_qid.get(lk["object"])
            if not a or not b:
                continue  # a link to a non-country node (organisation, language) is not a country–country link
            st, cells = _evidence_records(lk.get("evidence") or [], labels, props)
            t = lk.get("temporal") or {}
            period = (
                "–".join(x for x in (t.get("start"), t.get("end")) if x)
                or t.get("point")
                or (str(t["year"]) if t.get("year") is not None else None)
            )
            out.append(
                CountryLink(
                    kind=link_kind(lk["link_type"]),
                    link_type=lk["link_type"],
                    a_iso3=a,
                    b_iso3=b,
                    period=period,
                    weight=lk.get("weight"),
                    unit=lk.get("unit"),
                    statements=st,
                    cells=cells,
                    source=", ".join(lk.get("sources") or []) or None,
                    derived_by="reva.links",
                )
            )
            if len(out) >= limit:
                break
        return out


def _period_of(t: Mapping[str, Any]) -> str | None:
    return (
        "–".join(str(x) for x in (t.get("start"), t.get("end")) if x)
        or t.get("point")
        or (str(t["year"]) if t.get("year") is not None else None)
    )


def _pair_link(lk: Mapping[str, Any], labels: Mapping[str, Mapping[str, str]], props: Mapping[str, Any]) -> CountryLink:
    """One link of the pair view (``_link_json``) → port record; ends without an ISO3 (a predecessor state, a
    colony) keep their node id and label."""
    st, cells = _evidence_records(lk.get("evidence") or [], labels, props)
    iso = [*(lk.get("iso3") or ()), None, None]
    subj, obj = str(lk.get("subject")), str(lk.get("object"))
    basis = lk.get("basis") if isinstance(lk.get("basis"), Mapping) else {}
    return CountryLink(
        kind=link_kind(str(lk.get("link_type"))),
        link_type=str(lk.get("link_type")),
        a_iso3=iso[0] or subj,
        b_iso3=iso[1] or obj,
        a_label=pick_label(labels.get(subj), "en", subj),
        b_label=pick_label(labels.get(obj), "en", obj),
        period=_period_of(lk.get("temporal") or {}),
        weight=lk.get("weight"),
        unit=lk.get("unit"),
        statements=st,
        cells=cells,
        source=", ".join(lk.get("sources") or []) or None,
        derived_by="reva.links",
        basis=dict(basis or {}),
        facet=lk.get("facet") or None,
        a_node=None if iso[0] else subj,
        b_node=None if iso[1] else obj,
        via=tuple(str(v) for v in lk.get("via") or ()),
    )


# --------------------------------------------------------------------------- Wikidata search (optional)
class WikidataSearchAdapter:
    """``wbsearchentities`` (MediaWiki API) for identification only; bounded per process.

    It never builds an HTTP client: ``get`` is an injected, **budget-metered** GET (WDHARVEST's metered
    Wikidata client — ``reva.operations.budgets.MeteredHttp`` around the SafeHttpClient, with the
    machine-wide Wikidata lock) returning a :class:`~reva.adapters.http.safe_client.HttpResult`. Until such
    a client and a ``wikidata.wbsearchentities`` budget line exist it is not wired (see
    ``registry/atlas/budget_requests/nlqa.yaml``)."""

    name = "wikidata.wbsearchentities (live search)"
    HOST = "www.wikidata.org"

    def __init__(self, get: Callable[[str], Awaitable[Any]], *, max_per_hour: int = 120) -> None:
        self.get = get
        self.max_per_hour = max_per_hour
        self._lock = threading.Lock()
        self._calls: list[float] = []

    def _allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 3600]
            if len(self._calls) >= self.max_per_hour:
                return False
            self._calls.append(now)
            return True

    def search(self, text_: str, *, lang: str, limit: int) -> list[EntityMatch]:
        if not self._allow():
            raise CapabilityUnavailable("Wikidata search cap reached for this hour", backend="search")
        from urllib.parse import urlencode

        url = f"https://{self.HOST}/w/api.php?" + urlencode(
            {
                "action": "wbsearchentities",
                "search": text_[:120],
                "language": lang if lang in ("fr", "en") else "en",
                "uselang": lang if lang in ("fr", "en") else "en",
                "type": "item",
                "limit": max(1, min(limit, 10)),
                "format": "json",
            }
        )

        async def go() -> Any:
            return await self.get(url)

        try:
            res = asyncio.run(go())
            doc = json.loads(res.body)
        except Exception as e:
            raise CapabilityUnavailable(f"Wikidata search failed: {type(e).__name__}", backend="search") from None
        out = []
        for h in doc.get("search") or []:
            qid = h.get("id")
            if not isinstance(qid, str) or not re.fullmatch(r"Q[1-9][0-9]*", qid):
                continue
            label = h.get("label") or (h.get("display") or {}).get("label", {}).get("value")
            m = (h.get("match") or {}).get("type")
            out.append(
                EntityMatch(
                    Entity(
                        qid=qid,
                        labels={lang: str(label)} if label else {},
                        descriptions={lang: str(h["description"])} if h.get("description") else {},
                        source=self.name,
                    ),
                    "label_exact" if m == "label" else ("alias_exact" if m == "alias" else "search"),
                    str(label or ""),
                )
            )
        return out


# --------------------------------------------------------------------------- wiring
def build_backends(
    engine: Engine | None,
    settings: Any = None,
    *,
    atlas_store: Any = None,
    search_get: Callable[[str], Awaitable[Any]] | None = None,
) -> Backends:
    """Real backends for the API process; each one absent → ``None`` with a note (never a fake)."""
    b = Backends()
    if engine is not None:
        try:
            import reva.kg.query

            b.kg = KgDbAdapter(engine)
        except ImportError:
            b.notes.append("capability_unavailable: KG module absent")
        if b.kg is not None:
            try:
                import reva.links.service  # noqa: F401

                b.paths = LinksPathAdapter(b.kg)
                b.links = LinksAdapter(
                    b.kg,
                    atlas_factory=_atlas_snapshot_factory(engine, settings, atlas_store),
                    live_only=getattr(settings, "reva_env", "") == "production",
                )
            except ImportError:
                b.paths = KgPathAdapter(b.kg)
        try:
            b.atlas = _atlas(engine, settings, atlas_store)
        except CapabilityUnavailable as e:
            b.notes.append(f"capability_unavailable: atlas ({e.message})")
    if b.links is None:
        b.notes.append("capability_unavailable: interconnection layer (LINKS) not wired")
    if search_get is not None:  # a budget-metered Wikidata GET (WDHARVEST) — not wired by default
        b.search = WikidataSearchAdapter(search_get)
    return b


def _atlas_snapshot_factory(engine: Engine, settings: Any, store: Any) -> Callable[[str | None], Any]:
    """snapshot sha (None = latest; live only in production) → AtlasQuery, or None when none is readable."""
    holder: dict[str, Any] = {"store": store}

    def make(sha: str | None) -> Any:
        try:
            from reva.atlas.query import query_for
            from reva.atlas.snapshot import resolve_snapshot
        except ImportError:
            return None
        if holder["store"] is None:
            from reva.adapters.objects.factory import build_store

            holder["store"] = build_store(settings)
        origin = "live" if sha is None and getattr(settings, "reva_env", "") == "production" else None
        try:
            with engine.connect() as conn:
                snap = resolve_snapshot(conn, holder["store"], origin=origin, sha256=sha)
            return query_for(snap, holder["store"])
        except (RevaError, SQLAlchemyError, OSError):
            return None

    return make


def _atlas(engine: Engine, settings: Any, store: Any) -> AtlasQueryAdapter | None:
    try:
        from reva.atlas.catalog import cached_catalog
        from reva.atlas.query import query_for
        from reva.atlas.snapshot import resolve_snapshot
    except ImportError:
        raise CapabilityUnavailable("atlas module absent", backend="atlas") from None
    if store is None:
        from reva.adapters.objects.factory import build_store

        store = build_store(settings)
    try:
        with engine.connect() as conn:
            snap = resolve_snapshot(conn, store)
        return AtlasQueryAdapter(query_for(snap, store), cached_catalog())
    except (RevaError, SQLAlchemyError, OSError) as e:
        raise CapabilityUnavailable(f"no atlas snapshot: {type(e).__name__}", backend="atlas") from None
