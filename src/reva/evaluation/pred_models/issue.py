"""Registered PRED-MODELS forecasters in the prospective registry (V6, agent PRED-MODELS).

Only the scopes confirmed on the locked holdout (``registry/forecasts/pred_models/registered_models.yaml``) issue
forecasts, and only for registry question families that ask exactly that variable at that horizon
(``FAMILY_OF``). Scopes without such a family (e.g. the US SPF current-quarter density) are listed as
``awaiting_family`` and issue nothing.

Questions of the current cycle that were already sealed before a model was registered receive a **supplementary
sealed batch** (``supplement_of`` = the original batch): the same question records and the same pinned table
versions (identical information set), new forecasts only, sealed by the sha256 of the new manifest at the new
``issued_at``. Resolution and scoring then run unchanged on that batch (``registry.service.resolve_due``).

Inputs at issue: the WEO projection and history of the pinned ``atlas.imf.weo`` version (what the registry pins),
and the normalised out-of-sample WEO errors of the vintage backtest whose outcome was published at ``issued_at``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from typing import Any

import polars as pl
import yaml

from reva.evaluation.pred_models import realtime as R
from reva.evaluation.pred_models.derived import MAX_ERR_SAMPLE
from reva.evaluation.pred_models.service import MODEL_VERSION, REGISTRY_DIR
from reva.models.forecasting import combination as CB
from reva.models.forecasting.registry_baselines import Abstain, Predictive, final_run

#: (task, variable, horizon) -> (registry family, cycle horizon)
FAMILY_OF: dict[tuple[str, str, str], tuple[str, int]] = {
    ("weo", "real_gdp_growth", "h0"): ("weo.gdp_growth", 0),
    ("weo", "real_gdp_growth", "h1"): ("weo.gdp_growth", 1),
    ("weo", "cpi_inflation", "h0"): ("weo.cpi_inflation", 0),
    ("weo", "cpi_inflation", "h1"): ("weo.cpi_inflation", 1),
    ("weo", "unemployment_rate", "h0"): ("weo.unemployment", 0),
    ("weo", "unemployment_rate", "h1"): ("weo.unemployment", 1),
}
ISSUERS = {"forecast.weo_conformal", "forecast.weo_bias_corrected"}
#: FIX7 (QA-PRED X1): the fair benchmark, sealed next to the registered WEO densities on the same questions
FAIR_BENCHMARK = "forecast.weo_error_fan"
CLAIM = (
    "calibrated interval around the IMF WEO point - NOT a better point forecast (the point is the IMF's, attributed); "
    "its holdout win was against a weak benchmark (N(WEO point, persistence spread), 80 % interval covering 93-100 % "
    "of outcomes); on probation: issuance continues only if it beats the fair error-fan benchmark "
    "(forecast.weo_error_fan) on the next prospective evaluation"
)
DESCRIPTIONS = {
    "forecast.weo_conformal": "Calibrated interval around the IMF WEO point (WEO projection + scaled past "
    "out-of-sample WEO errors) - not a better point forecast. " + CLAIM,
    "forecast.weo_bias_corrected": "Calibrated interval around the IMF WEO point (WEO + median past WEO error, "
    "centred conformal density; its point is not better than the WEO's: holdout AE ratio 0.999) - not a better "
    "point forecast. " + CLAIM,
    FAIR_BENCHMARK: "BENCHMARK (FIX7): N(IMF WEO point, RMS of the country's own past WEO errors at this horizon whose "
    "outcome was published at issue) - the standard error-based fan, the fair benchmark for densities around the "
    "WEO point; not a REVA forecast",
}
DECISIONS_FILE = "issuance_decisions.yaml"
GATE_ALPHA = 0.05
GATE_MIN_QUESTIONS = 30


@lru_cache(maxsize=1)
def registered() -> tuple[dict[str, Any], ...]:
    path = REGISTRY_DIR / "registered_models.yaml"
    if not path.exists():
        return ()
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return tuple(dict(r) for r in doc.get("registered") or [])


def issuance_decisions() -> dict[str, dict[str, Any]]:
    """Latest prospective-gate decision per model (append-only ``issuance_decisions.yaml``; FIX7)."""
    path = REGISTRY_DIR / DECISIONS_FILE
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out: dict[str, dict[str, Any]] = {}
    for d in doc.get("decisions") or []:
        out[str(d["model"])] = dict(d)
    return out


def issuance_status(model_key: str) -> str:
    """``probation`` (registered, not yet tested against the fair benchmark prospectively), ``confirmed`` or
    ``suspended`` (lost to the fair benchmark: issues nothing)."""
    return str(issuance_decisions().get(model_key, {}).get("status") or "probation")


def issuing_scopes() -> dict[str, list[tuple[str, int]]]:
    """model_key -> [(registry family, cycle horizon)] for registered scopes that map to a question family; the
    fair benchmark issues on the union of the issuing densities' scopes (FIX7)."""
    out: dict[str, list[tuple[str, int]]] = {}
    for r in registered():
        fam = FAMILY_OF.get((r["task"], r["variable"], r["horizon"]))
        if fam and r["candidate"] in ISSUERS and issuance_status(r["candidate"]) != "suspended":
            out.setdefault(r["candidate"], []).append(fam)
    if out:
        out[FAIR_BENCHMARK] = sorted({f for fams in out.values() for f in fams})
    return out


def fair_gate(pairs: Sequence[tuple[str, float, float]], *, alpha: float = GATE_ALPHA) -> dict[str, Any]:
    """Prospective gate (pure). ``pairs``: (country, candidate CRPS, fair-benchmark CRPS) on the resolved questions
    both issued. ``pending`` below ``GATE_MIN_QUESTIONS`` pairs; ``confirmed`` iff the mean CRPS difference is
    negative with a one-sided country-clustered DM p < alpha; otherwise ``suspended``."""
    from reva.evaluation.conflict.stats import dm_test

    if len(pairs) < GATE_MIN_QUESTIONS:
        return {"status": "pending", "n": len(pairs), "reason": f"fewer than {GATE_MIN_QUESTIONS} resolved pairs"}
    t = dm_test([a - b for _g, a, b in pairs], [g for g, _a, _b in pairs])
    ok = t["mean_diff"] is not None and t["mean_diff"] < 0 and t["p_one_sided"] is not None and t["p_one_sided"] < alpha
    return {"status": "confirmed" if ok else "suspended", **t}


def registry_models() -> dict[str, tuple[str, str]]:
    """Registered models that issue in the registry: key -> (version, description)."""
    return {k: (MODEL_VERSION, DESCRIPTIONS[k]) for k in sorted(issuing_scopes())}


def awaiting_family() -> list[dict[str, Any]]:
    return [r for r in registered() if (r["task"], r["variable"], r["horizon"]) not in FAMILY_OF]


def weo_own_errors(cells: pl.DataFrame, variable: str, horizon: str, at: datetime) -> dict[str, list[float]]:
    """Raw WEO errors (outcome - WEO) per country, of the vintage backtest cases whose outcome was published at or
    before ``at`` (the fair benchmark's input; FIX7)."""
    origins = [t for t in R.default_weo_origins(2008, at.year) if t < at]
    out: dict[str, list[float]] = {}
    for c in R.weo_cases(cells, variable, origins):
        w = c.preds.get(R.WEO)
        if (
            c.horizon == horizon
            and isinstance(w, Predictive)
            and c.outcome is not None
            and c.outcome_known_at is not None
            and c.outcome_known_at <= at
        ):
            out.setdefault(c.geo, []).append(c.outcome - w.point)
    return out


def weo_error_book(cells: pl.DataFrame, variable: str, horizon: str, at: datetime) -> list[float]:
    """Normalised WEO errors (outcome - WEO) / scale of the vintage backtest cases whose outcome was published at
    or before ``at`` (all origins before ``at``)."""
    origins = [t for t in R.default_weo_origins(2008, at.year) if t < at]
    errs: list[float] = []
    for c in R.weo_cases(cells, variable, origins):
        w = c.preds.get(R.WEO)
        if (
            c.horizon == horizon
            and isinstance(w, Predictive)
            and c.outcome is not None
            and c.outcome_known_at is not None
            and c.outcome_known_at <= at
            and c.scale
        ):
            errs.append((c.outcome - w.point) / c.scale)
    return errs


def _sample(errs: Sequence[float]) -> list[float]:
    import numpy as np

    if len(errs) <= MAX_ERR_SAMPLE:
        return list(errs)
    probs = (np.arange(MAX_ERR_SAMPLE) + 0.5) / MAX_ERR_SAMPLE
    return [float(x) for x in np.quantile(np.asarray(errs), probs)]


def forecast_weo_registered(
    model_key: str, history: Sequence[tuple[int, float]], projection: float | None, errs: Sequence[float]
) -> Predictive | Abstain:
    if projection is None:
        return Abstain("no WEO projection cell for this period in the pinned table version")
    if model_key == FAIR_BENCHMARK:
        from reva.evaluation.pred_models.fair_benchmark import MIN_OWN, error_fan

        return error_fan(projection, errs, MIN_OWN)
    run = final_run(history, 60)
    scale = R.series_scale([v for _, v in run])
    if scale is None:
        return Abstain("series too short for its volatility scale")
    e = _sample(errs)
    if model_key == "forecast.weo_conformal":
        return CB.conformal(projection, e, scale=scale)
    adj = CB.bias_corrected(0.0, e, robust=True)
    if isinstance(adj, Abstain):
        return adj
    return CB.conformal(projection + scale * adj, e, scale=scale, center=True)


def supplement_batch(
    original: Mapping[str, Any],
    frames: Mapping[str, Any],
    cells: pl.DataFrame,
    *,
    issued_at: datetime,
    already: set[tuple[str, str]],
) -> dict[str, Any] | None:
    """Pure (given frames and vintage cells): the supplementary batch for one original batch, or None."""
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.questions import extract_series
    from reva.evaluation.registry.service import BATCH_SCHEMA, LABEL, _family_of

    scopes = issuing_scopes()
    if not scopes:
        return None
    books: dict[tuple[str, int], list[float]] = {}
    own_books: dict[tuple[str, int], dict[str, list[float]]] = {}
    var_of = {v: (t, var, h) for (t, var, h), v in FAMILY_OF.items()}
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    series_cache: dict[str, Any] = {}
    for q in original["questions"]:
        fam_key = q["family"]
        for mk, fams in scopes.items():
            if (fam_key, int(q.get("cycle_horizon", -1))) not in fams or (q["question_id"], mk) in already:
                continue
            if q["type"] != "quantity":
                continue
            fdoc = original["families"][fam_key]
            fam = _family_of(fdoc)
            pin = original["data"]["tables"][fam.dataset_key]
            if fam_key not in series_cache:
                series_cache[fam_key] = extract_series(frames[pin["artifact_sha256"]], fam)[0]
            s = series_cache[fam_key].get(q["geo"])
            if s is None:
                forecasts.append(abstention(q, mk, "series not found in the pinned table version"))
                continue
            key = (fam_key, int(q["cycle_horizon"]))
            _t, var, hz = var_of[key]
            if mk == FAIR_BENCHMARK:
                if key not in own_books:
                    own_books[key] = weo_own_errors(cells, var, hz, issued_at)
                errs = own_books[key].get(str(q.get("iso3") or q["geo"]), [])
            else:
                if key not in books:
                    books[key] = weo_error_book(cells, var, hz, issued_at)
                errs = books[key]
            target = P.parse_label(fam.frequency, q["reference_period"])
            cell = s.projections.get(target)
            proj = float(cell.value) if cell is not None else None
            hist = s.history()
            p = forecast_weo_registered(mk, hist, proj, errs)
            rec = abstention(q, mk, p.reason) if isinstance(p, Abstain) else _record(q, mk, p, hist)
            if not isinstance(p, Abstain):
                rec["inputs"] = {
                    "weo_projection_cell": dict(cell.refs[0]) if cell is not None else None,
                    "past_errors": len(errs),
                    "error_source": "vintage backtest (registry/forecasts/pred_models), outcomes published at "
                    "or before issued_at" + (" - this country's own raw WEO errors" if mk == FAIR_BENCHMARK else ""),
                }
                rec["attribution"] = (
                    "point: International Monetary Fund, World Economic Outlook (projection), as pinned"
                )
                rec["claim"] = "benchmark (error-based fan around the WEO point)" if mk == FAIR_BENCHMARK else CLAIM
            forecasts.append(rec)
            if not any(x["question_id"] == q["question_id"] for x in questions):
                questions.append(q)
    if not forecasts:
        return None
    reg = registered()
    return {
        "schema": BATCH_SCHEMA,
        "label": LABEL,
        "origin": original["origin"],
        "cycle": original["cycle"],
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original.get("_sha256"),
        "statement": "Supplementary sealed batch: forecasts of models registered after the original batch was "
        "sealed (locked-holdout winners, registry/forecasts/pred_models/registered_models.yaml), on the same "
        "questions and the same pinned table versions as the original batch, frozen at issued_at.",
        "specs": original["specs"],
        "families": {k: v for k, v in original["families"].items() if any(q["family"] == k for q in questions)},
        "models": [{"model_key": k, "version": v, "description": d} for k, (v, d) in registry_models().items()],
        "data": original["data"],
        "evidence": {
            "registered_scopes": [r for r in reg if r["candidate"] in scopes],
            "issuance_status": {k: issuance_status(k) for k in scopes if k != FAIR_BENCHMARK},
            "fair_benchmark": FAIR_BENCHMARK,
        },
        "code_digest": original.get("code_digest"),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


def issue_supplements(
    engine: Any, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry"
) -> dict[str, Any]:
    """Seals supplementary batches for the registered models on the sealed, open questions of the current cycle."""
    from sqlalchemy import text

    from reva.adapters.postgres import repositories as repo
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.data import FrameCache
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _batches, _insert_eval
    from reva.evaluation.vintages import load_vintage_cells

    models = registry_models()
    if not models:
        return {"status": "no_registered_issuer", "awaiting_family": awaiting_family()}
    with engine.connect() as conn:
        issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        cycle = P.cycle_of(issued_at.date())
        mv = {
            r["model_key"]: r["id"]
            for r in conn.execute(
                text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
                {"k": sorted(models)},
            ).mappings()
            if r["version"] == MODEL_VERSION
        }
        missing = sorted(set(models) - set(mv))
        if missing:
            return {"status": "models_not_seeded", "missing": missing}
        done = conn.execute(
            text(
                """SELECT task_scope->>'supplement_of' AS orig, metrics->>'model' AS m,
                          task_scope->'question_ids' AS q FROM model.evaluation
                   WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope ? 'supplement_of'"""
            ),
            {"k": ISSUE_KIND, "sk": SCOPE_KIND},
        ).mappings()
        already = {(qid, str(r["m"]).split("@")[0]) for r in done for qid in (r["q"] or [])}
        originals = [b for b in _batches(conn, origin) if b["cycle"] == cycle]
        cells = load_vintage_cells(conn, store)
    out = []
    cache = FrameCache(store, size=3)
    for b in originals:
        man = dict(load_frozen_manifest(store, b["sha"]))
        if man.get("supplement_of"):
            continue
        man["_sha256"] = b["sha"]
        frames = {t["artifact_sha256"]: cache.get(t["artifact_sha256"]) for t in man["data"]["tables"].values()}
        sup = supplement_batch(man, frames, cells, issued_at=issued_at, already=already)
        if sup is None:
            continue
        qids = sorted(q["question_id"] for q in sup["questions"])
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
            sha = repo.store_json_artifact(conn, store, sup, "evaluation", schema_version="forecast-registry-batch/1",
                                           rights={"licence": "internal-derived; WEO values: IMF terms (attribution)"})  # fmt: skip
            act = repo.simple_activity(
                "evaluation.forecast_registry_issue", "evaluation", actor,
                {"cycle": cycle, "origin": origin, "batch_sha256": sha, "supplement_of": b["sha"]},
            )  # fmt: skip
            repo.record_activity(conn, act, inputs=[(b["sha"], {"role": "original_batch"})], outputs=[sha])
            ctx = repo.create_context(
                conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=issued_at
            )
            scope = {
                "kind": SCOPE_KIND,
                "origin": origin,
                "cycle": cycle,
                "batch_sha256": sha,
                "supplement_of": b["sha"],
                "question_keys": sorted(q["question_key"] for q in sup["questions"]),
                "question_ids": qids,
            }
            for mk, (ver, _d) in models.items():
                mine = [f for f in sup["forecasts"] if f["model"] == mk]
                _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                             {"kind": ISSUE_KIND, "status": "sealed", "label": sup["label"], "model": f"{mk}@{ver}",
                              "issued_at": issued_at.isoformat(), "batch_sha256": sha, "supplement_of": b["sha"],
                              "n_questions": len(qids),
                              "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                              "n_abstained": sum(1 for f in mine if f["status"] == "abstained")}, act.id)  # fmt: skip
        out.append({"batch_sha256": sha, "supplement_of": b["sha"], "questions": len(qids),
                    "issued": sum(1 for f in sup["forecasts"] if f["status"] == "issued")})  # fmt: skip
    return {"status": "issued" if out else "nothing_to_issue", "cycle": cycle, "supplements": out,
            "awaiting_family": awaiting_family()}  # fmt: skip


__all__ = [
    "CLAIM",
    "FAIR_BENCHMARK",
    "FAMILY_OF",
    "awaiting_family",
    "fair_gate",
    "forecast_weo_registered",
    "issuance_decisions",
    "issuance_status",
    "issue_supplements",
    "issuing_scopes",
    "registered",
    "registry_models",
    "supplement_batch",
    "weo_error_book",
    "weo_own_errors",
]
