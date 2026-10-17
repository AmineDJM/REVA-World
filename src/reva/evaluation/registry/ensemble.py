"""REVA ensemble in the prospective forecast registry (V7, agent BEAT-ENSEMBLE).

For every registry family, an online combination of **all** forecasters sealed on a question (REVA baselines and
models, and the attributed official forecasts: IMF WEO, EIA STEO, ...) is sealed as ``reva.ensemble``:

* **weights from resolved prospective scores only**: the ``registry_score`` rows recorded at or before the
  ensemble's ``issued_at`` (database clock) for questions of the same family and type. Back-tests
  (``registry_backtest``, final-vintage pseudo-real-time), counterfactual scores of voided questions and the
  ensemble's own scores never enter. Until a task has ``WeightRule.min_resolved`` resolved questions the weights are
  equal and labelled ``equal_weights_insufficient_history``;
* **tasks** (one weight vector each): ``family|type|role`` with role ``location`` (absolute error of the point),
  ``shape`` (CRPS for quantity questions, interval score for interval questions; distributional components only)
  and ``probability`` (Brier score, binary questions);
* the combination rule is :mod:`reva.models.forecasting.online_combination` (Hedge on shrunk relative losses,
  weighted-mean location, quantile averaging of deviations, linear pool of probabilities);
* **sealed** as a supplementary batch of the question's original batch (``supplement_of``; same question records
  and table pins, so ``service.resolve_due`` resolves and scores it unchanged). The manifest records, per task, the
  rule, the exact score rows used (question, model, loss; their resolution manifests) with their digest, and the
  resulting weights; each forecast records its components (model + sealed batch) and its weights - the batch can be
  re-derived bit for bit (:func:`rebuild_forecasts`).

Same-date rule (V7 protocol): an ensemble is issued only for questions sealed at most ``SAME_WINDOW_DAYS`` before
it (normally the same weekly run), whose reference period has not ended, and that are not resolved; it combines only
forecasts sealed before it. The scoreboard (:func:`scoreboard`) compares it with its own components on the
questions both scored: no backfilled claim is possible because the ensemble has scores only for questions it sealed
before resolution.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import Engine, text

from reva.evaluation.registry.forecasts import (
    FORECAST_SCHEMA,
    QUANTILE_LEVELS,
    abstention,
    num,
    parse_num,
    predictive_from_record,
)
from reva.evaluation.registry.questions import period_ended
from reva.kernel.ids import logical_hash
from reva.models.forecasting import online_combination as OC
from reva.models.forecasting.registry_baselines import Predictive

ENSEMBLE_KEY = "reva.ensemble"
V1 = "1.0.0"
#: current method version (lead decision 2026-10-17: one weight per information source); 1.0.0 batches stay sealed
ENSEMBLE_VERSION = "2.0.0"
DESCRIPTION_V1 = (
    "REVA ensemble (V7): online combination of every forecaster sealed on the question (REVA baselines/models and "
    "attributed official forecasts) with weights from resolved prospective scores only (Hedge on shrunk relative "
    "losses; equal weights, labelled, until 20 resolved questions per family/type/role); weighted-mean point, "
    "quantile averaging, linear pool. A combination that uses the official forecast as an input: a win reads "
    "'REVA's combination beats <forecaster> alone', never 'REVA beats <forecaster>'"
)
DESCRIPTION_V2 = (
    "REVA ensemble, method 2: as 1.0.0, but forecasters built around the same official point share one weight per "
    "information source (registry/models/methods.yaml information_source, e.g. imf.weo.point = WEO projection, "
    "weo_conformal, weo_bias_corrected, weo_error_fan), split equally among the members present; equal weights per "
    "source, labelled, until 20 resolved questions per family/type/role. A win reads 'la combinaison REVA bat "
    "<forecaster> seul'"
)
DESCRIPTIONS = {V1: DESCRIPTION_V1, ENSEMBLE_VERSION: DESCRIPTION_V2}
ENSEMBLE_DESCRIPTION = DESCRIPTIONS[ENSEMBLE_VERSION]
ENSEMBLE_LABEL = (
    "REVA ENSEMBLE: online combination of the forecasts sealed on each question (official forecasts included, "
    "attributed), weights from resolved prospective scores only (equal until enough resolutions); prospective, "
    "frozen and sealed before resolution; not advice"
)
SAME_WINDOW_DAYS = 8
#: (question type, role) -> score metric of the role
ROLE_METRIC: dict[tuple[str, str], str] = {
    ("quantity", "location"): "abs_error",
    ("quantity", "shape"): "crps",
    ("interval", "location"): "abs_error",
    ("interval", "shape"): "interval_score",
    ("binary", "probability"): "brier",
}
#: scoreboard metrics per question type (lower is better); CRPS only against distributional forecasters (QA-PRED
#: X2: the CRPS of a point forecast is its absolute error), absolute error against every forecaster with a point
BOARD_METRICS: dict[str, tuple[str, ...]] = {
    "quantity": ("crps", "abs_error"),
    "interval": ("interval_score", "abs_error"),
    "binary": ("brier",),
}
MIN_PAIRS = 20
MIN_SHARE = 0.5
ALPHA = 0.05


def model_entry() -> dict[str, tuple[str, str]]:
    return {ENSEMBLE_KEY: (ENSEMBLE_VERSION, ENSEMBLE_DESCRIPTION)}


@lru_cache(maxsize=1)
def information_sources() -> dict[str, str]:
    """model_key -> declared ``information_source`` (``registry/models/methods.yaml``); undeclared models are their
    own source. The map used is sealed in every method-2 weight record."""
    import yaml

    path = Path(__file__).resolve().parents[4] / "registry" / "models" / "methods.yaml"
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return {str(m["model_key"]): str(m["information_source"]) for m in doc.get("models") or []
            if m.get("information_source")}  # fmt: skip


def rule_for(version: str) -> OC.WeightRule:
    return OC.WeightRule(by_source=version != V1)


def _record_version(version: str) -> str | None:
    """Version stamped on each forecast record (1.0.0 records carry none: they were sealed before 2.0.0 existed)."""
    return None if version == V1 else version


def roles_of(qtype: str) -> list[str]:
    return [r for (t, r) in ROLE_METRIC if t == qtype]


def task_id(family: str, qtype: str, role: str) -> str:
    return f"{family}|{qtype}|{role}"


def _has_role(f: Mapping[str, Any], role: str) -> bool:
    if f.get("status") != "issued":
        return False
    if role == "location":
        return f.get("point") is not None
    if role == "shape":
        return f.get("point") is not None and (f.get("predictive") or {}).get("family") in ("normal", "empirical")
    return f.get("prob_above") is not None


# --------------------------------------------------------------------------- weights (pure)


def history_rows(scores: Sequence[Mapping[str, Any]], family: str, qtype: str, metric: str) -> list[OC.LossRow]:
    """Loss rows of one task from resolved score records (the ensemble's own scores excluded)."""
    out = []
    for s in scores:
        if s.get("family") != family or s.get("type") != qtype or s.get("model") == ENSEMBLE_KEY:
            continue
        v = s.get(metric)
        if v is None:
            continue
        x = float(v)
        if math.isfinite(x):
            out.append(OC.LossRow(str(s["question_id"]), str(s["model"]), x))
    return sorted(out, key=lambda r: (r.question_id, r.model))


def task_weights(
    family: str,
    qtype: str,
    role: str,
    components: Sequence[str],
    scores: Sequence[Mapping[str, Any]],
    rule: OC.WeightRule,
    sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Sealed weight record of one task: rule, exact score rows used (with digest) and the weights (method 2: per
    information source, with the source map used)."""
    metric = ROLE_METRIC[(qtype, role)]
    rows = [r for r in history_rows(scores, family, qtype, metric) if r.model in set(components)]
    if rule.by_source:
        w = OC.grouped_weights(components, sources or {}, rows, rule)
    else:
        w = OC.online_weights(components, rows, rule)
    hist = [[r.question_id, r.model, num(r.loss)] for r in rows]
    res_shas = sorted({str(s["resolution_sha256"]) for s in scores if s.get("resolution_sha256")
                       and s.get("family") == family and s.get("type") == qtype})  # fmt: skip
    by_source = (
        {
            "sources": dict(sorted(w["sources"].items())),
            "source_weights": {k: num(v) for k, v in sorted(w["source_weights"].items())},
        }
        if rule.by_source
        else {}
    )
    return {
        **by_source,
        "task": task_id(family, qtype, role),
        "metric": metric,
        "rule": {k: (num(v) if isinstance(v, float) else v) for k, v in rule.doc().items()},
        "components": sorted(set(components)),
        "status": w["status"],
        "n_resolved_questions": w["n_resolved_questions"],
        "n_dropped_questions": w.get("n_dropped_questions", 0),
        "eta": num(w["eta"]) if w.get("eta") is not None else None,
        "weights": {k: num(v) for k, v in sorted(w["weights"].items())},
        "per_component": {
            k: {kk: (num(vv) if isinstance(vv, float) else vv) for kk, vv in d.items()}
            for k, d in sorted(w["per_component"].items())
        },
        "history": hist,
        "history_digest": logical_hash(hist),
        "resolution_sha256s": res_shas,
    }


# --------------------------------------------------------------------------- one ensemble forecast (pure)


def ensemble_forecast(
    q: Mapping[str, Any],
    comps: Sequence[tuple[str, Mapping[str, Any]]],
    tasks: Mapping[str, Mapping[str, Any]],
    version: str | None = None,
) -> dict[str, Any]:
    """``comps``: (sealed batch sha, issued forecast record) of the other forecasters on ``q``; ``tasks``: role ->
    sealed weight record (method 2 when it carries ``sources``); ``version`` stamped on the record if given."""
    qtype = str(q["type"])
    by_model = {
        f["model"]: (sha, f) for sha, f in sorted(comps, key=lambda c: c[1]["model"]) if f["model"] != ENSEMBLE_KEY
    }
    roles = roles_of(qtype)
    present = {r: sorted(m for m, (_s, f) in by_model.items() if _has_role(f, r)) for r in roles}
    needed = "probability" if qtype == "binary" else "location"
    if not present[needed]:
        return abstention(q, ENSEMBLE_KEY, f"no component forecast with a {needed} on this question")
    grouped = any("sources" in tasks[r] for r in roles)
    weights: dict[str, dict[str, float]] = {}
    for r in roles:
        t = tasks[r]
        if "sources" in t:
            sw = {k: parse_num(v) for k, v in t["source_weights"].items()}
            weights[r] = OC.restrict_grouped(sw, t["sources"], present[r])
        else:
            weights[r] = OC.restrict({k: parse_num(v) for k, v in t["weights"].items()}, present[r])
    out: dict[str, Any] = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": ENSEMBLE_KEY,
                           "status": "issued"}  # fmt: skip
    if version is not None:
        out["model_version"] = version
    if qtype == "binary":
        probs = {m: parse_num(by_model[m][1]["prob_above"]) for m in present["probability"]}
        out["prob_above"] = num(OC.combine_probability(probs, weights["probability"]))
    else:
        points = {m: parse_num(by_model[m][1]["point"]) for m in present["location"]}
        loc = OC.combine_location(points, weights["location"])
        if present["shape"]:
            preds: dict[str, Predictive] = {
                m: predictive_from_record(by_model[m][1]["point"], by_model[m][1]["predictive"])
                for m in present["shape"]
            }
            sample = [parse_num(num(v)) for v in OC.combine_quantiles(loc, preds, weights["shape"], OC.SAMPLE_LEVELS)]
            out["point"] = num(loc)
            out["predictive"] = {"family": "empirical", "sample": [num(v) for v in sample]}
            qs = OC.combine_quantiles(loc, preds, weights["shape"], QUANTILE_LEVELS)
            out["quantiles"] = {f"{lv:g}": num(v) for lv, v in zip(QUANTILE_LEVELS, qs, strict=True)}
            if qtype == "interval":
                a = (1.0 - float(q["interval_level"])) / 2.0
                lo, hi = OC.combine_quantiles(loc, preds, weights["shape"], (a, 1.0 - a))
                out["interval"] = [num(lo), num(hi)]
        else:
            out["point"] = num(loc)
            out["predictive"] = {"family": "point"}
            if qtype == "interval":
                out["interval"] = None
    out["ensemble"] = {
        "method": OC.METHOD_BY_SOURCE if grouped else OC.METHOD,
        "components": [{"model": m, "batch_sha256": by_model[m][0]} for m in sorted(by_model)],
        "weight_status": {r: tasks[r]["status"] for r in roles},
        "tasks": {r: tasks[r]["task"] for r in roles},
        "weights": {r: {k: num(v) for k, v in sorted(weights[r].items())} for r in roles},
    }
    if grouped:
        srcs: dict[str, str] = {}
        for r in roles:
            srcs.update({m: tasks[r]["sources"][m] for m in present[r] if m in tasks[r].get("sources", {})})
        out["ensemble"]["sources"] = dict(sorted(srcs.items()))
    out["detail"] = {"n_components": str(len(by_model))}
    return out


# --------------------------------------------------------------------------- batch (pure)


def build_ensemble_batch(
    original: Mapping[str, Any],
    components: Mapping[str, Sequence[tuple[str, Mapping[str, Any]]]],
    scores: Sequence[Mapping[str, Any]],
    *,
    issued_at: datetime,
    already: set[str],
    rule: OC.WeightRule | None = None,
    version: str = ENSEMBLE_VERSION,
    sources: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The ensemble supplement of one original batch (``original['_sha256']``), or None when nothing is due.

    ``components``: question id -> [(sealed batch sha, forecast record)] of every forecaster sealed on it;
    ``scores``: resolved score records (each with ``model`` and ``resolution_sha256``) known at ``issued_at``."""
    from reva.evaluation.registry.service import BATCH_SCHEMA

    rule = rule or rule_for(version)
    if rule.by_source and sources is None:
        sources = information_sources()
    fam_docs: Mapping[str, Any] = original.get("families") or {}
    skipped: list[dict[str, Any]] = []
    due = []
    for q in original["questions"]:
        if q["question_id"] in already:
            continue
        if period_ended(q, issued_at.date()) and not (fam_docs.get(q["family"]) or {}).get("nowcast"):
            skipped.append({"question_id": q["question_id"], "reason": "reference period ended before the ensemble"})
            continue
        due.append(q)
    if not due:
        return None
    universe: dict[str, set[str]] = defaultdict(set)
    for q in due:
        for r in roles_of(str(q["type"])):
            for _sha, f in components.get(q["question_id"], ()):
                if f["model"] != ENSEMBLE_KEY and _has_role(f, r):
                    universe[task_id(q["family"], q["type"], r)].add(f["model"])
    tasks: dict[str, dict[str, Any]] = {}
    for tk in sorted(universe):
        fam, qtype, role = tk.split("|")
        tasks[tk] = task_weights(fam, qtype, role, sorted(universe[tk]), scores, rule, sources)
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    for q in due:
        qt = {r: tasks.get(task_id(q["family"], q["type"], r)) or _empty_task(q, r) for r in roles_of(str(q["type"]))}
        rec = ensemble_forecast(q, components.get(q["question_id"], ()), qt, _record_version(version))
        questions.append(dict(q))
        forecasts.append(rec)
    return {
        "schema": BATCH_SCHEMA,
        "label": ENSEMBLE_LABEL,
        "origin": original["origin"],
        "cycle": original["cycle"],
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original["_sha256"],
        "statement": "Supplementary sealed batch: the REVA ensemble of the forecasts sealed on these questions (same "
        "question records and pinned table versions as the original batch), weighted only from prospective scores "
        "resolved at or before issued_at, frozen at issued_at.",
        "specs": original["specs"],
        "families": {k: v for k, v in fam_docs.items() if any(q["family"] == k for q in questions)},
        "models": [{"model_key": ENSEMBLE_KEY, "version": version, "description": DESCRIPTIONS[version]}],
        "data": original["data"],
        "ensemble": {
            "rule": {k: (num(v) if isinstance(v, float) else v) for k, v in rule.doc().items()},
            "score_source": "registry_score rows (kind registry_score, same origin) recorded at or before issued_at; "
            "back-tests, counterfactual scores and the ensemble's own scores excluded",
            "same_window_days": SAME_WINDOW_DAYS,
            "model_version": version,
            "tasks": tasks,
        },
        "code_digest": original.get("code_digest"),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": skipped, "pending_families": [], "notes": []},
    }


def _empty_task(q: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {"task": task_id(q["family"], q["type"], role), "status": "no_component", "weights": {}}


def rebuild_forecasts(
    manifest: Mapping[str, Any], components: Mapping[str, Sequence[tuple[str, Mapping[str, Any]]]]
) -> list[dict[str, Any]]:
    """Re-derive a sealed ensemble batch's forecasts from its recorded weight tasks and the component batches."""
    tasks = manifest["ensemble"]["tasks"]
    version = _record_version(str(manifest["ensemble"].get("model_version") or V1))
    out = []
    for q in manifest["questions"]:
        sealed = next(f for f in manifest["forecasts"] if f["question_id"] == q["question_id"])
        used = {c["batch_sha256"] for c in (sealed.get("ensemble") or {}).get("components", [])}
        comps = [(s, f) for s, f in components.get(q["question_id"], ()) if s in used or not used]
        qt = {r: tasks.get(task_id(q["family"], q["type"], r)) or _empty_task(q, r) for r in roles_of(str(q["type"]))}
        out.append(ensemble_forecast(q, comps, qt, version))
    return out


def recheck_weights(manifest: Mapping[str, Any]) -> bool:
    """The recorded weights follow from the recorded score rows and rule (pure re-computation)."""
    for t in manifest["ensemble"]["tasks"].values():
        rd = t["rule"]
        by_source = rd.get("method") == OC.METHOD_BY_SOURCE
        rule = OC.WeightRule(int(rd["min_resolved"]), float(rd["prior_n"]), float(rd["cap"]), by_source)
        rows = [OC.LossRow(a, b, parse_num(c)) for a, b, c in t["history"]]
        if logical_hash(t["history"]) != t["history_digest"]:
            return False
        if by_source:
            w = OC.grouped_weights(t["components"], t["sources"], rows, rule)
            if {k: num(v) for k, v in sorted(w["source_weights"].items())} != t["source_weights"]:
                return False
        else:
            w = OC.online_weights(t["components"], rows, rule)
        if w["status"] != t["status"] or {k: num(v) for k, v in sorted(w["weights"].items())} != t["weights"]:
            return False
    return True


# --------------------------------------------------------------------------- service (worker credentials)


def _score_records(conn: Any, origin: str, until: datetime | None) -> list[dict[str, Any]]:
    from reva.evaluation.registry.service import SCOPE_KIND, SCORE_KIND

    tc = " AND e.recorded_at <= :t" if until is not None else ""
    rows = conn.execute(
        text(
            f"""SELECT e.metrics->'scores' AS scores, e.metrics->>'resolution_sha256' AS rsha,
                       e.task_scope->>'batch_sha256' AS bsha
                FROM model.evaluation e
                WHERE e.metrics->>'kind' = :k AND e.task_scope->>'kind' = :sk AND e.task_scope->>'origin' = :o{tc}
                ORDER BY e.recorded_at, e.id"""
        ),
        {"k": SCORE_KIND, "sk": SCOPE_KIND, "o": origin, **({"t": until} if until is not None else {})},
    ).mappings()
    out = []
    for r in rows:
        for s in r["scores"] or []:
            out.append({**s, "resolution_sha256": r["rsha"], "batch_sha256": r["bsha"]})
    return out


def ensembled_questions(conn: Any, origin: str, version: str = ENSEMBLE_VERSION) -> set[str]:
    """Questions that already have an ensemble forecast of this method version (1.0.0 rows carry no version)."""
    from reva.evaluation.registry.service import ISSUE_KIND, SCOPE_KIND

    rows = conn.execute(
        text(
            """SELECT DISTINCT jsonb_array_elements_text(task_scope->'question_ids') FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope->>'origin' = :o
                 AND task_scope->>'ensemble' = 'true'
                 AND COALESCE(task_scope->>'ensemble_version', '1.0.0') = :v"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "o": origin, "v": version},
    ).scalars()
    return set(rows)


def _load_batches(conn: Any, store: Any, origin: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry.service import _batches

    out = []
    for b in _batches(conn, origin):
        m = dict(load_frozen_manifest(store, b["sha"]))
        m["_sha256"] = b["sha"]
        out.append((b, m))
    return out


def components_of(
    original_sha: str, manifests: Sequence[Mapping[str, Any]], *, before: datetime | None = None
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """question id -> [(batch sha, issued forecast)] from the original batch and its non-ensemble supplements
    sealed at or before ``before``."""
    out: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for m in manifests:
        if m["_sha256"] != original_sha and m.get("supplement_of") != original_sha:
            continue
        if m.get("ensemble") is not None:
            continue
        if before is not None and datetime.fromisoformat(str(m["issued_at"])) > before:
            continue
        for f in m["forecasts"]:
            if f.get("status") == "issued" and f["model"] != ENSEMBLE_KEY:
                out[f["question_id"]].append((m["_sha256"], f))
    return out


def issue_ensemble(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    actor: str = "worker:forecast_registry",
    rule: OC.WeightRule | None = None,
    version: str = ENSEMBLE_VERSION,
) -> dict[str, Any]:
    """Seal the REVA ensemble (method ``version``, default the current one) on every recently sealed, open question
    without one of that version (idempotent). Earlier versions' sealed batches are never touched."""
    from reva.adapters.postgres import repositories as repo
    from reva.evaluation.registry.service import (
        ISSUE_KIND,
        ISSUE_LOCK,
        SCOPE_KIND,
        _check_origin,
        _insert_eval,
        handled_questions,
    )

    _check_origin(origin)
    with engine.connect() as conn:
        issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        mv = conn.execute(
            text("SELECT id::text FROM model.version WHERE model_key = :k AND version = :v"),
            {"k": ENSEMBLE_KEY, "v": version},
        ).scalar_one_or_none()
        if mv is None:
            return {"status": "model_not_seeded", "model": f"{ENSEMBLE_KEY}@{version}"}
        loaded = _load_batches(conn, store, origin)
        scores = _score_records(conn, origin, issued_at)
        already = ensembled_questions(conn, origin, version)
        handled = {b["sha"]: handled_questions(conn, b["sha"]) for b, m in loaded if not m.get("supplement_of")}
    manifests = [m for _b, m in loaded]
    horizon = issued_at - timedelta(days=SAME_WINDOW_DAYS)
    out: list[dict[str, Any]] = []
    late = 0
    for b, man in loaded:
        if man.get("supplement_of"):
            continue
        seal = datetime.fromisoformat(str(man["issued_at"]))
        if seal < horizon:
            late += 1
            continue
        comps = components_of(b["sha"], manifests, before=issued_at)
        sup = build_ensemble_batch(man, comps, scores, issued_at=issued_at, already=already | handled[b["sha"]],
                                   rule=rule, version=version)  # fmt: skip
        if sup is None:
            continue
        qids = sorted(q["question_id"] for q in sup["questions"])
        comp_shas = sorted(
            {c["batch_sha256"] for f in sup["forecasts"] for c in (f.get("ensemble") or {}).get("components", [])}
        )
        res_shas = sorted({s for t in sup["ensemble"]["tasks"].values() for s in t["resolution_sha256s"]})
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
            if ensembled_questions(conn, origin, version) & set(qids):
                raise RuntimeError("another ensemble issue overlapped; re-run to issue what is left")
            sha = repo.store_json_artifact(
                conn, store, sup, "evaluation", schema_version="forecast-registry-batch/1",
                rights={"licence": "internal-derived; component forecasts keep their attribution and source rights"},
            )  # fmt: skip
            act = repo.simple_activity(
                "evaluation.forecast_registry_issue", "evaluation", actor,
                {"cycle": sup["cycle"], "origin": origin, "batch_sha256": sha, "supplement_of": b["sha"],
                 "ensemble": True, "ensemble_version": version, "label": ENSEMBLE_LABEL},
            )  # fmt: skip
            repo.record_activity(
                conn,
                act,
                inputs=[(s, {"role": "component_batch"}) for s in comp_shas]
                + [(s, {"role": "resolution_used_for_weights"}) for s in res_shas],
                outputs=[sha],
            )
            ctx = repo.create_context(
                conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=issued_at
            )
            scope = {
                "kind": SCOPE_KIND,
                "origin": origin,
                "cycle": sup["cycle"],
                "batch_sha256": sha,
                "supplement_of": b["sha"],
                "ensemble": True,
                "ensemble_version": version,
                "question_keys": sorted(q["question_key"] for q in sup["questions"]),
                "question_ids": qids,
            }
            n_iss = sum(1 for f in sup["forecasts"] if f["status"] == "issued")
            status_counts: dict[str, int] = defaultdict(int)
            for t in sup["ensemble"]["tasks"].values():
                status_counts[t["status"]] += 1
            _insert_eval(conn, mv, ctx, sha, "operational_as_known", scope,
                         {"kind": ISSUE_KIND, "status": "sealed", "label": ENSEMBLE_LABEL,
                          "model": f"{ENSEMBLE_KEY}@{version}", "issued_at": issued_at.isoformat(),
                          "batch_sha256": sha, "supplement_of": b["sha"], "n_questions": len(qids),
                          "n_issued": n_iss, "n_abstained": len(sup["forecasts"]) - n_iss,
                          "weight_tasks": dict(status_counts)}, act.id)  # fmt: skip
        out.append({"batch_sha256": sha, "supplement_of": b["sha"], "questions": len(qids), "issued": n_iss,
                    "weight_tasks": dict(status_counts)})  # fmt: skip
    return {"status": "issued" if out else "nothing_to_issue", "version": version, "issued_at": issued_at.isoformat(),
            "ensembles": out, "originals_outside_same_window": late}  # fmt: skip


# --------------------------------------------------------------------------- scoreboard (read side)


def _cells(
    ens: Mapping[str, Mapping[str, Any]], scores: Mapping[tuple[str, str], Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Per (family, type, metric): ensemble vs each of its components on the questions both scored; the best single
    forecaster is the eligible component the ensemble does worst against (selected ex post: conservative)."""
    from reva.evaluation.conflict.stats import dm_test

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for qid, e in ens.items():
        groups[(e["family"], e["type"])].append(qid)
    cells = []
    for (fam, qtype), qids in sorted(groups.items()):
        for metric in BOARD_METRICS.get(qtype, ()):
            e_sc = {q: float(scores[(q, ENSEMBLE_KEY)][metric]) for q in qids
                    if scores.get((q, ENSEMBLE_KEY), {}).get(metric) is not None}  # fmt: skip
            comps = sorted({c for q in qids for c in ens[q]["components"]})
            per: list[dict[str, Any]] = []
            for c in comps:
                pairs = [(q, e_sc[q], float(scores[(q, c)][metric])) for q in sorted(e_sc)
                         if c in ens[q]["components"] and scores.get((q, c), {}).get(metric) is not None]  # fmt: skip
                if not pairs:
                    continue
                me = math.fsum(p[1] for p in pairs) / len(pairs)
                mc = math.fsum(p[2] for p in pairs) / len(pairs)
                per.append({"model": c, "n_common": len(pairs), "mean_ensemble": me, "mean_competitor": mc,
                            "ratio": me / mc if mc > 0 else None, "_pairs": pairs})  # fmt: skip
            need = max(MIN_PAIRS, math.ceil(MIN_SHARE * len(e_sc)))
            eligible = [p for p in per if p["n_common"] >= need and p["ratio"] is not None]
            cell: dict[str, Any] = {
                "family": fam,
                "type": qtype,
                "metric": metric,
                "n_ensemble_issued": len(qids),
                "n_ensemble_scored": len(e_sc),
                "min_pairs": need,
                "competitors": [{k: v for k, v in p.items() if k != "_pairs"} for p in per],
                "best_single": None,
                "test": None,
                "status": "awaiting_resolutions" if len(e_sc) < MIN_PAIRS else "no_comparable_competitor",
            }
            if eligible:
                best = max(eligible, key=lambda p: (p["ratio"], p["model"]))
                d, geo, per_ = [], [], []
                for q, le, lc in best["_pairs"]:
                    vals = [float(scores[(q, c)][metric]) for c in ens[q]["components"]
                            if scores.get((q, c), {}).get(metric) is not None]  # fmt: skip
                    scale = median(vals) if vals else 0.0
                    if scale <= 0:
                        continue
                    d.append((le - lc) / scale)
                    geo.append(str(ens[q]["geo"]))
                    per_.append(str(ens[q]["reference_period"]))
                t = dm_test(d, geo, per_)
                cell.update(
                    best_single={k: v for k, v in best.items() if k != "_pairs"},
                    test={**t, "differential": "(loss_ensemble - loss_best) / median component loss of the question"},
                    status="tested",
                )  # fmt: skip
            cells.append(cell)
    return cells


def scoreboard(conn: Any, store: Any, origin: str | None) -> dict[str, Any]:
    """ "REVA ensemble vs best single forecaster" per family, from resolved prospective scores only."""
    from reva.evaluation.conflict.stats import holm
    from reva.evaluation.registry.read import CACHE, _origin_clause
    from reva.evaluation.registry.service import ISSUE_KIND, SCOPE_KIND, SCORE_KIND

    oc, op = _origin_clause(origin)
    shas = (
        conn.execute(
            text(
                f"""SELECT DISTINCT evaluation_manifest_sha256 FROM model.evaluation
                WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope->>'ensemble' = 'true'{oc}"""
            ),
            {"k": ISSUE_KIND, "sk": SCOPE_KIND, **op},
        )
        .scalars()
        .all()
    )
    ens: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)  # version -> question id -> meta
    weight_status: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for sha in sorted(shas):
        m = CACHE.get(store, sha)
        ver = str((m.get("ensemble") or {}).get("model_version") or V1)
        qmeta = {q["question_id"]: q for q in m["questions"]}
        for f in m["forecasts"]:
            q = qmeta[f["question_id"]]
            fam = str(q["family"])
            if f["status"] != "issued":
                weight_status[ver][fam]["abstained"] += 1
                continue
            st = set((f.get("ensemble") or {}).get("weight_status", {}).values())
            weight_status[ver][fam][OC.ONLINE if OC.ONLINE in st else (OC.EQUAL if OC.EQUAL in st else "other")] += 1
            ens[ver][f["question_id"]] = {
                "family": fam,
                "type": q["type"],
                "geo": q["geo"],
                "reference_period": q["reference_period"],
                "components": [c["model"] for c in f["ensemble"]["components"]],
            }
    wanted = {qid for e in ens.values() for qid in e}
    scores: dict[tuple[str, str], dict[str, Any]] = {}  # (question, model) ; ensemble: model "reva.ensemble@<ver>"
    rows = conn.execute(
        text(
            f"""SELECT metrics->'scores' AS scores FROM model.evaluation
                WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk{oc} ORDER BY recorded_at"""
        ),
        {"k": SCORE_KIND, "sk": SCOPE_KIND, **op},
    ).scalars()
    for sc in rows:
        for s in sc or []:
            if s["question_id"] not in wanted:
                continue
            mk = s["model"]
            if mk == ENSEMBLE_KEY:
                mk = f"{ENSEMBLE_KEY}@{s.get('model_version') or V1}"
            scores.setdefault((s["question_id"], mk), s)
    cells: list[dict[str, Any]] = []
    for ver in sorted(ens):
        own = {k: v for k, v in scores.items() if not k[1].startswith(ENSEMBLE_KEY + "@")}
        own.update({(q, ENSEMBLE_KEY): v for (q, m), v in scores.items() if m == f"{ENSEMBLE_KEY}@{ver}"})
        for c in _cells(ens[ver], own):
            cells.append({"ensemble_version": ver, **c})

    def key(c: Mapping[str, Any]) -> str:
        return f"{c['ensemble_version']}|{c['family']}|{c['type']}|{c['metric']}"

    keys = {key(c): (c["test"] or {}).get("p_two_sided") for c in cells}
    adj = holm(keys)
    n_family = sum(1 for p in keys.values() if p is not None)
    headline: dict[str, list[str]] = defaultdict(list)
    for c in cells:
        c["p_holm"] = adj.get(key(c))
        c["holm_family_size"] = n_family
        t = c["test"] or {}
        c["claim_fr"] = None
        if c["status"] != "tested":
            c["verdict"] = c["status"]
        elif c["p_holm"] is None or t.get("mean_diff") is None:
            c["verdict"] = "not_testable"
        elif c["p_holm"] < ALPHA:
            c["verdict"] = "ensemble_better" if t["mean_diff"] < 0 else "best_single_better"
        else:
            c["verdict"] = "no_significant_difference"
        if c["verdict"] == "ensemble_better":
            c["claim_fr"] = f"la combinaison REVA bat {c['best_single']['model']} seul ({c['metric']}, {c['type']})"
            if c["ensemble_version"] not in headline[c["family"]]:
                headline[c["family"]].append(c["ensemble_version"])
    return {
        "label": ENSEMBLE_LABEL,
        "model": ENSEMBLE_KEY,
        "current_version": ENSEMBLE_VERSION,
        "rule": (
            "Per ensemble method version, family, question type and metric: the ensemble against each forecaster it "
            "combined, on the questions both scored; the best single forecaster is the eligible competitor (scored on "
            ">= max(20, 50 %) of the ensemble's scored questions) with the highest mean-loss ratio ensemble/competitor "
            "(chosen ex post: conservative). DM test of the per-question loss differential scaled by the question's "
            "median component loss, two-way clustered (geography, reference period); Holm over every tested cell of "
            "every version (two-sided). CRPS only against distributional forecasters; absolute error against every "
            "forecaster with a point. reva.ensemble is the headline forecast only in families listed in "
            "headline_families (an ensemble_better verdict); a win reads 'la combinaison REVA bat <forecaster> seul'."
        ),
        "alpha": ALPHA,
        "holm_family_size": n_family,
        "n_ensemble_forecasts": sum(sum(f.values()) for v in weight_status.values() for f in v.values()),
        "n_ensemble_scored": sum(1 for (_q, m) in scores if m.startswith(ENSEMBLE_KEY + "@")),
        "weights_by_version": {
            v: {k: dict(x) for k, x in sorted(fams.items())} for v, fams in sorted(weight_status.items())
        },
        "headline_families": dict(sorted(headline.items())),
        "cells": cells,
    }


__all__ = [
    "BOARD_METRICS",
    "ENSEMBLE_KEY",
    "ENSEMBLE_LABEL",
    "ENSEMBLE_VERSION",
    "ROLE_METRIC",
    "SAME_WINDOW_DAYS",
    "V1",
    "build_ensemble_batch",
    "components_of",
    "ensemble_forecast",
    "ensembled_questions",
    "information_sources",
    "issue_ensemble",
    "model_entry",
    "rebuild_forecasts",
    "recheck_weights",
    "rule_for",
    "scoreboard",
    "task_weights",
]
