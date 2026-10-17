"""In-memory index of one atlas snapshot's series for the V8 catalog search (built once per snapshot).

Tables -> measures -> variants (indicator, filters); catalog concepts by series; folded search text. Built from
the query layer's own ``series_of`` map and the concept catalog: ~0.6 M series index in about a second, then kept
for the process (one per snapshot, at most two).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from reva.atlas.catalog import Catalog, Concept
from reva.atlas.query import AtlasQuery
from reva.v8.series.core import Dims, default_score, fold, parse_indicator, tokens


@dataclass(slots=True)
class Variant:
    sid: int
    indicator: str
    dims: Dims


@dataclass(slots=True)
class Measure:
    table: str
    measure: str
    variants: list[Variant] = field(default_factory=list)
    folded: str = ""

    def default(self) -> Variant:
        return min(self.variants, key=lambda v: default_score(v.dims))

    def facets(self, limit: int = 200) -> list[dict[str, Any]]:
        """Each filter key with its values in first-seen order (bounded)."""
        keys: dict[str, list[str]] = {}
        for v in self.variants:
            for k, val in v.dims:
                vals = keys.setdefault(k, [])
                if val not in vals and len(vals) < limit:
                    vals.append(val)
        return [{"key": k, "values": vals, "truncated": len(vals) >= limit} for k, vals in keys.items()]

    def resolve(self, filters: dict[str, str]) -> Variant | None:
        """The variant matching every given filter (other keys at their default-scored value)."""
        cands = [v for v in self.variants if all(dict(v.dims).get(k) == val for k, val in filters.items())]
        if not cands:
            return None
        return min(cands, key=lambda v: default_score(v.dims))


@dataclass
class Table:
    key: str
    source: str
    dataset: str
    measures: dict[str, Measure] = field(default_factory=dict)
    n_series: int = 0


@dataclass
class ConceptHit:
    concept: Concept
    folded_label: str
    folded_all: str


class SeriesIndex:
    def __init__(self, q: AtlasQuery, catalog: Catalog):
        self.snapshot = q.snapshot.sha256
        self.tables: dict[str, Table] = {}
        for sid, (src, ds, ind) in q.series_of.items():
            tkey = f"{src}.{ds}"
            t = self.tables.get(tkey)
            if t is None:
                t = self.tables[tkey] = Table(tkey, src, ds)
            m, dims = parse_indicator(ind)
            meas = t.measures.get(m)
            if meas is None:
                meas = t.measures[m] = Measure(tkey, m, folded=fold(f"{m} {tkey}"))
            meas.variants.append(Variant(sid, ind, dims))
            t.n_series += 1
        # concepts by series key, and the concepts' search text
        self.concepts_of: dict[str, list[str]] = {}
        self.concepts: list[ConceptHit] = []
        for c in catalog.concepts.values():
            for s in c.source_order:
                self.concepts_of.setdefault(s.key, []).append(c.id)
            label = " ".join(v for v in (c.labels.get("fr"), c.labels.get("en")) if v)
            text = " ".join(
                [label, c.id, c.unit, c.theme or "", c.domain, c.subdomain or "", " ".join(s.key for s in c.source_order)]
            )
            self.concepts.append(ConceptHit(c, fold(label), fold(text + " " + c.definition[:300])))
        self.catalog = catalog
        self.sid_of = q.sid_of

    # ------------------------------------------------------------------ lookups
    def measure(self, table: str, measure: str) -> Measure | None:
        t = self.tables.get(table)
        return t.measures.get(measure) if t else None

    def concept_for(self, series: str) -> Concept | None:
        ids = self.concepts_of.get(series)
        return self.catalog.concepts.get(ids[0]) if ids else None

    def primary_series(self, c: Concept) -> tuple[str, str, str] | None:
        """The first series of the concept's source order present in this snapshot."""
        for s in c.source_order:
            if (s.source, s.dataset, s.indicator) in self.sid_of:
                return s.source, s.dataset, s.indicator
        return None

    # ------------------------------------------------------------------ search
    def search(self, query: str, *, theme: str | None = None, limit: int = 30) -> dict[str, Any]:
        toks = tokens(query)
        concept_hits: list[tuple[float, ConceptHit]] = []
        for h in self.concepts:
            c = h.concept
            if theme and c.theme != theme:
                continue
            if not toks:
                score = 0.0
            else:
                if not all(t in h.folded_all for t in toks):
                    continue
                score = 0.0
                for t in toks:
                    if h.folded_label.startswith(t):
                        score += 6
                    elif f" {t}" in f" {h.folded_label}":
                        score += 4
                    elif t in h.folded_label:
                        score += 2
                    else:
                        score += 0.5
                score -= len(h.folded_label) / 200  # shorter labels first among equals
            if self.primary_series(c) is None:
                score -= 100  # listed after concepts with data in this snapshot
            concept_hits.append((score, h))
        concept_hits.sort(key=lambda x: (-x[0], x[1].concept.id))
        tables: list[Table] = []
        measures: list[Measure] = []
        if toks and not theme:
            for t in self.tables.values():
                ft = fold(t.key)
                if all(tok in ft for tok in toks):
                    tables.append(t)
            for t in self.tables.values():
                for m in t.measures.values():
                    if all(tok in m.folded for tok in toks):
                        measures.append(m)
                        if len(measures) >= limit * 4:
                            break
                if len(measures) >= limit * 4:
                    break
            measures.sort(key=lambda m: (len(m.measure), m.table, m.measure))
        return {
            "concepts": [h for _, h in concept_hits[:limit]],
            "concepts_total": len(concept_hits),
            "tables": sorted(tables, key=lambda t: t.key)[:limit],
            "measures": measures[:limit],
        }


_INDEXES: OrderedDict[str, SeriesIndex] = OrderedDict()
_LOCK = threading.Lock()


def index_for(q: AtlasQuery, catalog: Catalog) -> SeriesIndex:
    key = f"{q.snapshot.sha256}|{id(catalog)}"
    with _LOCK:
        idx = _INDEXES.get(key)
        if idx is not None:
            _INDEXES.move_to_end(key)
            return idx
    idx = SeriesIndex(q, catalog)
    with _LOCK:
        _INDEXES[key] = idx
        while len(_INDEXES) > 2:
            _INDEXES.popitem(last=False)
    return idx
