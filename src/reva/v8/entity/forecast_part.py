"""Forecast questions about the entity (by geography or by question family) from the sealed registry: each
question with every sealed forecast (model, kind, point, interval), the official forecaster's attributed forecast
when one was sealed, its status, outcome and scores once resolved. A pending question is never presented as
judged; a REVA candidate is never presented as better than the official forecaster before resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    if d.startswith("candidate") or "candidate" in d[:40] or model.startswith("forecast.cm_") or model.startswith("forecast.weo_"):
        return "candidate" if "benchmark" not in d[:20] else "baseline"
    return "baseline"


def model_label(model: str) -> str:
    if model in OFFICIAL:
        return f"{OFFICIAL[model]} — attribué"
    short = model.split(".", 1)[1] if "." in model else model
    return short.replace("registry_", "").replace("_", " ")


KIND_FR = {"official": "officiel (attribué)", "ensemble": "ensemble REVA", "candidate": "candidat REVA", "baseline": "référence naïve"}


def _qband(f: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    q = f.get("quantiles") or {}
    lo = q.get("0.05") or q.get("0.1")
    hi = q.get("0.95") or q.get("0.9")
    lev = "90 %" if q.get("0.05") else ("80 %" if q.get("0.1") else None)
    return (None if lo is None else str(lo)), (None if hi is None else str(hi)), lev


def forecasts(
    conn: Connection,
    store: Any,
    origin: str | None,
    *,
    geo: str | None = None,
    families: Iterable[str] = (),
    limit: int = MAX_QUESTIONS,
) -> dict[str, Any]:
    from reva.evaluation.registry import read as R
    from reva.evaluation.registry.forecasts import all_models

    fams = set(families)
    if not geo and not fams:
        return {"questions": [], "n_total": 0, "n_pending": 0, "n_resolved": 0, "families": [], "note_fr": NOTE_FR}
    models = all_models()
    bs = R.batches(conn, origin)
    oc = R.outcomes(conn, store, origin)
    qs: dict[str, dict[str, Any]] = {}
    fcs: dict[str, list[dict[str, Any]]] = {}
    manifests = [(b, R.CACHE.get(store, b["batch_sha256"])) for b in bs]
    for b, m in manifests:
        for q in m["questions"]:
            if geo and q.get("geo") != geo and q.get("iso3") != geo:
                continue
            if fams and not geo and q.get("family") not in fams:
                continue
            qs.setdefault(q["question_id"], {"q": q, "batch": b})
    for b, m in manifests:
        for f in m["forecasts"]:
            if f.get("question_id") in qs:
                fcs.setdefault(f["question_id"], []).append({**f, "_batch": b["batch_sha256"], "_issued_at": b["issued_at"]})
    scores: dict[str, dict[str, Any]] = {}
    if any(oc.get(q) for q in qs):
        for r in R._score_rows(conn, origin):
            for s in (r.get("metrics") or {}).get("scores", []):
                if s.get("question_id") in qs:
                    scores.setdefault(s["question_id"], {})[s["model"]] = s
    out = []
    for qid, e in qs.items():
        q = e["q"]
        o = oc.get(qid) or {}
        status = o.get("status", "pending")
        rows = []
        seen_models: set[str] = set()
        for f in fcs.get(qid, []):
            mdl = str(f.get("model"))
            if mdl in seen_models:
                continue
            seen_models.add(mdl)
            desc = (models.get(mdl) or ("", ""))[1]
            kind = model_kind(mdl, desc)
            lo, hi, lev = _qband(f)
            sc = (scores.get(qid) or {}).get(mdl)
            rows.append(
                {
                    "model": mdl,
                    "label": model_label(mdl),
                    "kind": kind,
                    "kind_fr": KIND_FR[kind],
                    "description": desc[:300] if desc else None,
                    "status": f.get("status"),
                    "point": None if f.get("point") is None else str(f["point"]),
                    "probability": None if f.get("probability") is None else str(f["probability"]),
                    "lo": lo,
                    "hi": hi,
                    "level": lev,
                    "issued_at": f.get("_issued_at"),
                    "batch_sha256": f.get("_batch"),
                    "score": {k: (None if v is None else str(v)) for k, v in sc.items() if k in ("crps", "abs_error", "ae", "brier", "log_score")} if sc else None,
                }
            )
        order = {"official": 0, "ensemble": 1, "candidate": 2, "baseline": 3}
        rows.sort(key=lambda r: (order.get(r["kind"], 9), r["model"]))
        res = q.get("resolution") or {}
        out.append(
            {
                "question_id": qid,
                "family": q.get("family"),
                "label_fr": q.get("label_fr") or q.get("label_en"),
                "text_fr": q.get("text_fr"),
                "geo": q.get("geo"),
                "geo_label_fr": q.get("geo_label_fr"),
                "reference_period": q.get("reference_period"),
                "type": q.get("type"),
                "unit": q.get("unit"),
                "threshold": None if q.get("threshold") is None else str(q.get("threshold")),
                "last_observed": q.get("last_observed"),
                "status": status,
                "outcome": None if o.get("value") is None else str(o.get("value")),
                "expected_resolution": res.get("expected_date"),
                "resolution_source": res.get("source"),
                "forecasts": rows,
                "official_present": any(r["kind"] == "official" for r in rows),
                "judged_fr": judged_fr(status, rows, res.get("expected_date")),
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


def judged_fr(status: str, rows: list[Mapping[str, Any]], expected: str | None) -> str:
    if status == "pending":
        return f"en attente de résolution (attendue le {expected}) — pas encore jugée" if expected else "en attente de résolution — pas encore jugée"
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
