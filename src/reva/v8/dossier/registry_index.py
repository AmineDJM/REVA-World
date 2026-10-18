"""Compact read-only index of the sealed forecast registry for briefs.

The registry API reads each sealed batch manifest per question (verified, LRU of 16 manifests); a brief needs a
few questions of many batches, which thrashes that cache (minutes with 100+ batches). This index reads every batch
manifest ONCE (``load_frozen_manifest``: sha256-verified), keeps only what briefs show (question fields, each
model's point and 5 %/95 % quantiles), merges supplementary batches into their original batch like the registry
read side, joins the outcomes, and caches the result per batch set (process memory + a JSON file in the temp dir).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from reva.evaluation.ledger import load_frozen_manifest
from reva.evaluation.registry import read as R

_LOCK = threading.Lock()
_MEM: dict[str, dict[str, Any]] = {}
DISK = Path(tempfile.gettempdir()) / "reva-dossier-cache"
Q_FIELDS = (
    "question_id", "family", "label_fr", "geo", "geo_label_fr", "reference_period", "type", "unit", "last_observed",
)  # fmt: skip


def _compact_forecast(f: dict[str, Any]) -> dict[str, Any]:
    disp = R._display(f)  # sealed point-only records stay point-only
    q = disp.get("quantiles") or {}
    return {"model": f["model"], "point": disp.get("point"), "q05": q.get("0.05"), "q95": q.get("0.95")}


def build_index(conn: Any, store: Any, origin: str) -> dict[str, Any]:
    bs = R.batches(conn, origin)
    key = hashlib.sha256(json.dumps(sorted(b["batch_sha256"] for b in bs) + [origin]).encode()).hexdigest()[:20]
    with _LOCK:
        if key in _MEM:
            return _MEM[key]
    path = DISK / f"registry-index-{key}.json"
    try:
        idx = json.loads(path.read_text())
    except (OSError, ValueError):
        idx = None
    if idx is None:
        questions: dict[str, dict[str, Any]] = {}
        forecasts: dict[str, list[dict[str, Any]]] = {}
        for b in bs:
            m = load_frozen_manifest(store, b["batch_sha256"])
            for q in m.get("questions", []):
                if q["question_id"] not in questions and not m.get("supplement_of"):
                    questions[q["question_id"]] = {
                        **{k: q.get(k) for k in Q_FIELDS},
                        "expected_resolution": (q.get("resolution") or {}).get("expected_date"),
                        "batch_sha256": b["batch_sha256"],
                    }
            for f in m.get("forecasts", []):
                if f.get("status") == "issued":
                    forecasts.setdefault(f["question_id"], []).append(_compact_forecast(f))
        oc = R.outcomes(conn, store, origin)
        for qid, q in questions.items():
            o = oc.get(qid) or {}
            q["status"] = o.get("status", "pending")
            q["outcome"] = o.get("value")
        idx = {"key": key, "questions": questions, "forecasts": forecasts}
        try:
            DISK.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx))
            tmp.replace(path)
        except OSError:  # pragma: no cover - only costs recomputation
            pass
    with _LOCK:
        _MEM.clear()
        _MEM[key] = idx
    return idx
