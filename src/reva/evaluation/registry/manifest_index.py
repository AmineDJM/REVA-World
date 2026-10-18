"""Shared index of verified sealed manifests (V8 PERFQ): read each manifest once, then serve slices of it.

A sealed manifest (registry batch, health batch, resolution) is immutable and addressed by its sha256. It is read
and verified once per process (``load_frozen_manifest``: byte hash + canonical form; the verification result is
cached per sha, like ``read.ManifestCache``), then kept as small zlib-compressed pickles:

* one slice per question_id: the first question with that id and its forecasts, in manifest order;
* the questions list, the forecast order and every other top-level key (the "header").

So ``question()`` costs a few kB of unpickling, the whole registry a few MB of memory instead of ~60 MB of JSON
(several hundred MB once parsed: ``ManifestCache(16)`` could not hold the 25+ batches and re-verified them on
every request), and ``manifest()`` rebuilds a dict equal to ``load_frozen_manifest`` for callers that need all of
it. Which manifests exist, their order, outcomes and scores are always read from the database by the callers:
nothing here can go stale (a sha never changes content), so there is no invalidation to get wrong. Every accessor
returns fresh objects (callers may mutate them). The pickles are produced here from verified content and never
read from outside the process.
"""

from __future__ import annotations

import pickle
import zlib
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from reva.evaluation.ledger import load_frozen_manifest

_LEVEL = 6
_ZDICT_BYTES = 32768  # zlib window: a preset dictionary sampled from the manifest's own slices (~5x smaller)


def _pack(obj: Any, zdict: bytes | None = None) -> bytes:
    return _z(pickle.dumps(obj, protocol=5), zdict)


def _z(raw: bytes, zdict: bytes | None) -> bytes:
    co = zlib.compressobj(_LEVEL, zdict=zdict) if zdict else zlib.compressobj(_LEVEL)
    return co.compress(raw) + co.flush()


def _unpack(raw: bytes, zdict: bytes | None = None) -> Any:
    d = zlib.decompressobj(zdict=zdict) if zdict else zlib.decompressobj()
    return pickle.loads(d.decompress(raw) + d.flush())  # noqa: S301 - produced in-process from a verified manifest


def _indexable(xs: Any) -> bool:
    return isinstance(xs, list) and all(isinstance(x, Mapping) and isinstance(x.get("question_id"), str) for x in xs)


@dataclass(frozen=True)
class ManifestIndex:
    """One verified manifest, sliced by question_id."""

    sha: str
    keys: tuple[str, ...]  # top-level keys in manifest order
    supplement_of: Any  # manifest.get("supplement_of")
    meta: dict[str, Any]  # label, code_digest, data_tables when present (small, read on every question())
    n_issued: dict[str, int]  # question_id -> forecasts with status "issued"
    slices: dict[str, bytes]  # question_id -> pack((first question with that id | None, [its forecasts]), zdict)
    zdict: bytes | None  # preset dictionary of the slices
    _header: bytes  # pack({key: value} for every key except indexed questions/forecasts)
    _questions: bytes | None  # pack(questions list) when indexed
    _order: bytes | None  # pack([question_id of each forecast, in manifest order]) when indexed

    def get(self, question_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """The first question with this id (None if only forecasts carry it) and its forecasts, manifest order."""
        if self._questions is None:  # not a questions/forecasts manifest: plain scan, as the manifest reads
            h = _unpack(self._header)
            qs = [x for x in h.get("questions") or [] if isinstance(x, Mapping) and x.get("question_id") == question_id]
            fs = [x for x in h.get("forecasts") or [] if isinstance(x, Mapping) and x.get("question_id") == question_id]
            return (qs[0] if qs else None), fs
        raw = self.slices.get(question_id)
        if raw is None:
            return None, []
        q, fcs = _unpack(raw, self.zdict)
        return q, fcs

    def header(self) -> dict[str, Any]:
        """Every top-level key except the indexed questions/forecasts lists."""
        return dict(_unpack(self._header))

    def questions(self) -> list[dict[str, Any]]:
        if self._questions is None:
            return list(_unpack(self._header).get("questions") or [])
        return list(_unpack(self._questions))

    def forecasts(self) -> list[dict[str, Any]]:
        """All forecasts in manifest order."""
        if self._order is None:
            return list(_unpack(self._header).get("forecasts") or [])
        return list(self._iter_forecasts(_unpack(self._order)))

    def _iter_forecasts(self, order: list[str]) -> Iterator[dict[str, Any]]:
        lists: dict[str, Iterator[dict[str, Any]]] = {}
        for qid in order:
            it = lists.get(qid)
            if it is None:
                it = lists[qid] = iter(self.get(qid)[1])
            yield next(it)

    def manifest(self) -> dict[str, Any]:
        """The whole manifest, equal to ``load_frozen_manifest`` (same keys, order and values)."""
        h = _unpack(self._header)
        if self._questions is not None:
            h["questions"] = _unpack(self._questions)
        if self._order is not None:
            h["forecasts"] = list(self._iter_forecasts(_unpack(self._order)))
        return {k: h[k] for k in self.keys}

    @property
    def size_bytes(self) -> int:
        n = len(self._header) + len(self._questions or b"") + len(self._order or b"") + len(self.zdict or b"")
        return n + sum(len(v) for v in self.slices.values())


def build(manifest: Mapping[str, Any], sha: str) -> ManifestIndex:
    """Pure: index an already verified manifest."""
    qs, fcs = manifest.get("questions"), manifest.get("forecasts")
    ok = "questions" in manifest and "forecasts" in manifest and _indexable(qs) and _indexable(fcs)
    first: dict[str, Any] = {}
    by_q: dict[str, list[Any]] = {}
    n_issued: dict[str, int] = {}
    if ok:
        for q in qs:
            first.setdefault(q["question_id"], q)
        for f in fcs:
            by_q.setdefault(f["question_id"], []).append(f)
            if f.get("status") == "issued":
                n_issued[f["question_id"]] = n_issued.get(f["question_id"], 0) + 1
    raws = {qid: pickle.dumps((first.get(qid), by_q.get(qid, [])), protocol=5) for qid in first.keys() | by_q.keys()}
    sample = [raws[k] for k in sorted(raws)]
    sample = sample[:: max(1, len(sample) // 8)]
    zdict = b"".join(sample)[-_ZDICT_BYTES:] or None
    slices = {qid: _z(raw, zdict) for qid, raw in raws.items()}
    header = {k: v for k, v in manifest.items() if not (ok and k in ("questions", "forecasts"))}
    meta: dict[str, Any] = {k: manifest[k] for k in ("label", "code_digest") if k in manifest}
    data = manifest.get("data")
    if isinstance(data, Mapping) and "tables" in data:
        meta["data_tables"] = data["tables"]
    return ManifestIndex(
        sha=sha,
        keys=tuple(manifest.keys()),
        supplement_of=manifest.get("supplement_of"),
        meta=meta,
        n_issued=n_issued,
        slices=slices,
        zdict=zdict,
        _header=_pack(header),
        _questions=_pack(qs) if ok else None,
        _order=_pack([f["question_id"] for f in fcs]) if ok else None,
    )


class Index:
    """Verified manifest indexes by sha256 (bounded LRU; content never changes once sealed)."""

    def __init__(self, size: int = 1024) -> None:
        self.size = size
        self._d: OrderedDict[str, ManifestIndex] = OrderedDict()
        self._lock = Lock()
        self._build_lock = Lock()  # one build at a time: a cold burst verifies each manifest once
        self.builds = 0

    def peek(self, sha: str) -> ManifestIndex | None:
        with self._lock:
            return self._d.get(sha)

    def get(self, store: Any, sha: str) -> ManifestIndex:
        with self._lock:
            hit = self._d.get(sha)
            if hit is not None:
                self._d.move_to_end(sha)
                return hit
        with self._build_lock:
            hit = self.peek(sha)
            if hit is None:
                hit = build(load_frozen_manifest(store, sha), sha)  # IntegrityFailure propagates, nothing cached
                self.builds += 1
            with self._lock:
                self._d[sha] = hit
                self._d.move_to_end(sha)
                while len(self._d) > self.size:
                    self._d.popitem(last=False)
        return hit

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return sum(v.size_bytes for v in self._d.values())


INDEX = Index()

__all__ = ["INDEX", "Index", "ManifestIndex", "build"]
