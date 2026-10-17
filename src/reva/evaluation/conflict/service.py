"""Conflict forecasting against the database: pinned inputs, back-test record, sealed issue (worker credentials).

* :func:`load_inputs` pins, at the database clock, the latest version of every input table (same selection rule as
  the forecast registry), reads only the indicators used (bytes verified against the artifact sha256 first) and the
  CShapes borders from the KG (``cshapes:shares_border_with``, live, not retracted).
* :func:`record_backtest` runs :func:`reva.evaluation.conflict.backtest.run_backtest` and stores its manifest like
  the registry's back-tests (``metrics.kind = registry_backtest``, scope ``forecast_registry`` with
  ``conflict: true``), one ``model.evaluation`` row per conflict model.
* :func:`issue_batch` generates the country-year questions of the current quarterly cycle (data through the last
  published UCDP year; reference years last+1 and last+2), forecasts them with the baselines, the attributed VIEWS
  reference (quantity only) and -- only for the (family, horizon) pairs the latest recorded back-test declared
  winners -- the candidate, and seals the batch in the registry format (``reva-forecast-registry-batch/1``), so the
  registry's API, leaderboard and daily resolver handle it like any other batch.
"""

from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl
import pyarrow.parquet as pq
from sqlalchemy import Engine, text

from reva.atlas.tables import ATLAS_ARROW_SCHEMA, NAMESPACE
from reva.evaluation.conflict import families as F
from reva.evaluation.conflict.backtest import LABEL as BACKTEST_LABEL
from reva.evaluation.conflict.backtest import build_examples, run_backtest
from reva.evaluation.conflict.models import (
    BASELINE_KEYS,
    CANDIDATE_KEYS,
    CONFLICT_MODELS,
    VIEWS_KEY,
    Example,
    Forecast,
    forecast_family,
)
from reva.evaluation.conflict.panel import ACD, COVARIATES, OUTCOME, Panel, build_panel
from reva.evaluation.conflict.views import ViewsRun, latest_covering, runs_from_frame
from reva.evaluation.registry import periods as P
from reva.evaluation.registry.data import latest_version
from reva.evaluation.registry.forecasts import FORECAST_SCHEMA, QUANTILE_LEVELS, num
from reva.evaluation.registry.questions import QUESTION_SCHEMA, RESOLUTION_RULE, country_label, question_id
from reva.evaluation.registry.service import (
    BACKTEST_KIND,
    BATCH_SCHEMA,
    ISSUE_KIND,
    ISSUE_LOCK,
    SCOPE_KIND,
    _check_origin,
    _code_digest,
    _insert_eval,
    _short_pin,
    issued_question_keys,
    model_version_ids,
)
from reva.kernel.errors import IntegrityFailure, MissingInput, ValidationProblem

VIEWS_TABLE = "atlas.views.cm_sb"
BORDER_PID = "cshapes:shares_border_with"
ISSUE_LABEL = (
    "Conflict-risk BASELINE forecasts (persistence, country base rate, conflict-history logistic), the VIEWS "
    "forecast attributed to VIEWS (quantity questions; REVA sums its 12 monthly point predictions), and the KG "
    "candidate only where it beat every baseline significantly on the as-of back-test. Country-year RISK ESTIMATES: "
    "not predictions of specific attacks, actors or persons; not validated models; not advice"
)
CONFLICT_SCOPE = "conflict"


def tables_used() -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    out[OUTCOME[0]].append(OUTCOME[1])
    out[ACD[0]].append(ACD[1])
    for dk, ind in COVARIATES.values():
        out[dk].append(ind)
    return dict(out)


def read_filtered(store: Any, sha: str, indicators: Sequence[str] | None) -> pl.DataFrame:
    """An atlas table restricted to some indicators; the bytes are verified against the sha256 first."""
    data = store.get(NAMESPACE, sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise IntegrityFailure("atlas table bytes do not match their sha256", sha256=sha)
    filters = [("indicator", "in", list(indicators))] if indicators else None
    table = pq.read_table(io.BytesIO(data), filters=filters)
    if not table.schema.equals(ATLAS_ARROW_SCHEMA, check_metadata=False):
        raise IntegrityFailure("atlas table does not have the atlas schema", sha256=sha)
    return pl.from_arrow(table)  # type: ignore[return-value]


def borders(conn: Any, origin: str) -> list[tuple[str, str, int, int]]:
    rows = conn.execute(
        text(
            """SELECT s.subject_qid, s.object_qid, s.start_time, s.end_time FROM kg.statement s
               WHERE s.property_pid = :p AND s.origin = :o
                 AND NOT EXISTS (SELECT 1 FROM kg.statement_retraction r WHERE r.statement_row_id = s.id)"""
        ),
        {"p": BORDER_PID, "o": origin},
    ).all()
    out = []
    for a, b, st, en in rows:
        if not (str(a).startswith("cshapes:gw:") and str(b).startswith("cshapes:gw:")) or not st or not en:
            continue
        out.append((str(a)[len("cshapes:") :], str(b)[len("cshapes:") :], int(str(st)[:4]), int(str(en)[:4])))
    return sorted(set(out))


def load_inputs(engine: Engine, store: Any, *, origin: str, with_views: bool = True) -> dict[str, Any]:
    with engine.connect() as conn:
        cutoff: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        pins = {dk: latest_version(conn, dk, origin=origin, cutoff=cutoff) for dk in tables_used()}
        vpin = latest_version(conn, VIEWS_TABLE, origin=origin, cutoff=cutoff) if with_views else None
        brd = borders(conn, origin)
    if pins[OUTCOME[0]] is None:
        raise MissingInput("the outcome table is not registered", dataset_key=OUTCOME[0])
    frames = {dk: read_filtered(store, p["artifact_sha256"], tables_used()[dk]) for dk, p in pins.items() if p}
    panel = build_panel(frames, brd)
    runs: dict[str, ViewsRun] = {}
    if vpin is not None:
        runs = runs_from_frame(read_filtered(store, vpin["artifact_sha256"], None))
    return {
        "cutoff": cutoff,
        "pins": {k: v for k, v in pins.items() if v},
        "missing_tables": sorted(k for k, v in pins.items() if v is None),
        "views_pin": vpin,
        "panel": panel,
        "views_runs": runs,
        "n_borders": len(brd),
    }


def _model_ids(conn: Any) -> dict[str, str]:
    mv = model_version_ids(conn)
    missing = sorted(set(CONFLICT_MODELS) - set(mv))
    if missing:
        raise MissingInput("conflict model versions are not registered (run `reva seed`)", missing=missing)
    return mv


def record_backtest(
    engine: Engine, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry"
) -> dict[str, Any]:
    from reva.adapters.postgres import repositories as repo

    _check_origin(origin)
    inp = load_inputs(engine, store, origin=origin)
    res = run_backtest(inp["panel"], views_runs=inp["views_runs"])
    used = {k: _short_pin(v) for k, v in inp["pins"].items()}
    if inp["views_pin"] is not None:
        used[VIEWS_TABLE] = _short_pin(inp["views_pin"])
    manifest = {
        "schema": "reva-forecast-registry-backtest/1",
        "conflict": True,
        "origin": origin,
        "run_at": inp["cutoff"].isoformat(),
        "years": inp["panel"].last_year - res["first_origin"],
        "data": {
            "tables": used,
            "missing_tables": inp["missing_tables"],
            "kg_borders": {"property": BORDER_PID, "statements": inp["n_borders"]},
        },
        "panel_notes": inp["panel"].notes,
        "models": [
            {"model_key": k, "version": v, "kind": kd, "description": d} for k, (v, kd, d) in CONFLICT_MODELS.items()
        ],
        "code_digest": _code_digest(),
        **res,
    }
    with engine.connect() as conn:
        mv = _model_ids(conn)
    with engine.begin() as conn:
        sha = repo.store_json_artifact(
            conn, store, manifest, "evaluation", schema_version="forecast-registry-backtest/1"
        )
        act = repo.simple_activity(
            "evaluation.conflict_backtest", "evaluation", actor, {"label": BACKTEST_LABEL, "sha256": sha}
        )
        repo.record_activity(
            conn, act, inputs=[(t["artifact_sha256"], {"role": "atlas_table"}) for t in used.values()], outputs=[sha]
        )
        ctx = repo.create_context(
            conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=inp["cutoff"]
        )
        scope = {
            "kind": SCOPE_KIND,
            "origin": origin,
            "backtest": True,
            CONFLICT_SCOPE: True,
            "families": sorted({f["family"] for f in res["families"]}),
            "vintage_mode": "final_vintage_pseudo_real_time",
        }
        for mk, (ver, _kind, _d) in sorted(CONFLICT_MODELS.items()):
            per = [
                {"family": f["family"], "horizon": f["horizon"], **{k: r[k] for k in r if k != "calibration"}}
                for f in res["families"]
                for r in f["results"]
                if r["model"] == mk
            ]
            if mk == VIEWS_KEY:
                per = [dict(x) for x in res["views"]["summary"]]
            _insert_eval(
                conn,
                mv[mk],
                ctx,
                sha,
                "revised_data_retrospective",
                scope,
                {
                    "kind": BACKTEST_KIND,
                    "label": BACKTEST_LABEL,
                    "model": f"{mk}@{ver}",
                    "backtest_sha256": sha,
                    "results": per,
                },
                act.id,
            )
    return {
        "status": "recorded",
        "backtest_sha256": sha,
        "winners": res["winners"],
        "n_scored": sum(r.get("n_scored", 0) for f in res["families"] for r in f["results"]),
        "views_runs": sorted(res["views"]["runs"]),
    }


def latest_backtest(conn: Any, store: Any, origin: str) -> tuple[str, dict[str, Any]] | None:
    from reva.evaluation.ledger import load_frozen_manifest

    r = conn.execute(
        text(
            """SELECT evaluation_manifest_sha256 AS sha FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope->>'origin' = :o
                 AND (task_scope->>'conflict')::boolean IS TRUE
               ORDER BY recorded_at DESC LIMIT 1"""
        ),
        {"k": BACKTEST_KIND, "sk": SCOPE_KIND, "o": origin},
    ).first()
    if r is None:
        return None
    return str(r[0]), load_frozen_manifest(store, str(r[0]))


# --------------------------------------------------------------------------- issue


def _record(q: Mapping[str, Any], mk: str, fc: Forecast, origin_year: int, h: int) -> dict[str, Any]:
    base: dict[str, Any] = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": mk}
    if fc.abstain:
        return {**base, "status": "abstained", "reason": fc.abstain}
    out: dict[str, Any] = {
        **base,
        "status": "issued",
        "origin_period": str(origin_year),
        "horizon_periods": h,
        "detail": dict(fc.detail),
    }
    if fc.sample:
        s = tuple(sorted(fc.sample))
        out["point"] = num(fc.point if fc.point is not None else s[len(s) // 2])
        out["predictive"] = {"family": "empirical", "sample": [num(v) for v in s]}
        import numpy as np

        out["quantiles"] = {f"{lv:g}": num(float(np.quantile(np.asarray(s), lv))) for lv in QUANTILE_LEVELS}
    elif fc.point is not None:
        out["point"] = num(fc.point)
        out["predictive"] = {"family": "point"}
    else:
        out["point"] = None  # probability-only record (registry: scored by Brier / log score only)
    if q["type"] == "binary":
        if fc.prob is None:
            return {**base, "status": "abstained", "reason": "no probability for this family"}
        out["prob_above"] = num(fc.prob)
    return out


def build_questions_and_forecasts(
    panel: Panel,
    views_runs: Mapping[str, ViewsRun],
    *,
    cycle: str,
    issued_on: date,
    source_pin: Mapping[str, Any],
    winners: set[tuple[str, int]],
    already: set[str],
) -> dict[str, Any]:
    """Pure: questions of one cycle with every forecaster's frozen record."""
    last = panel.last_year
    anchor_year = P.cycle_anchor(cycle).year
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    fam_docs: dict[str, Any] = {}
    views_meta: dict[str, Any] = {}
    for h in (1, 2):
        target = last + h
        ch = target - anchor_year
        if ch not in (0, 1):
            skipped.append({"reason": f"target year {target} outside the cycle's annual horizons", "horizon": h})
            continue
        ex = build_examples(panel, h)
        vr = latest_covering(dict(views_runs), target, issued_on.isoformat()) if views_runs else None
        if vr is not None:
            views_meta[str(target)] = {"run": vr.run, "release_date": vr.release_date}
        for fam in F.FAMILIES:
            train = [
                e
                for t in range(1993, last - h + 1)
                for e in ex.get(t, ())
                if e.target is not None and fam.applies(e.last)
            ]
            test: list[Example] = []
            for e in ex.get(last, ()):
                iso = panel.iso(e.geo, last)
                if iso is None:
                    skipped.append({"family": fam.key, "geo": e.geo, "reason": "no ISO3 for this GW code in the table"})
                    continue
                if not fam.applies(e.last):
                    continue
                test.append(e)
            models = list(BASELINE_KEYS) + [c for c in CANDIDATE_KEYS if (fam.key, h) in winners]
            fcs = {mk: forecast_family(mk, fam, panel, h, train, test) for mk in models}
            countries: list[str] = []
            for i, e in enumerate(test):
                iso = panel.iso(e.geo, last)
                assert iso is not None
                q = _question(fam, e, iso, target, ch, cycle, source_pin)
                if q["question_key"] in already:
                    continue
                recs = [_record(q, mk, fcs[mk][i], last, h) for mk in models]
                if fam.kind == "quantity":
                    recs.append(_views_record(q, vr, e.geo, target, last, h))
                else:
                    recs.append(
                        {
                            "schema": FORECAST_SCHEMA,
                            "question_id": q["question_id"],
                            "model": VIEWS_KEY,
                            "status": "abstained",
                            "reason": "VIEWS forecasts country-months; a calendar-year probability is not derivable from "
                            "monthly probabilities without a dependence model",
                        }
                    )
                if all(r["status"] == "abstained" for r in recs):
                    skipped.append(
                        {"family": fam.key, "geo": iso, "period": str(target), "reason": "every model abstained"}
                    )
                    continue
                questions.append(q)
                forecasts.extend(recs)
                countries.append(iso)
            if countries:
                doc = fam_docs.get(fam.key) or F.family_doc(fam, [])
                doc["countries"] = sorted(set(doc["countries"]) | set(countries))
                fam_docs[fam.key] = doc
    return {
        "questions": questions,
        "forecasts": forecasts,
        "skipped": skipped,
        "families": fam_docs,
        "views_runs_used": views_meta,
    }


def _question(
    fam: F.ConflictFamily, e: Example, iso: str, target: int, ch: int, cycle: str, source_pin: Mapping[str, Any]
) -> dict[str, Any]:
    en, fr = country_label(iso)
    last = {"period": str(e.t), "value": num(e.last)}
    pend = date(target + 1, 1, 1)
    q: dict[str, Any] = {
        "schema": QUESTION_SCHEMA,
        "question_key": f"{cycle}|{fam.key}|{iso}|{target}|{fam.kind}",
        "cycle": cycle,
        "family": fam.key,
        "concept_id": None,
        "label_fr": fam.label_fr,
        "label_en": fam.label_en,
        "definition": fam.definition(),
        "definition_source": F.DEFINITIONS_SOURCE,
        "geo": iso,
        "iso3": iso,
        "geo_label_en": en,
        "geo_label_fr": fr,
        "provider_geo": e.geo,
        "frequency": "A",
        "reference_period": str(target),
        "period_start": date(target, 1, 1).isoformat(),
        "period_end": pend.isoformat(),
        "cycle_horizon": ch,
        "type": fam.kind,
        "unit": F.UNIT,
        "interval_level": None,
        "threshold": str(fam.threshold) if fam.threshold is not None else None,
        "threshold_rule": "above_fixed" if fam.kind == "binary" else None,
        "threshold_basis": (
            {"period": "fixed", "value": str(fam.threshold), "meaning": f"at least {fam.threshold + 1} deaths"}
            if fam.threshold is not None
            else None
        ),
        "condition": ({"last_observed_between": list(fam.condition), "last_observed": last} if fam.condition else None),
        "last_observed": last,
        "risk_note": F.RISK_NOTE,
        "resolution": {
            "source": F.DATASET_KEY,
            "indicator": F.INDICATOR,
            "indicator_parts": None,
            "geo_match": "iso3",
            "provider_geo": None,
            "value_rule": "cell",
            "rule": RESOLUTION_RULE,
            "expected_date": (pend + timedelta(days=F.PUBLICATION_LAG_DAYS)).isoformat(),
            "void_after": (pend + timedelta(days=F.VOID_AFTER_DAYS)).isoformat(),
            "note": "First atlas.ucdp.orgvio_cy version registered after the seal publishing this country-year "
            "(UCDP annual release; the atlas.ucdp connector must be moved to the new UCDP version when it is out).",
            "issue_table": dict(source_pin),
        },
    }
    q["text_fr"], q["text_en"] = F.question_texts(fam, fr, en, iso, str(target), last)
    q["question_id"] = question_id(q)
    return q


def _views_record(
    q: Mapping[str, Any], vr: ViewsRun | None, gw: str, target: int, origin_year: int, h: int
) -> dict[str, Any]:
    base = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": VIEWS_KEY}
    if vr is None:
        return {**base, "status": "abstained", "reason": "no VIEWS run released before the issue covers this year"}
    v = vr.annual(gw, target)
    if v is None:
        return {**base, "status": "abstained", "reason": f"VIEWS run {vr.run} has no 12 monthly values for {gw}"}
    return {
        **base,
        "status": "issued",
        "point": num(v),
        "predictive": {"family": "point"},
        "origin_period": str(origin_year),
        "horizon_periods": h,
        "attribution": "VIEWS (Uppsala University / PRIO), run "
        f"{vr.run} released {vr.release_date}; REVA summed its 12 monthly point predictions (main_mean) for {target}",
        "source_cells": [vr.refs[(gw, target, m)] for m in (1, 12) if (gw, target, m) in vr.refs],
        "detail": {"views_run": vr.run, "release_date": vr.release_date, "aggregation": "sum of 12 monthly points"},
    }


def issue_batch(
    engine: Engine, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry"
) -> dict[str, Any]:
    from reva.adapters.postgres import repositories as repo

    _check_origin(origin)
    inp = load_inputs(engine, store, origin=origin)
    issued_at: datetime = inp["cutoff"]
    cycle = P.cycle_of(issued_at.date())
    with engine.connect() as conn:
        mv = _model_ids(conn)
        already = issued_question_keys(conn, origin, cycle)
        bt = latest_backtest(conn, store, origin)
    winners = {(w["family"], int(w["horizon"])) for w in (bt[1]["winners"] if bt else [])}
    pin = inp["pins"][F.DATASET_KEY]
    built = build_questions_and_forecasts(
        inp["panel"],
        inp["views_runs"],
        cycle=cycle,
        issued_on=issued_at.date(),
        source_pin=_short_pin(pin),
        winners=winners,
        already=already,
    )
    if not built["questions"]:
        return {"status": "nothing_to_issue", "cycle": cycle, "already_issued_in_cycle": len(already)}
    used = {k: _short_pin(v) for k, v in inp["pins"].items()}
    if inp["views_pin"] is not None:
        used[VIEWS_TABLE] = _short_pin(inp["views_pin"])
    manifest = {
        "schema": BATCH_SCHEMA,
        "label": ISSUE_LABEL,
        "conflict": True,
        "origin": origin,
        "cycle": cycle,
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "These questions and forecasts were frozen at issued_at (database clock) from the pinned atlas "
        "table versions, sealed by the sha256 of this manifest, and are scored only against values published "
        "in table versions registered after issued_at. Country-year risk estimates only.",
        "specs": {"version": "conflict-1.0.0", "sha256": None, "interval_level": None},
        "families": built["families"],
        "models": [
            {"model_key": k, "version": v, "description": f"{kd}: {d}"}
            for k, (v, kd, d) in sorted(CONFLICT_MODELS.items())
        ],
        "data": {
            "tables": used,
            "selection_rule": "per dataset_key: latest evidence.dataset version at issued_at",
            "kg_borders": {"property": BORDER_PID, "statements": inp["n_borders"]},
        },
        "candidate_rule": {
            "backtest_sha256": bt[0] if bt else None,
            "winners": sorted([list(w) for w in winners]),
            "note": "the candidate forecasts only the (family, horizon) pairs its latest as-of back-test declared "
            "winners; horizon h = years after the last published UCDP year",
        },
        "views_runs_used": built["views_runs_used"],
        "code_digest": _code_digest(),
        "questions": built["questions"],
        "forecasts": built["forecasts"],
        "generation": {"skipped": built["skipped"], "pending_families": [], "notes": inp["panel"].notes},
    }
    from reva.evaluation.ledger import manifest_sha256

    keys = sorted(q["question_key"] for q in built["questions"])
    by_type: dict[str, int] = defaultdict(int)
    for q in built["questions"]:
        by_type[q["type"]] += 1
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        if issued_question_keys(conn, origin, cycle) & set(keys):
            raise ValidationProblem("another issue of this cycle overlapped; re-run to issue what is left")
        sha = repo.store_json_artifact(
            conn,
            store,
            manifest,
            "evaluation",
            schema_version="forecast-registry-batch/1",
            rights={
                "licence": "internal-derived; each pinned atlas table keeps its source rights; VIEWS forecasts "
                "attributed to VIEWS (licence not stated by the provider: licence_review)"
            },
        )
        if sha != manifest_sha256(manifest):
            raise IntegrityFailure("stored batch manifest hash differs from its canonical hash")
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue",
            "evaluation",
            actor,
            {"cycle": cycle, "origin": origin, "batch_sha256": sha, "label": ISSUE_LABEL, "questions": len(keys)},
        )
        repo.record_activity(
            conn,
            act,
            inputs=[(t["artifact_sha256"], {"role": "atlas_table", "dataset_key": k}) for k, t in sorted(used.items())],
            outputs=[sha],
        )
        ctx = repo.create_context(
            conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=issued_at
        )
        scope = {
            "kind": SCOPE_KIND,
            "origin": origin,
            "cycle": cycle,
            "batch_sha256": sha,
            "question_keys": keys,
            CONFLICT_SCOPE: True,
        }
        ids: dict[str, str] = {}
        for mk, (ver, _kd, _d) in sorted(CONFLICT_MODELS.items()):
            mine = [f for f in built["forecasts"] if f["model"] == mk]
            ids[mk] = str(
                _insert_eval(
                    conn,
                    mv[mk],
                    ctx,
                    sha,
                    "operational_as_known",
                    scope,
                    {
                        "kind": ISSUE_KIND,
                        "status": "sealed",
                        "label": ISSUE_LABEL,
                        "model": f"{mk}@{ver}",
                        "issued_at": issued_at.isoformat(),
                        "batch_sha256": sha,
                        "n_questions": len(keys),
                        "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                        "n_abstained": sum(1 for f in mine if f["status"] == "abstained"),
                        "by_type": dict(by_type),
                    },
                    act.id,
                )
            )
        repo.emit_outbox(
            conn, "evaluation.forecast_registry_issued", {"batch_sha256": sha, "cycle": cycle, "origin": origin}
        )
    return {
        "status": "issued",
        "batch_sha256": sha,
        "cycle": cycle,
        "issued_at": issued_at.isoformat(),
        "n_questions": len(keys),
        "by_family": dict(
            sorted({k: sum(1 for q in built["questions"] if q["family"] == k) for k in built["families"]}.items())
        ),
        "n_forecasts": sum(1 for f in built["forecasts"] if f["status"] == "issued"),
        "n_abstained": sum(1 for f in built["forecasts"] if f["status"] == "abstained"),
        "candidate_winners": sorted([list(w) for w in winners]),
        "views_runs_used": built["views_runs_used"],
        "evaluation_ids": ids,
    }


__all__ = [
    "ISSUE_LABEL",
    "VIEWS_TABLE",
    "build_questions_and_forecasts",
    "issue_batch",
    "latest_backtest",
    "load_inputs",
    "record_backtest",
]
