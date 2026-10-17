"""Live state of the prospective forecast registries for the V8 scoreboard (read-only).

Counts sealed batches, questions, forecasts and outcomes of the main registry (``forecast_registry``) and of the
health registry (``forecast_registry_health``), the next expected resolution dates per family, which models
have sealed forecasts (to say whether a "prospective" scoreboard row is actually sealed), and a summary of the
live REVA-ensemble scoreboard. Reading every sealed manifest takes seconds, so the result is cached per origin
and recomputed only when the registry's fingerprint (row counts, latest ``recorded_at``) changes.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from threading import Lock, Thread
from typing import Any

from sqlalchemy import text

LOG = logging.getLogger(__name__)
SCOPES = ("forecast_registry", "forecast_registry_health")
FINGERPRINT_TTL_S = 30.0
OFFICIAL_REFERENCES = {
    "forecast.reference_imf_weo": "imf_weo",
    "forecast.reference_eia_steo": "eia_steo",
    "forecast.reference_views": "views",
    "forecast.reference_flusight_ensemble": "cdc_flusight",
    "forecast.reference_covidhub_ensemble": "cdc_covidhub",
}


def _base(model: str | None) -> str:
    return (model or "").split("@")[0]


def fingerprint(conn: Any, origin: str | None) -> tuple[Any, ...]:
    oc = " AND task_scope->>'origin' = :o" if origin else ""
    row = conn.execute(
        text(
            f"""SELECT count(*), max(recorded_at) FROM model.evaluation
                WHERE task_scope->>'kind' IN ('forecast_registry', 'forecast_registry_health'){oc}"""
        ),
        {"o": origin} if origin else {},
    ).one()
    return (int(row[0]), row[1].isoformat() if row[1] else None)


def sealed_models(conn: Any, origin: str | None) -> dict[str, int]:
    """model key (without version) -> forecasts issued over every sealed batch of both registries (cheap SQL)."""
    oc = " AND task_scope->>'origin' = :o" if origin else ""
    rows = conn.execute(
        text(
            f"""SELECT metrics->>'model' AS m, sum(coalesce((metrics->>'n_issued')::int, 0)) AS n
                FROM model.evaluation WHERE metrics->>'kind' IN ('registry_issue', 'health_issue')
                  AND task_scope->>'kind' IN ('forecast_registry', 'forecast_registry_health'){oc}
                GROUP BY 1"""
        ),
        {"o": origin} if origin else {},
    ).mappings()
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[_base(r["m"])] += int(r["n"] or 0)
    return dict(out)


def _main_registry(conn: Any, store: Any, origin: str | None) -> dict[str, Any]:
    from reva.evaluation.registry import read as R

    batches = R.batches(conn, origin)
    oc_all = R.outcomes(conn, store, origin)
    seen: set[str] = set()
    questions: list[dict[str, Any]] = []
    n_forecasts = 0
    for b in batches:
        m = R.CACHE.get(store, b["batch_sha256"])
        refs: dict[str, set[str]] = defaultdict(set)
        for f in m["forecasts"]:
            if f["status"] == "issued":
                n_forecasts += 1
                fid = OFFICIAL_REFERENCES.get(_base(f.get("model")))
                if fid:
                    refs[f["question_id"]].add(fid)
        for q in m["questions"]:
            qid = q["question_id"]
            if qid in seen:
                if refs.get(qid):
                    next(x for x in questions if x["id"] == qid)["officials"] |= refs[qid]
                continue
            seen.add(qid)
            questions.append({
                "id": qid, "family": q["family"], "label_fr": q.get("label_fr") or q["family"],
                "expected": q["resolution"]["expected_date"],
                "status": (oc_all.get(qid) or {}).get("status", "pending"), "officials": set(refs.get(qid, ())),
            })  # fmt: skip
    return {
        "registry": "forecast",
        "batches": len(batches),
        "original_batches": sum(1 for b in batches if not b.get("supplement_of")),
        "supplement_batches": sum(1 for b in batches if b.get("supplement_of")),
        "forecasts": n_forecasts,
        "questions": questions,
        "last_sealed_at": max((str(b["issued_at"]) for b in batches if b.get("issued_at")), default=None),
    }


def _health_registry(conn: Any, store: Any, origin: str | None) -> dict[str, Any]:
    from reva.evaluation.ledger import load_frozen_manifest

    oc = " AND task_scope->>'origin' = :o" if origin else ""
    shas = [
        r[0]
        for r in conn.execute(
            text(
                f"""SELECT evaluation_manifest_sha256, min(recorded_at) FROM model.evaluation
                    WHERE metrics->>'kind' = 'health_issue' AND task_scope->>'kind' = 'forecast_registry_health'{oc}
                    GROUP BY 1 ORDER BY 2"""
            ),
            {"o": origin} if origin else {},
        )
    ]
    handled: dict[str, str] = {}
    for r in conn.execute(
        text(
            f"""SELECT task_scope->'question_ids' AS q, metrics->'outcomes' AS o FROM model.evaluation
                WHERE metrics->>'kind' = 'health_score' AND task_scope->>'kind' = 'forecast_registry_health'{oc}"""
        ),
        {"o": origin} if origin else {},
    ).mappings():
        outcomes = r["o"] if isinstance(r["o"], list) else []
        st = {o.get("question_id"): o.get("status") for o in outcomes if isinstance(o, Mapping)}
        for qid in r["q"] or []:
            handled[qid] = st.get(qid) or "resolved"
    questions: list[dict[str, Any]] = []
    n_forecasts, issued_at = 0, []
    for sha in shas:
        m = load_frozen_manifest(store, sha)
        issued_at.append(str(m.get("issued_at") or ""))
        refs: dict[str, set[str]] = defaultdict(set)
        for f in m.get("forecasts", []):
            if f.get("status") == "issued":
                n_forecasts += 1
                fid = OFFICIAL_REFERENCES.get(_base(f.get("model")))
                if fid:
                    refs[f["question_id"]].add(fid)
        for q in m.get("questions", []):
            questions.append({
                "id": q["question_id"], "family": q["family"], "label_fr": q.get("label_fr") or q["family"],
                "expected": q["resolution"].get("expected_after") or q.get("period_end"),
                "status": handled.get(q["question_id"], "pending"), "officials": refs.get(q["question_id"], set()),
            })  # fmt: skip
    return {
        "registry": "health",
        "batches": len(shas),
        "original_batches": len(shas),
        "supplement_batches": 0,
        "forecasts": n_forecasts,
        "questions": questions,
        "last_sealed_at": max((x for x in issued_at if x), default=None),
    }


def _summarise(reg: dict[str, Any], today: date) -> dict[str, Any]:
    qs = reg.pop("questions")
    st = Counter(q["status"] for q in qs)
    pending = [q for q in qs if q["status"] == "pending"]
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for q in pending:
        k = (str(q["expected"]), q["family"])
        e = by.setdefault(k, {"date": k[0], "family": q["family"], "label_fr": q["label_fr"], "n_questions": 0,
                              "officials": set()})  # fmt: skip
        e["n_questions"] += 1
        e["officials"] |= q["officials"]
    upcoming = sorted(by.values(), key=lambda e: (e["date"], e["family"]))
    for e in upcoming:
        e["officials"] = sorted(e["officials"])
        e["registry"] = reg["registry"]
        e["overdue"] = e["date"] < today.isoformat()
    return {
        **reg,
        "questions": len(qs),
        "status": {k: st.get(k, 0) for k in ("pending", "resolved", "void", "not_prospective")},
        "families": len({q["family"] for q in qs}),
        "upcoming": upcoming,
    }


def _ensemble_summary(conn: Any, store: Any, origin: str | None) -> dict[str, Any] | None:
    try:
        from reva.evaluation.registry.ensemble import scoreboard

        b = scoreboard(conn, store, origin)
    except Exception as e:  # another agent's module: never break this page
        return {"available": False, "reason": f"{type(e).__name__}: {e}"[:200]}
    cells = b.get("cells", [])
    by_official: dict[str, Counter[str]] = defaultdict(Counter)
    for c in cells:
        comps = {_base(x.get("model")) for x in c.get("competitors", [])}
        for ref, fid in OFFICIAL_REFERENCES.items():
            if ref in comps:
                by_official[fid][c.get("verdict", "unknown")] += 1
    return {
        "available": True,
        "cells": len(cells),
        "verdicts": dict(Counter(c.get("verdict", "unknown") for c in cells)),
        "holm_family_size": b.get("holm_family_size"),
        "n_ensemble_forecasts": b.get("n_ensemble_forecasts"),
        "n_ensemble_scored": b.get("n_ensemble_scored"),
        "by_official": {k: dict(v) for k, v in sorted(by_official.items())},
        "claims_fr": [c["claim_fr"] for c in cells if c.get("claim_fr")],
    }


def compute_live(conn: Any, store: Any, origin: str | None, *, today: date | None = None) -> dict[str, Any]:
    """Registry counts, upcoming resolutions and the ensemble summary (reads every sealed manifest: seconds)."""
    t0 = time.perf_counter()
    d = today or datetime.now(UTC).date()
    regs = [_summarise(_main_registry(conn, store, origin), d), _summarise(_health_registry(conn, store, origin), d)]
    upcoming = sorted((u for r in regs for u in r["upcoming"]), key=lambda e: (e["date"], e["registry"], e["family"]))
    for r in regs:
        r.pop("upcoming")
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "compute_ms": round((time.perf_counter() - t0) * 1000, 1),
        "origin": origin,
        "registries": regs,
        "totals": {
            "sealed_batches": sum(r["batches"] for r in regs),
            "questions": sum(r["questions"] for r in regs),
            "forecasts": sum(r["forecasts"] for r in regs),
            **{k: sum(r["status"][k] for r in regs) for k in ("pending", "resolved", "void", "not_prospective")},
        },
        "next_resolution": next((u["date"] for u in upcoming if not u["overdue"]), None),
        "overdue_groups": sum(1 for u in upcoming if u["overdue"]),
        "upcoming": upcoming[:200],
        "upcoming_total": len(upcoming),
        "ensemble": _ensemble_summary(conn, store, origin),
        "refreshing": False,
    }


@contextmanager
def _ro(engine: Any) -> Iterator[Any]:
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            yield conn


class LiveService:
    """Per-origin cache with stale-while-revalidate: a changed registry is recomputed in a background thread
    while the previous state is served (marked ``refreshing``); only a cold cache blocks."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._d: dict[str | None, tuple[tuple[Any, ...], float, dict[str, Any]]] = {}
        self._running: set[str | None] = set()

    def _recompute(self, engine: Any, store: Any, origin: str | None) -> dict[str, Any]:
        with _ro(engine) as conn:
            fp = fingerprint(conn, origin)
            val = compute_live(conn, store, origin)
        with self._lock:
            self._d[origin] = (fp, time.monotonic(), val)
        return val

    def _background(self, engine: Any, store: Any, origin: str | None) -> None:
        with self._lock:
            if origin in self._running:
                return
            self._running.add(origin)

        def run() -> None:
            try:
                self._recompute(engine, store, origin)
            except Exception:  # keep serving the previous state; next request retries
                LOG.warning("v8 scores: live registry refresh failed", exc_info=True)
            finally:
                with self._lock:
                    self._running.discard(origin)

        Thread(target=run, name=f"v8-scores-live-{origin}", daemon=True).start()

    def get(self, engine: Any, store: Any, origin: str | None, *, wait: bool = True) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(origin)
        if hit and now - hit[1] < FINGERPRINT_TTL_S:
            return hit[2]
        if hit:
            with _ro(engine) as conn:
                fp = fingerprint(conn, origin)
            if fp == hit[0]:
                with self._lock:
                    self._d[origin] = (fp, now, hit[2])
                return hit[2]
            self._background(engine, store, origin)
            return {**hit[2], "refreshing": True}
        if not wait:
            self._background(engine, store, origin)
            return None
        return self._recompute(engine, store, origin)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


SERVICE = LiveService()
