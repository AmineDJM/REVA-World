"""V8 « Demander » service: one question → the NLQA answer (:func:`reva.nlqa.service.answer_question`, keyword
planner unless the API enables the LLM planner) → the V8 answer bundle (:mod:`reva.v8.ask.bundle`).

The time series shown under an answer is re-read from the source the answer cited (same atlas snapshot or the
same registered table version), point by point with its period and projection flag; nothing is interpolated.
Answers are cached for :data:`CACHE_TTL_S` per (question, language) in this process (an answer is a read of
immutable snapshots; the cache only spares recomputation when a page is reloaded).
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.nlqa.ports import cell_is_projection
from reva.v8.ask import bundle as B
from reva.v8.ask.brainstorm import Brainstormer

CACHE_TTL_S = 300.0
CACHE_SIZE = 128
MAX_SERIES_POINTS = 600


class _Cache:
    def __init__(self) -> None:
        self._d: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._d.get(key)
            if hit is None or time.monotonic() - hit[0] > CACHE_TTL_S:
                return None
            self._d.move_to_end(key)
            return hit[1]

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._d[key] = (time.monotonic(), value)
            while len(self._d) > CACHE_SIZE:
                self._d.popitem(last=False)


def _cell_point(c: Any) -> dict[str, Any]:
    return {
        "t": str(c.period_start)[:10],
        "t_end": str(c.period_end)[:10],
        "v": c.value,
        "projection": bool(cell_is_projection(c)),
        "source": f"{c.source}/{c.dataset}",
    }


class SeriesReader:
    """Re-reads the cited series for the evidence chart (atlas snapshot, or the registered table version)."""

    def __init__(self, backends: Any, catalog: Any) -> None:
        self.b = backends
        self.catalog = catalog

    def __call__(self, ans: Mapping[str, Any], cells: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        ops = list((ans.get("plan") or {}).get("operations") or [])
        if len(ops) != 1:
            return None
        op = ops[0]
        c0 = cells[0]
        cit = c0.get("citation") or {}
        key = (cit.get("source"), cit.get("dataset"), cit.get("indicator"))
        try:
            if op.get("op") == "atlas_period":
                if cit.get("outside_snapshot") and getattr(self.b, "tables", None) is not None:
                    got, _ = self.b.tables.period_cells(op["concept"], [key], op.get("from_year"), op.get("to_year"))
                else:
                    got = list(self.b.atlas.period_cells(op["concept"], op.get("from_year"), op.get("to_year")).cells)
            elif op.get("op") in ("atlas_value", "atlas_series") and op.get("country") and len(cells) <= 2:
                iso3 = c0.get("iso3") or op.get("country")
                got = list(self.b.atlas.series(op["concept"], iso3, None, None).cells)
            else:
                return None
        except Exception:
            return None
        pts = [_cell_point(c) for c in got if (c.source, c.dataset, c.indicator) == key and c.value is not None]
        pts.sort(key=lambda p: (p["t"], p["t_end"]))
        if len(pts) < 3:
            return None
        cited = {str(c.get("period_start"))[:10] for c in cells}
        for p in pts:
            p["cited"] = p["t"] in cited
        concept = self.catalog.concepts.get(op.get("concept") or "")
        return {
            "type": "timeseries",
            "series": [
                {
                    "label": concept.label(ans.get("language") or "fr") if concept else op.get("concept"),
                    "concept": op.get("concept"),
                    "unit": c0.get("unit"),
                    "iso3": c0.get("iso3") or None,
                    "source": f"{cit.get('source')}/{cit.get('dataset')}",
                    "indicator": cit.get("indicator"),
                    "licence": B._rights_text(cit),
                    "outside_snapshot": bool(cit.get("outside_snapshot")),
                    "points": pts[-MAX_SERIES_POINTS:],
                    "truncated": len(pts) > MAX_SERIES_POINTS,
                }
            ],
            "note": "série relue à la source citée ; points non interpolés",
        }


class MemoForecasts:
    """Memo of the forecast-registry port (immutable sealed manifests; outcomes refreshed after ``ttl`` seconds).

    The registry read path re-verifies every sealed manifest per call (104 batches on the dev DB against a
    16-manifest LRU): ~20–35 s per call under load. Sealed content never changes, so the V8 page memoises the
    answers per (family, geo) and per question for ``ttl`` (outcomes and scores can only appear later)."""

    def __init__(self, inner: Any, ttl: float = 900.0) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "forecasts")
        self.ttl = ttl
        self._d: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _memo(self, key: tuple[Any, ...], fn: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        v = fn()
        with self._lock:
            self._d[key] = (now, v)
            if len(self._d) > 2000:
                self._d.clear()
        return v

    def families(self) -> Any:
        return self._memo(("families",), self.inner.families)

    def questions(self, family: str, geo: str | None, *, limit: int) -> Any:
        return self._memo(("q", family, geo, limit), lambda: self.inner.questions(family, geo, limit=limit))

    def question(self, question_id: str) -> Any:
        return self._memo(("d", question_id), lambda: self.inner.question(question_id))

    def models(self) -> Any:
        return self._memo(("models",), self.inner.models)


class AskService:
    def __init__(
        self, backends: Any, catalog: Any, *, drafters: Any = None, ledger: Any = None, budget: Any = None
    ) -> None:
        if getattr(backends, "forecasts", None) is not None and not isinstance(backends.forecasts, MemoForecasts):
            try:
                backends = dataclasses.replace(backends, forecasts=MemoForecasts(backends.forecasts))
            except TypeError:  # not a dataclass (test double): used as is
                pass
        self.backends = backends
        self.catalog = catalog
        self.drafters = drafters
        self.ledger = ledger
        self.budget = budget
        self.cache = _Cache()
        self.reader = SeriesReader(backends, catalog)

    async def answer(self, question: str, *, lang: str | None = None) -> dict[str, Any]:
        from reva.nlqa.service import AskInput, answer_question, clean_question

        q = clean_question(question)
        key = hashlib.sha256(f"{lang or ''}\x00{q}".encode()).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return {**hit, "cached": True}
        t0 = time.perf_counter()
        r = await answer_question(
            AskInput(q, key, lang=lang),
            catalog=self.catalog,
            backends=self.backends,
            drafters=self.drafters,
            ledger=self.ledger,
            budget=self.budget,
        )
        ans = r.as_json()
        out = B.build(ans, q, series_reader=self.reader)
        self._label(out, ans.get("language") or "fr")
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["cached"] = False
        self.cache.put(key, out)
        return out

    def _label(self, out: dict[str, Any], lang: str) -> None:
        """Catalog labels for the concept ids shown (headline, bars, series), so the page never shows a bare id."""

        def lab(cid: Any) -> str | None:
            c = self.catalog.concepts.get(str(cid)) if cid else None
            return c.label(lang) if c is not None else None

        h = out.get("headline") or {}
        if h.get("concept"):
            h["concept_label"] = lab(h["concept"])
        v = out.get("viz") or {}
        for r in v.get("rows") or []:
            if isinstance(r, dict) and r.get("concept"):
                r["label"] = lab(r["concept"]) or r.get("label")
        for sr in v.get("series") or []:
            if isinstance(sr, dict) and sr.get("concept"):
                sr["label"] = lab(sr["concept"]) or sr.get("label")
        for t in out.get("targets") or []:
            if t.get("kind") in ("monde", "comparer") and t.get("label"):
                t["label"] = lab(t["label"]) or t["label"]

    def brainstorm(self, target: str) -> dict[str, Any]:
        return Brainstormer(self.backends, self.catalog).run(target)
