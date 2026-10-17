"""Read side of the forecast registry (API): batches, questions, frozen forecasts, outcomes, leaderboard,
calibration, back-tests, families. Every manifest read is verified against its sha256 (sealed content)."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from threading import Lock
from typing import Any

from sqlalchemy import text

from reva.evaluation.ledger import load_frozen_manifest
from reva.evaluation.registry.forecasts import all_models
from reva.evaluation.registry.scores import aggregate, calibration, relative_to
from reva.evaluation.registry.service import (
    BACKTEST_KIND,
    BACKTEST_LABEL,
    BASELINE_KEY,
    ISSUE_KIND,
    LABEL,
    SCOPE_KIND,
    SCORE_KIND,
)
from reva.evaluation.registry.specs import load_specs

STATUSES = ("pending", "resolved", "void", "not_prospective")


class ManifestCache:
    """Verified sealed manifests by sha256 (immutable content: safe to cache)."""

    def __init__(self, size: int = 16) -> None:
        self.size = size
        self._d: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = Lock()

    def get(self, store: Any, sha: str) -> dict[str, Any]:
        with self._lock:
            if sha in self._d:
                self._d.move_to_end(sha)
                return self._d[sha]
        m = load_frozen_manifest(store, sha)
        with self._lock:
            self._d[sha] = m
            while len(self._d) > self.size:
                self._d.popitem(last=False)
        return m


CACHE = ManifestCache()


def _origin_clause(origin: str | None) -> tuple[str, dict[str, Any]]:
    if origin is None:
        return "", {}
    return " AND task_scope->>'origin' = :o", {"o": origin}


def batches(conn: Any, origin: str | None) -> list[dict[str, Any]]:
    oc, op = _origin_clause(origin)
    rows = conn.execute(
        text(
            f"""SELECT evaluation_manifest_sha256 AS sha, context_id::text AS context_id, task_scope, metrics,
                       recorded_at
                FROM model.evaluation WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk{oc}
                ORDER BY recorded_at DESC"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, **op},
    ).mappings()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        m, ts = r["metrics"], r["task_scope"]
        b = out.setdefault(
            r["sha"],
            {
                "batch_sha256": r["sha"],
                "context_id": r["context_id"],
                "cycle": ts.get("cycle"),
                "origin": ts.get("origin"),
                "issued_at": m.get("issued_at"),
                "recorded_at": r["recorded_at"],
                "n_questions": m.get("n_questions"),
                "by_type": m.get("by_type") or {},
                "label": m.get("label"),
                "models": [],
            },
        )
        b["models"].append(
            {"model": m.get("model"), "n_issued": m.get("n_issued"), "n_abstained": m.get("n_abstained")}
        )
    for b in out.values():
        b["models"].sort(key=lambda x: str(x["model"]))
    return sorted(out.values(), key=lambda b: b["recorded_at"], reverse=True)


def _score_rows(conn: Any, origin: str | None, batch_sha: str | None = None) -> list[dict[str, Any]]:
    oc, op = _origin_clause(origin)
    bc = " AND task_scope->>'batch_sha256' = :b" if batch_sha else ""
    rows = conn.execute(
        text(
            f"""SELECT e.task_scope, e.metrics, v.model_key, e.recorded_at FROM model.evaluation e
                JOIN model.version v ON v.id = e.model_version_id
                WHERE e.metrics->>'kind' = :k AND e.task_scope->>'kind' = :sk{oc}{bc} ORDER BY e.recorded_at"""
        ),
        {"k": SCORE_KIND, "sk": SCOPE_KIND, **op, **({"b": batch_sha} if batch_sha else {})},
    ).mappings()
    return [dict(r) for r in rows]


def outcomes(conn: Any, store: Any, origin: str | None, batch_sha: str | None = None) -> dict[str, dict[str, Any]]:
    """question_id -> outcome (status, value, source) from the resolution manifests."""
    shas = sorted({r["metrics"].get("resolution_sha256") for r in _score_rows(conn, origin, batch_sha)} - {None})
    out: dict[str, dict[str, Any]] = {}
    for sha in shas:
        doc = CACHE.get(store, sha)
        for o in doc.get("outcomes", []):
            out[o["question_id"]] = {**o, "resolution_sha256": sha, "resolved_at": doc.get("resolved_at")}
    return out


def _summary(q: Mapping[str, Any], b: Mapping[str, Any], n_fc: int, oc: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "question_id": q["question_id"],
        "question_key": q["question_key"],
        "cycle": q["cycle"],
        "family": q["family"],
        "concept_id": q.get("concept_id"),
        "label_fr": q["label_fr"],
        "label_en": q["label_en"],
        "geo": q["geo"],
        "geo_label_fr": q["geo_label_fr"],
        "geo_label_en": q["geo_label_en"],
        "reference_period": q["reference_period"],
        "type": q["type"],
        "unit": q["unit"],
        "threshold": q.get("threshold"),
        "text_fr": q["text_fr"],
        "text_en": q["text_en"],
        "expected_resolution": q["resolution"]["expected_date"],
        "void_after": q["resolution"]["void_after"],
        "resolution_source": q["resolution"]["source"],
        "status": (oc or {}).get("status", "pending"),
        "outcome": (oc or {}).get("value"),
        "n_forecasts": n_fc,
        "batch_sha256": b["batch_sha256"],
        "issued_at": b["issued_at"],
    }


def questions(
    conn: Any,
    store: Any,
    origin: str | None,
    *,
    family: str | None = None,
    geo: str | None = None,
    qtype: str | None = None,
    status: str | None = None,
    cycle: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    oc_all = outcomes(conn, store, origin)
    listed: set[str] = set()  # a supplementary batch (V6 PRED-MODELS) repeats sealed questions: list each once
    for b in batches(conn, origin):
        if cycle and b["cycle"] != cycle:
            continue
        m = CACHE.get(store, b["batch_sha256"])
        n_fc: dict[str, int] = defaultdict(int)
        for f in m["forecasts"]:
            if f["status"] == "issued":
                n_fc[f["question_id"]] += 1
        for q in m["questions"]:
            if family and q["family"] != family:
                continue
            if geo and q["geo"] != geo:
                continue
            if qtype and q["type"] != qtype:
                continue
            if q["question_id"] in listed:
                continue
            listed.add(q["question_id"])
            s = _summary(q, b, n_fc[q["question_id"]], oc_all.get(q["question_id"]))
            if status and s["status"] != status:
                continue
            out.append(s)
    return out[offset : offset + limit], len(out)


def question(conn: Any, store: Any, question_id: str) -> dict[str, Any] | None:
    for b in batches(conn, None):
        m = CACHE.get(store, b["batch_sha256"])
        q = next((x for x in m["questions"] if x["question_id"] == question_id), None)
        if q is None:
            continue
        oc = outcomes(conn, store, None, b["batch_sha256"]).get(question_id)
        scores: dict[str, dict[str, Any]] = {}
        for r in _score_rows(conn, None, b["batch_sha256"]):
            for s in r["metrics"].get("scores", []):
                if s["question_id"] == question_id:
                    scores[s["model"]] = s
        fcs = [f for f in m["forecasts"] if f["question_id"] == question_id]
        for b2 in batches(conn, None):  # forecasts of supplementary batches (V6 PRED-MODELS) on the same question
            if b2["batch_sha256"] == b["batch_sha256"]:
                continue
            m2 = CACHE.get(store, b2["batch_sha256"])
            if m2.get("supplement_of") != b["batch_sha256"]:
                continue
            sc2 = {s["model"]: s for r in _score_rows(conn, None, b2["batch_sha256"])
                   for s in r["metrics"].get("scores", []) if s["question_id"] == question_id}  # fmt: skip
            scores.update(sc2)
            fcs += [{**f, "supplement_batch_sha256": b2["batch_sha256"]} for f in m2["forecasts"]
                    if f["question_id"] == question_id]  # fmt: skip
        return {
            "question": q,
            "forecasts": [{**f, "score": scores.get(f["model"])} for f in fcs],
            "outcome": oc,
            "status": (oc or {}).get("status", "pending"),
            "seal": {
                "batch_sha256": b["batch_sha256"],
                "issued_at": b["issued_at"],
                "cycle": b["cycle"],
                "origin": b["origin"],
                "context_id": b["context_id"],
                "verified": True,
                "label": m["label"],
                "data_tables": m["data"]["tables"],
                "code_digest": m.get("code_digest"),
            },
        }
    return None


def _scores_by_model(conn: Any, origin: str | None, family: str | None = None) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _score_rows(conn, origin):
        for s in r["metrics"].get("scores", []):
            if family and s.get("family") != family:
                continue
            by[r["model_key"]].append(s)
    return by


def leaderboard(conn: Any, store: Any, origin: str | None, family: str | None = None) -> dict[str, Any]:
    issued: dict[str, int] = defaultdict(int)
    abstained: dict[str, int] = defaultdict(int)
    for b in batches(conn, origin):
        for mm in b["models"]:
            key = str(mm["model"]).split("@", 1)[0]
            issued[key] += int(mm.get("n_issued") or 0)
            abstained[key] += int(mm.get("n_abstained") or 0)
    by = _scores_by_model(conn, origin, family)
    base = by.get(BASELINE_KEY, [])
    rows = []
    for mk, (ver, desc) in sorted(all_models().items()):
        sc = by.get(mk, [])
        types = sorted({s["type"] for s in sc})
        rows.append(
            {
                "model": mk,
                "version": ver,
                "description": desc,
                "kind": "external_reference" if mk.startswith("forecast.reference_") else "baseline",
                "n_issued": issued.get(mk, 0),
                "n_abstained": abstained.get(mk, 0),
                **aggregate(sc),
                "by_type": {t: aggregate([s for s in sc if s["type"] == t]) for t in types},
                "mae_vs_persistence": relative_to(sc, base, "abs_error"),
                "crps_vs_persistence": relative_to(sc, base, "crps"),
                "brier_vs_persistence": relative_to(sc, base, "brier"),
            }
        )
    counts: dict[str, int] = defaultdict(int)
    for o in outcomes(conn, store, origin).values():
        counts[o["status"]] += 1
    n_q = sum(int(b["n_questions"] or 0) for b in batches(conn, origin))
    counts["pending"] = n_q - sum(counts.values())
    return {"label": LABEL, "baseline_for_skill": BASELINE_KEY, "questions": dict(counts), "models": rows}


def calibration_of(conn: Any, origin: str | None, model: str, family: str | None = None) -> dict[str, Any]:
    sc = _scores_by_model(conn, origin, family).get(model, [])
    return {"model": model, "family": family, "n_scored": len(sc), **calibration(sc)}


def backtests(conn: Any, store: Any, origin: str | None, *, with_calibration: bool = False) -> list[dict[str, Any]]:
    oc, op = _origin_clause(origin)
    rows = conn.execute(
        text(
            f"""SELECT DISTINCT ON (evaluation_manifest_sha256) evaluation_manifest_sha256 AS sha, recorded_at,
                       task_scope
                FROM model.evaluation WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk{oc}
                ORDER BY evaluation_manifest_sha256, recorded_at"""
        ),
        {"k": BACKTEST_KIND, "sk": SCOPE_KIND, **op},
    ).mappings()
    out = []
    for r in sorted(rows, key=lambda x: x["recorded_at"], reverse=True):
        m = CACHE.get(store, r["sha"])
        fams = m["families"]
        if not with_calibration:
            fams = [
                {**f, "results": [{k: v for k, v in x.items() if k != "calibration"} for x in f["results"]]}
                for f in fams
            ]
        out.append(
            {
                "backtest_sha256": r["sha"],
                "label": m.get("label", BACKTEST_LABEL),
                "vintage_mode": m.get("vintage_mode"),
                "run_at": m.get("run_at"),
                "origin": m.get("origin"),
                "years": m.get("years"),
                "data_tables": m["data"]["tables"],
                "families": fams,
            }
        )
    return out


def families(conn: Any, origin: str | None) -> list[dict[str, Any]]:
    specs = load_specs()
    keys = sorted({f.dataset_key for f in specs.families if f.dataset_key})
    present: dict[str, Any] = {}
    if keys:
        q = """SELECT dataset_key, max(recorded_at) AS latest FROM evidence.dataset
               WHERE concept_id = 'atlas.table' AND dataset_key = ANY(:k)"""
        params: dict[str, Any] = {"k": keys}
        if origin:
            q += " AND origin = :o"
            params["o"] = origin
        for r in conn.execute(text(q + " GROUP BY dataset_key"), params).mappings():
            present[r["dataset_key"]] = r["latest"]
    out = []
    for f in specs.families:
        if f.dataset_key is None:
            st, why = "awaiting_source", f.awaiting
        elif f.dataset_key in present:
            st, why = "available", None
        else:
            st = "awaiting_table"
            why = f"table {f.dataset_key} not registered yet" + (f" (owner {f.owner_agent})" if f.owner_agent else "")
        out.append(
            {
                "key": f.key,
                "label_fr": f.label_fr,
                "label_en": f.label_en,
                "concept_id": f.concept_id,
                "definition": f.definition,
                "definition_source": f.definition_source,
                "dataset_key": f.dataset_key,
                "indicator": f.indicator or "|".join(f.indicator_parts) or None,
                "frequency": f.frequency,
                "value_rule": f.value_rule,
                "unit": f.unit,
                "question_types": sorted({q.type for q in f.questions}),
                "horizons": sorted({h for q in f.questions for h in q.horizons}),
                "countries": list(f.countries) or [g.key for g in f.geos],
                "reference_forecasters": list(f.reference_forecasters),
                "status": st,
                "status_reason": why,
                "table_latest_recorded_at": present.get(f.dataset_key) if f.dataset_key else None,
                "resolution_note": f.resolution_note,
            }
        )
    return out


__all__ = [
    "CACHE",
    "STATUSES",
    "backtests",
    "batches",
    "calibration_of",
    "families",
    "leaderboard",
    "outcomes",
    "question",
    "questions",
]
