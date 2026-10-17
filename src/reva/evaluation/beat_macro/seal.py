"""Prospective issuance of the confirmed BEAT-MACRO scope(s) as supplementary sealed registry batches - monthly.

Only scopes confirmed on the holdout (``holdout_ledger.yaml`` -> ``confirmed``) issue, and only on registry
question families asking exactly that variable for the current year:

* ``g7rt | unemployment_rate | cy | forecast.bm_stack`` -> family ``weo.unemployment``, cycle horizon 0, the G7
  questions of the current cycle.

**Monthly issuance (lead decision, V7).** One supplementary batch per calendar month M (``task_scope``
``beat_macro_month = "YYYY-MM"``): the forecast is computed exactly as in the evaluation (``cases.g7_cases`` +
``combine.add_second_pass``) at the evaluation origin of month M, i.e. with the information published by
00:00 UTC on the 1st of M (OECD MEI edition and IMF WEO vintage in force, past errors whose outcome was published),
in the bucket of month M. The weekly ``forecast_registry:issue`` job calls :func:`issue_monthly`, which seals the
month's batch only if it is absent (idempotent). Early issuance for the next month (``month`` = M while the clock
is in M-1) uses the information at issue time and asserts that no outcome was published between issue and the 1st.

The forecast **uses the IMF WEO projection as an input** (attributed). **Display: point only** (lead decision): the
interval is stored in the sealed record but labelled ``INTERVAL_LABEL`` and masked by the read layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from reva.evaluation.beat_macro import cases as C
from reva.evaluation.beat_macro import combine as K
from reva.evaluation.beat_macro import service as S
from reva.evaluation.beat_macro.inputs import LaggedMonthly, mi, month_start
from reva.models.forecasting.registry_baselines import Abstain, Predictive

#: confirmed scope key -> (registry family, cycle horizon, model key)
SCOPES: dict[str, tuple[str, int, str]] = {
    "g7rt|unemployment_rate|cy|forecast.bm_stack|benchmark.imf_weo|ae": ("weo.unemployment", 0, K.key("stack")),
}
INTERVAL_LABEL = "intervalle non calibré (couverture holdout 56 %) — non affiché"
DISPLAY = "point_only"
MONTH_KEY = "beat_macro_month"
CLAIM = (
    "REVA combination of the IMF WEO unemployment projection with REVA's nowcast from the OECD MEI real-time monthly "
    "unemployment rates (weight from past errors, shrunk toward 1/2) - USES THE IMF FORECAST AS AN INPUT. Holdout "
    "(2019-2025, 70 between-release monthly origins, G7): point AE x0.63 vs the WEO in force on the same date, Holm "
    "p 0.0044 (family of 7). Only the point is a claim; the stored interval is not calibrated and not displayed."
)
NO_OIL = LaggedMonthly("none", {}, {}, "unused for unemployment")


def confirmed_scopes() -> list[str]:
    led = S.load_yaml(S.REGISTRY_DIR / "holdout_ledger.yaml")
    out: list[str] = []
    for o in led.get("openings") or []:
        out += [k for k in o.get("confirmed") or [] if k in SCOPES]
    return sorted(set(out))


def month_label(m: int) -> str:
    return f"{m // 12:04d}-{m % 12 + 1:02d}"


def plan_month(issued_at: datetime, done: set[str], month: str | None = None) -> tuple[str, datetime] | str:
    """(month key, information cutoff) to issue, or the reason nothing is issued. Default month = the month of
    ``issued_at`` (cutoff = its 1st); an explicit ``month`` may be the next month (early issuance, cutoff =
    ``issued_at``); never a past month (its 1st-of-month state would be reconstructed after the fact)."""
    cur = mi(issued_at)
    m = cur if month is None else int(month[:4]) * 12 + int(month[5:7]) - 1
    key = month_label(m)
    if key in done:
        return f"already sealed for {key}"
    if m == cur:
        return key, month_start(m)
    if m == cur + 1:
        return key, issued_at
    return f"month {key} is neither the current nor the next month"


def issue_time_cases(cells: Any, cutoff: datetime, month: str) -> list[Any]:
    """G7 unemployment cases with the information at ``cutoff``, placed at the evaluation origin of ``month``
    (1st, 00:00 UTC), after every dev/holdout origin before it (their errors feed the second pass)."""
    origin = month_start(int(month[:4]) * 12 + int(month[5:7]) - 1)
    if cutoff > origin:
        raise ValueError("information cutoff after the month's evaluation origin")
    past = [t for t in S.origins("g7rt", "holdout", last=origin.date()) if t < origin]
    hist = [c for c in C.g7_cases(cells, NO_OIL, past) if c.variable == "unemployment_rate"]
    late = [c for c in hist if c.outcome_known_at is not None and cutoff < c.outcome_known_at <= origin]
    if late:
        raise RuntimeError("an outcome was published between the information cutoff and the month's origin")
    now = [c for c in C.g7_cases(cells, NO_OIL, [cutoff]) if c.variable == "unemployment_rate"]
    for c in now:
        c.info["information_cutoff"] = cutoff.isoformat()
        c.info["bucket_origin"] = origin.isoformat()
        c.origin = origin
    K.add_second_pass([*hist, *now], S.RULES["g7rt"])
    return now


def supplement(original: Mapping[str, Any], now_cases: Sequence[Any], *, issued_at: datetime,
               model_key: str, family: str, horizon: int, month: str | None = None) -> dict[str, Any] | None:  # fmt: skip
    """Pure: the supplementary batch for one original registry batch (same questions and pins)."""
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.service import BATCH_SCHEMA, LABEL

    by_geo = {c.geo: c for c in now_cases}
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    for q in original["questions"]:
        if q["family"] != family or int(q.get("cycle_horizon", -1)) != horizon or q["type"] != "quantity":
            continue
        geo = str(q.get("iso3") or q["geo"])
        if geo not in C.G7:
            continue
        c = by_geo.get(geo)
        p = c.preds.get(model_key) if c is not None else None
        if c is None or c.target != str(q["reference_period"]):
            rec = abstention(q, model_key, "no issue-time case for this country / target year")
        elif not isinstance(p, Predictive):
            rec = abstention(q, model_key, p.reason if isinstance(p, Abstain) else "no stack (missing input)")
        else:
            rec = _record(q, model_key, p, ())
            w = c.preds[C.WEO]
            rec["inputs"] = {
                "imf_weo_vintage_in_force": c.info.get("vintage_in_force"),
                "imf_weo_projection": w.point,
                "reva_nowcast_bridge": c.info["raw"].get("bridge"),
                "bridge_detail": c.info.get("bridge"),
                "stack_lambda": c.info.get("stack_lambda"),
                "oecd_mei_state": c.info.get("mei_state"),
                "bucket_origin": c.info.get("bucket_origin"),
                "information_cutoff": c.info.get("information_cutoff"),
            }
            rec["attribution"] = (
                "combination input: International Monetary Fund, World Economic Outlook (projection, vintage in force "
                "at issue); OECD MEI real-time editions (unemployment rate)"
            )
            rec["claim"] = CLAIM
            rec["display"] = DISPLAY
            rec["interval_status"] = INTERVAL_LABEL
        forecasts.append(rec)
        questions.append(q)
    if not forecasts:
        return None
    return {
        "schema": BATCH_SCHEMA,
        "label": LABEL,
        "origin": original["origin"],
        "cycle": original["cycle"],
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original.get("_sha256"),
        "statement": "Supplementary sealed batch (agent BEAT-MACRO): forecasts of a scope confirmed on the "
        "pre-registered BEAT-MACRO holdout (registry/forecasts/beat_macro/holdout_ledger.yaml), on the same questions "
        "and pinned table versions as the original batch, frozen at issued_at.",
        "specs": original["specs"],
        "families": {k: v for k, v in original["families"].items() if k == family},
        "models": [{"model_key": model_key, "version": S.MODEL_VERSION, "description": CLAIM}],
        "data": original["data"],
        "evidence": {
            "protocol": "registry/forecasts/beat_macro/protocol.yaml",
            "hypotheses": "registry/forecasts/beat_macro/hypotheses.yaml",
            "holdout": "registry/forecasts/beat_macro/holdout.json",
            "code_sha256": S.code_sha256(),
            "confirmed_scopes": confirmed_scopes(),
            "resolution_note": "the registry resolves on the first WEO vintage publishing the year as an actual "
            "(normally April of the next year); the BEAT-MACRO holdout used the first vintage on/after 1 October "
            "of the next year",
        },
        "beat_macro_month": month,
        "display": {"model": model_key, "show": DISPLAY, "interval_status": INTERVAL_LABEL},
        "code_digest": original.get("code_digest"),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


def done_months(conn: Any, model_key: str) -> set[str]:
    """Months already sealed for ``model_key`` (task_scope ``beat_macro_month``)."""
    from sqlalchemy import text

    from reva.evaluation.registry.service import ISSUE_KIND, SCOPE_KIND

    rows = conn.execute(
        text(
            """SELECT DISTINCT task_scope->>:mk AS m FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope ? :mk
                 AND metrics->>'model' LIKE :model"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "mk": MONTH_KEY, "model": f"{model_key}@%"},
    ).scalars()
    return {str(r) for r in rows if r}


def issue_monthly(engine: Any, store: Any, *, origin: str = "live", month: str | None = None,
                  dry_run: bool = False, actor: str = "worker:forecast_registry") -> dict[str, Any]:  # fmt: skip
    """Seals this month's supplementary batch(es) for every confirmed scope, unless already sealed (idempotent)."""
    from sqlalchemy import text

    from reva.adapters.postgres import repositories as repo
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _batches, _insert_eval
    from reva.evaluation.vintages import load_vintage_cells

    scopes = confirmed_scopes()
    if not scopes:
        return {"status": "no_confirmed_scope"}
    out: list[dict[str, Any]] = []
    for sk in scopes:
        family, hz, mk = SCOPES[sk]
        with engine.connect() as conn:
            issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            plan = plan_month(issued_at, done_months(conn, mk), month)
            if isinstance(plan, str):
                out.append({"scope": sk, "status": "skipped", "reason": plan})
                continue
            key, cutoff = plan
            cycle = P.cycle_of(issued_at.date())
            originals = [b for b in _batches(conn, origin) if b["cycle"] == cycle]
            mv = conn.execute(
                text("SELECT id::text FROM model.version WHERE model_key = :k AND version = :v"),
                {"k": mk, "v": S.MODEL_VERSION},
            ).scalar()
            cells = load_vintage_cells(conn, store)
        if mv is None and not dry_run:
            out.append({"scope": sk, "status": "model_not_seeded", "model": f"{mk}@{S.MODEL_VERSION}"})
            continue
        now_cases = issue_time_cases(cells, cutoff, key)
        for b in originals:
            man = dict(load_frozen_manifest(store, b["sha"]))
            if man.get("supplement_of"):
                continue
            man["_sha256"] = b["sha"]
            sup = _supplement(man, now_cases, issued_at, mk, family, hz, key)
            if sup is None:
                continue
            qids = sorted(q["question_id"] for q in sup["questions"])
            rec: dict[str, Any] = {"scope": sk, "month": key, "information_cutoff": cutoff.isoformat(),
                                   "supplement_of": b["sha"], "questions": len(qids),
                                   "points": {q["geo"]: f.get("point") for q, f in zip(sup["questions"], sup["forecasts"], strict=True)}}  # fmt: skip
            if dry_run:
                out.append({**rec, "status": "dry_run"})
                continue
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
                if key in done_months(conn, mk):  # concurrent run sealed it meanwhile
                    out.append({**rec, "status": "skipped", "reason": f"already sealed for {key}"})
                    continue
                sha = repo.store_json_artifact(conn, store, sup, "evaluation", schema_version="forecast-registry-batch/1",
                                               rights={"licence": "internal-derived; WEO values: IMF terms (attribution); OECD CC BY 4.0"})  # fmt: skip
                act = repo.simple_activity(
                    "evaluation.forecast_registry_issue", "evaluation", actor,
                    {"cycle": cycle, "origin": origin, "batch_sha256": sha, "supplement_of": b["sha"], MONTH_KEY: key},
                )  # fmt: skip
                repo.record_activity(conn, act, inputs=[(b["sha"], {"role": "original_batch"})], outputs=[sha])
                ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha,
                                          knowledge_cutoff=cutoff)  # fmt: skip
                scope = {"kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha,
                         "supplement_of": b["sha"], MONTH_KEY: key,
                         "question_keys": sorted(q["question_key"] for q in sup["questions"]), "question_ids": qids}  # fmt: skip
                _insert_eval(conn, mv, ctx, sha, "operational_as_known", scope,
                             {"kind": ISSUE_KIND, "status": "sealed", "label": sup["label"],
                              "model": f"{mk}@{S.MODEL_VERSION}", "issued_at": issued_at.isoformat(),
                              "batch_sha256": sha, "supplement_of": b["sha"], "n_questions": len(qids),
                              "n_issued": sum(1 for f in sup["forecasts"] if f["status"] == "issued"),
                              "n_abstained": sum(1 for f in sup["forecasts"] if f["status"] == "abstained")}, act.id)  # fmt: skip
            out.append({**rec, "status": "sealed", "batch_sha256": sha})
    return {"status": "done", "results": out}


def _supplement(man: Mapping[str, Any], now_cases: Sequence[Any], issued_at: datetime, mk: str, family: str,
                  hz: int, key: str) -> dict[str, Any] | None:  # fmt: skip
    return supplement(man, now_cases, issued_at=issued_at, model_key=mk, family=family, horizon=hz, month=key)


def mask_for_display(f: Mapping[str, Any]) -> dict[str, Any]:
    """Read-layer view of a sealed record: point only when the record says so (interval kept in the seal)."""
    if f.get("display") != DISPLAY:
        return dict(f)
    hidden = ("predictive", "quantiles", "interval")
    return {**{k: v for k, v in f.items() if k not in hidden}, "interval_status": f.get("interval_status")}


__all__ = [
    "CLAIM",
    "DISPLAY",
    "INTERVAL_LABEL",
    "SCOPES",
    "confirmed_scopes",
    "done_months",
    "issue_monthly",
    "issue_time_cases",
    "mask_for_display",
    "plan_month",
    "supplement",
]
