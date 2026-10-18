"""Per-batch question index of the forecast registry (V8 PERFQ): question_id -> that question's sealed slice.

A sealed batch manifest is immutable and addressed by its sha256, so its index is built once per sha from the
*verified* manifest (``load_frozen_manifest``: byte hash + canonical form) and reused for the life of the process
(the verification result is cached per sha, like ``read.ManifestCache`` and ``reva.v8.scores``). Which batches
exist, their order, and the outcomes/scores are still read from the database on every request, so a new batch,
supplement, resolution or void is visible immediately and nothing here can go stale.

Each slice (the question as sealed and its forecasts, in manifest order) is kept pickled and zlib-compressed, so
the whole registry costs a few MB of memory instead of the ~60 MB of manifests JSON (and several hundred MB once
parsed), and one lookup unpickles only the few kB it needs. The pickles are produced here, from verified content,
and never read from outside the process.
"""

from __future__ import annotations

import pickle
import zlib
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from threading import Lock
from typing import Any

from reva.evaluation.ledger import load_frozen_manifest


@dataclass(frozen=True)
class BatchIndex:
    """One verified batch manifest, indexed by question_id (only what ``read.question`` reads from it)."""

    sha: str
    supplement_of: Any  # manifest["supplement_of"] as sealed (None for an original batch)
    meta: dict[str, Any]  # label, data_tables, code_digest as sealed (absent keys stay absent)
    slices: dict[str, bytes]  # question_id -> zlib(pickle((first question with that id | None, [forecasts])))

    def get(self, question_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        raw = self.slices.get(question_id)
        if raw is None:
            return None, []
        q, fcs = pickle.loads(zlib.decompress(raw))  # noqa: S301 — produced in-process from a verified manifest
        return q, fcs


def build(manifest: dict[str, Any], sha: str) -> BatchIndex:
    """Pure: index an already verified manifest."""
    qs: dict[str, dict[str, Any]] = {}
    for q in manifest["questions"]:
        qs.setdefault(q["question_id"], q)  # read.question takes the first match
    fcs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in manifest["forecasts"]:
        fcs[f["question_id"]].append(f)
    sup = manifest.get("supplement_of")
    keep_q = not sup  # supplementary batches only contribute forecasts
    slices = {
        qid: zlib.compress(pickle.dumps((qs.get(qid) if keep_q else None, fcs.get(qid, [])), protocol=5), 1)
        for qid in qs.keys() | fcs.keys()
    }
    meta: dict[str, Any] = {k: manifest[k] for k in ("label", "code_digest") if k in manifest}
    if isinstance(manifest.get("data"), dict) and "tables" in manifest["data"]:
        meta["data_tables"] = manifest["data"]["tables"]
    return BatchIndex(sha=sha, supplement_of=sup, meta=meta, slices=slices)


class QuestionIndex:
    """Verified batch indexes by sha256 (bounded LRU; a batch never changes once sealed)."""

    def __init__(self, size: int = 512) -> None:
        self.size = size
        self._d: OrderedDict[str, BatchIndex] = OrderedDict()
        self._lock = Lock()
        self._build_lock = Lock()  # one build at a time: a cold burst verifies each manifest once

    def get(self, store: Any, sha: str) -> BatchIndex:
        with self._lock:
            hit = self._d.get(sha)
            if hit is not None:
                self._d.move_to_end(sha)
                return hit
        with self._build_lock:
            with self._lock:
                hit = self._d.get(sha)
            if hit is None:
                hit = build(load_frozen_manifest(store, sha), sha)  # IntegrityFailure propagates, nothing cached
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


INDEX = QuestionIndex()

__all__ = ["INDEX", "BatchIndex", "QuestionIndex", "build"]
