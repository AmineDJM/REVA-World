"""Forecast questions about the entity (by geography or by question family) from the sealed registry: each
question with every sealed forecast (model, kind, point, interval), the official forecaster's attributed forecast
when one was sealed, its status, outcome and scores once resolved. A pending question is never presented as
judged; a REVA candidate is never presented as better than the official forecaster before resolution."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Connection

MAX_QUESTIONS = 24
OFFICIAL = {
    "forecast.reference_imf_weo": "FMI (WEO)",
    "forecast.reference_eia_steo": "EIA (STEO)",
    "forecast.reference_views": "VIEWS",
}
NOTE_FR = (
    "Prévisions scellées avant résolution dans le registre prospectif REVA. Une question en attente n’est pas "
    "jugée ; une prévision officielle (FMI, EIA, VIEWS) est attribuée à son auteur. Les références naïves "
    "(persistance, AR…) servent d’étalon, pas de prévision REVA."
)


def model_kind(model: str, description: str) -> str:
    d = description.lower()
    if model in OFFICIAL or "external_reference" in d or d.startswith("reference:"):
        return "official"
    if model == "reva.ensemble":
        return "ensemble"
    if (
        d.startswith("candidate")
        or "candidate" in d[:40]
        or model.startswith("forecast.cm_")
        or model.startswith("forecast.weo_")
    ):
        return "candidate" if "benchmark" not in d[:20] else "baseline"
    return "baseline"


def model_label(model: str) -> str:
    if model in OFFICIAL:
        return f"{OFFICIAL[model]} — attribué"
    short = model.split(".", 1)[1] if "." in model else model
    return short.replace("registry_", "").replace("_", " ")


KIND_FR = {
    "official": "officiel (attribué)",
    "ensemble": "ensemble REVA",
    "candidate": "candidat REVA",
    "baseline": "référence naïve",
}


def _qband(f: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    q = f.get("quantiles") or {}
    lo = q.get("0.05") or q.get("0.1")
    hi = q.get("0.95") or q.get("0.9")
    lev = "90 %" if q.get("0.05") else ("80 %" if q.get("0.1") else None)
    return (None if lo is None else str(lo)), (None if hi is None else str(hi)), lev


class IndexWarming(Exception):
    """The registry index of this process is being built (first access): the section says so, never 'none'."""


_IDX_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()
#: per sealed batch (immutable content): compact questions and forecast rows
_BATCH: dict[str, dict[str, Any]] = {}
_STATE: dict[str, Any] = {"thread": None, "error": None}


def _compact_batch(store: Any, sha: str, issued_at: Any) -> dict[str, Any]:
    from reva.evaluation.ledger import load_frozen_manifest

    m = load_frozen_manifest(store, sha)
    qs = {}
    for q in m["questions"]:
        res = q.get("resolution") or {}
        qs[q["question_id"]] = {
            "question_id": q["question_id"],
            "family": q.get("family"),
            "label_fr": q.get("label_fr") or q.get("label_en"),
            "text_fr": q.get("text_fr"),
            "geo": q.get("geo") or q.get("iso3"),
            "geo_label_fr": q.get("geo_label_fr"),
            "reference_period": q.get("reference_period"),
            "type": q.get("type"),
            "unit": q.get("unit"),
            "threshold": None if q.get("threshold") is None else str(q.get("threshold")),
            "last_observed": q.get("last_observed"),
            "expected_resolution": res.get("expected_date"),
            "resolution_source": res.get("source"),
        }
    rows = []
    for f in m["forecasts"]:
        lo, hi, lev = _qband(f)
        rows.append(
            {
                "question_id": f.get("question_id"),
                "model": str(f.get("model")),
                "status": f.get("status"),
                "point": None if f.get("point") is None else str(f["point"]),
                "probability": None if f.get("probability") is None else str(f["probability"]),
                "lo": lo,
                "hi": hi,
                "level": lev,
                "issued_at": None if issued_at is None else str(issued_at),
                "batch_sha256": sha,
            }
        )
    return {"questions": qs, "forecasts": rows}


def _ensure_index(conn: Connection, store: Any, origin: str | None, wait_s: float) -> list[dict[str, Any]]:
    """Batches of the registry, each compacted once per process; the first build runs in a thread and the caller
    waits at most ``wait_s`` (then :class:`IndexWarming`)."""
    from reva.evaluation.registry import read as R

    bs = R.batches(conn, origin)
    todo = [b for b in bs if b["batch_sha256"] not in _BATCH]
    if not todo:
        return bs

    def run() -> None:
        try:
            with _BUILD_LOCK:
                for b in todo:
                    if b["batch_sha256"] not in _BATCH:
                        data = _compact_batch(store, b["batch_sha256"], b.get("issued_at"))
                        with _IDX_LOCK:
                            _BATCH[b["batch_sha256"]] = data
            _STATE["error"] = None
        except Exception as e:  # pragma: no cover - reported to the next caller
            _STATE["error"] = f"{type(e).__name__}: {e}"[:300]

    with _IDX_LOCK:
        th = _STATE["thread"]
        if th is None or not th.is_alive():
            th = threading.Thread(target=run, name="v8fiche-forecast-index", daemon=True)
            _STATE["thread"] = th
            th.start()
    th.join(timeout=wait_s)
    if any(b["batch_sha256"] not in _BATCH for b in bs):
        if _STATE["error"]:
            raise RuntimeError(_STATE["error"])
        raise IndexWarming(f"{sum(b['batch_sha256'] not in _BATCH for b in bs)} sealed batches still being indexed")
    return bs


def warm(conn: Connection, store: Any, origin: str | None = None) -> None:
    try:
        _ensure_index(conn, store, origin, 0.0)
    except IndexWarming:
        pass


def forecasts(
    conn: Connection,
    store: Any,
    origin: str | None,
    *,
    geo: str | None = None,
    families: Iterable[str] = (),
    question_id: str | None = None,
    limit: int = MAX_QUESTIONS,
    wait_s: float = 2.0,
) -> dict[str, Any]:
    from reva.evaluation.registry import read as R
    from reva.evaluation.registry.forecasts import all_models

    fams = set(families)
    if not geo and not fams and not question_id:
        return {"questions": [], "n_total": 0, "n_pending": 0, "n_resolved": 0, "families": [], "note_fr": NOTE_FR}
    bs = _ensure_index(conn, store, origin, wait_s)
    models = all_models()
    qs: dict[str, dict[str, Any]] = {}
    for b in bs:
        for qid, q in _BATCH[b["batch_sha256"]]["questions"].items():
            if question_id and qid != question_id:
                continue
            if geo and q.get("geo") != geo:
                continue
            if fams and not geo and q.get("family") not in fams:
                continue
            qs.setdefault(qid, q)
    fcs: dict[str, list[dict[str, Any]]] = {}
    for b in bs:
        for f in _BATCH[b["batch_sha256"]]["forecasts"]:
            if f["question_id"] in qs:
                fcs.setdefault(f["question_id"], []).append(f)
    oc = R.outcomes(conn, store, origin) if qs else {}
    scores: dict[str, dict[str, Any]] = {}
    if any(oc.get(q) for q in qs):
        for r in R._score_rows(conn, origin):
            for sc in (r.get("metrics") or {}).get("scores", []):
                if sc.get("question_id") in qs:
                    scores.setdefault(sc["question_id"], {})[sc["model"]] = sc
    out = []
    for qid, q in qs.items():
        o = oc.get(qid) or {}
        status = o.get("status", "pending")
        rows = []
        seen_models: set[str] = set()
        for f in fcs.get(qid, []):
            mdl = f["model"]
            if mdl in seen_models:
                continue
            seen_models.add(mdl)
            desc = (models.get(mdl) or ("", ""))[1]
            kind = model_kind(mdl, desc)
            sc = (scores.get(qid) or {}).get(mdl)
            rows.append(
                {
                    **{k: v for k, v in f.items() if k != "question_id"},
                    "label": model_label(mdl),
                    "kind": kind,
                    "kind_fr": KIND_FR[kind],
                    "description": desc[:240] if desc else None,
                    "score": {
                        k: (None if v is None else str(v))
                        for k, v in sc.items()
                        if k in ("crps", "abs_error", "ae", "brier", "log_score")
                    }
                    if sc
                    else None,
                }
            )
        order = {"official": 0, "ensemble": 1, "candidate": 2, "baseline": 3}
        rows.sort(key=lambda r: (order.get(r["kind"], 9), r["model"]))
        out.append(
            {
                **q,
                "status": status,
                "outcome": None if o.get("value") is None else str(o.get("value")),
                "forecasts": rows,
                "official_present": any(r["kind"] == "official" for r in rows),
                "judged_fr": judged_fr(status, rows, q.get("expected_resolution")),
            }
        )
    out.sort(key=lambda x: (x["status"] != "resolved", str(x["expected_resolution"] or ""), str(x["family"])))
    n_res = sum(1 for x in out if x["status"] == "resolved")
    n_pend = sum(1 for x in out if x["status"] == "pending")
    return {
        "questions": out[:limit],
        "n_total": len(out),
        "n_pending": n_pend,
        "n_resolved": n_res,
        "families": sorted({str(x["family"]) for x in out}),
        "note_fr": NOTE_FR,
    }


def judged_fr(status: str, rows: Sequence[Mapping[str, Any]], expected: str | None) -> str:
    if status == "pending":
        return (
            f"en attente de résolution (attendue le {expected}) — pas encore jugée"
            if expected
            else "en attente de résolution — pas encore jugée"
        )
    if status == "void":
        return "annulée (la source de résolution n’a pas publié la valeur à temps)"
    if status == "not_prospective":
        return "non prospective — exclue du jugement"
    off = next((r for r in rows if r["kind"] == "official" and r.get("score")), None)
    reva = [r for r in rows if r["kind"] in ("candidate", "ensemble") and r.get("score")]
    if off is None or not reva:
        return "résolue — pas de comparaison avec un prévisionniste officiel scellé"
    metric = next((k for k in ("crps", "abs_error", "ae", "brier") if (off["score"] or {}).get(k) is not None), None)
    if metric is None:
        return "résolue — scores non comparables"
    try:
        o = float(off["score"][metric])
        best = min(float(r["score"][metric]) for r in reva if r["score"].get(metric) is not None)
    except (TypeError, ValueError):
        return "résolue — scores non comparables"
    who = off["label"].split(" — ")[0]
    return f"résolue — {'bat' if best < o else 'ne bat pas'} {who} sur cette question ({metric}, une question ne prouve rien)"
