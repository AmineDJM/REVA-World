"""Health forecast registry, database part: stored back-tests, sealed issues, resolution and scoring.

Same sealing discipline as the V5 registry (:mod:`reva.evaluation.registry.service`): every document (back-test,
issue batch, resolution) is stored content-addressed in the ``evaluation`` namespace and its sha256 recorded in
immutable ``model.evaluation`` rows (one per model) inside a new evaluation context, with a provenance activity whose
inputs are the pinned atlas table artifacts. Scope kind ``forecast_registry_health``; metrics kinds
``health_backtest`` / ``health_issue`` / ``health_score``.

Replay modes: as-of hub back-tests use the data vintages published for each round -> ``available_then_reconstruction``;
final-vintage back-tests (FluNet, ERVISS, GHE: no vintages available) -> ``revised_data_retrospective`` and are
labelled so; issues -> ``operational_as_known``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.evaluation.health_registry import core as C
from reva.evaluation.ledger import load_frozen_manifest, manifest_sha256
from reva.evaluation.registry.data import FrameCache, latest_version, versions_between
from reva.evaluation.registry.service import _code_digest, _insert_eval
from reva.kernel.errors import IntegrityFailure, MissingInput, ValidationProblem
from reva.models.forecasting import health_quantile as HQ

SCOPE_KIND = "forecast_registry_health"
BACKTEST_KIND = "health_backtest"
ISSUE_KIND = "health_issue"
SCORE_KIND = "health_score"
BACKTEST_SCHEMA = "reva-health-backtest/1"
BATCH_SCHEMA = "reva-health-batch/1"
RESOLUTION_SCHEMA = "reva-health-resolution/1"
ISSUE_LOCK = 7262106
ORIGINS = ("live", "test_fixture")
LABEL = (
    "Health forecast registry: BASELINE quantile forecasts (persistence, seasonal climatology), the attributed US CDC "
    "hub ensemble where declared, and only candidate models that significantly beat every baseline on the stored "
    "back-test; prospective, frozen and sealed before resolution; not validated models, not medical or public-health "
    "advice"
)
BACKTEST_LABELS = {
    "as_of_vintages": "as-of back-test: each round uses the data vintage published for that round (hub target-data "
    "archive / as_of time series); truth = the latest published target table",
    "final_vintage": "final-vintage pseudo-real-time back-test: every fit uses only periods up to its origin (minus the "
    "publication lag) but values are the final (revised) vintage, not the data as published then; never real-time skill",
}
REPLAY = {"as_of_vintages": "available_then_reconstruction", "final_vintage": "revised_data_retrospective"}


def _clock(conn: Any, origin: str, at: datetime | None) -> datetime:
    """The database clock; a fixed instant only for test_fixture runs."""
    if at is not None:
        if origin != "test_fixture":
            raise ValidationProblem("a clock override is only allowed for test_fixture runs")
        return at
    now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    return now


def _check_origin(origin: str) -> str:
    if origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    return origin


def all_models() -> dict[str, str]:
    return {**{k: v[0] for k, v in C.MODELS.items()}, **{k: v[0] for k, v in C.REFERENCE_MODELS.items()}}


def model_version_ids(conn: Any) -> dict[str, str]:
    models = all_models()
    rows = conn.execute(
        text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
        {"k": sorted(models)},
    ).mappings()
    found = {r["model_key"]: r["id"] for r in rows if models[r["model_key"]] == r["version"]}
    missing = sorted(set(models) - set(found))
    if missing:
        raise MissingInput(
            "health registry model versions are not registered (run `reva seed` as the owner)", missing=missing
        )
    return found


def _short_pin(pin: Mapping[str, Any]) -> dict[str, Any]:
    return {k: pin[k] for k in ("dataset_key", "dataset_id", "artifact_sha256", "recorded_at")}


def _families(specs: C.SpecSet, families: Sequence[str] | None) -> list[C.Family]:
    fams = [f for f in specs.families if not families or f.key in families]
    unknown = sorted(set(families or ()) - {f.key for f in specs.families})
    if unknown:
        raise ValidationProblem("unknown health question family", unknown=unknown)
    return fams


def _tables(fam: C.Family) -> list[str]:
    keys = [fam.dataset_key]
    if fam.reference:
        keys.append(str(fam.reference["dataset_key"]))
    if fam.backtest.get("vintages_dataset_key"):
        keys.append(str(fam.backtest["vintages_dataset_key"]))
    return keys


def _seal(
    conn: Any,
    store: Any,
    doc: Mapping[str, Any],
    *,
    schema_version: str,
    activity: str,
    actor: str,
    params: Mapping[str, Any],
    inputs: Sequence[tuple[str, Mapping[str, Any]]],
    origin: str,
    cutoff: datetime,
    parent: UUID | None = None,
) -> tuple[str, UUID, UUID]:
    from reva.adapters.postgres import repositories as repo

    sha = repo.store_json_artifact(
        conn,
        store,
        dict(doc),
        "evaluation",
        schema_version=schema_version,
        rights={
            "licence": "internal-derived; each pinned atlas table keeps its source rights (hub attribution requests, ERVISS licence_review)"
        },
    )
    if sha != manifest_sha256(doc):
        raise IntegrityFailure("stored manifest hash differs from its canonical hash")
    act = repo.simple_activity(activity, "evaluation", actor, {**dict(params), "manifest_sha256": sha})
    repo.record_activity(conn, act, inputs=list(inputs), outputs=[sha])
    kw: dict[str, Any] = {"mode": "evaluation", "origin": origin, "manifest_sha256": sha, "knowledge_cutoff": cutoff}
    if parent is not None:
        kw["parent_context_id"] = parent
    ctx = repo.create_context(conn, **kw)
    return sha, ctx, act.id


# --------------------------------------------------------------------------- back-tests


def build_backtest(fam: C.Family, frames: Mapping[str, Any], *, alpha: float) -> dict[str, Any]:
    """Pure: the back-test result of one family from its pinned frames (``dataset_key`` -> frame)."""
    series, _labels, notes = C.extract_series(frames[fam.dataset_key], fam)
    kind = str(fam.backtest.get("kind"))
    skipped: list[dict[str, Any]] = []
    if kind == "as_of_vintages":
        assert fam.reference is not None
        vint = C.extract_vintages(frames[str(fam.backtest["vintages_dataset_key"])], fam)
        ens = C.extract_ensemble(frames[str(fam.reference["dataset_key"])], fam)
        tasks, skipped = C.as_of_tasks(fam, series, vint, ens)
        res = C.run_backtest(fam, tasks, alpha=alpha)
        res["vintages"] = [v.isoformat() for v in sorted(vint)]
    elif kind == "final_vintage":
        res = C.run_backtest(fam, C.final_vintage_tasks(fam, series), alpha=alpha)
    else:
        raise ValidationProblem("unknown back-test kind", family=fam.key, kind=kind)
    out: dict[str, Any] = C.finite(
        {**res, "kind": kind, "label": BACKTEST_LABELS[kind], "skipped_rounds": skipped, "notes": notes}
    )
    return out


def run_backtests(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    families: Sequence[str] | None = None,
    specs: C.SpecSet | None = None,
    actor: str = "worker:health_registry",
) -> dict[str, Any]:
    """Back-test every (selected) family whose tables are registered and store one sealed result per family."""
    _check_origin(origin)
    specs = specs or C.load_specs()
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        mv = model_version_ids(conn)
    cache = FrameCache(store, size=4)
    for fam in _families(specs, families):
        with engine.connect() as conn:
            pins = {k: latest_version(conn, k, origin=origin, cutoff=now) for k in _tables(fam)}
        missing = sorted(k for k, p in pins.items() if p is None)
        if missing:
            out.append({"family": fam.key, "status": "awaiting_table", "missing": missing})
            continue
        frames = {k: cache.get(p["artifact_sha256"]) for k, p in pins.items() if p}
        res = build_backtest(fam, frames, alpha=specs.alpha)
        if not res["models"]:
            out.append({"family": fam.key, "status": "no_tasks", "n_tasks": res["n_tasks"]})
            continue
        kind = res["kind"]
        doc = {
            "schema": BACKTEST_SCHEMA,
            "origin": origin,
            "family": fam.key,
            "recorded_at": now.isoformat(),
            "replay_mode": REPLAY[kind],
            "specs": {"version": specs.version, "sha256": specs.sha256},
            "family_spec": dict(fam.raw),
            "models": {k: v for k, v in all_models().items()},
            "data": {k: _short_pin(p) for k, p in pins.items() if p},
            "code_digest": _code_digest(),
            "result": res,
        }
        with engine.begin() as conn:
            sha, ctx, act = _seal(
                conn,
                store,
                doc,
                schema_version="health-backtest/1",
                activity="evaluation.health_registry_backtest",
                actor=actor,
                params={"family": fam.key, "origin": origin, "label": res["label"]},
                inputs=[
                    (p["artifact_sha256"], {"role": "atlas_table", "dataset_key": k})
                    for k, p in sorted(pins.items())
                    if p
                ],
                origin=origin,
                cutoff=now,
            )
            scope = {"kind": SCOPE_KIND, "origin": origin, "family": fam.key, "backtest_sha256": sha}
            for mk, st in sorted(res["models"].items()):
                _insert_eval(
                    conn,
                    mv[mk],
                    ctx,
                    sha,
                    REPLAY[kind],
                    scope,
                    {
                        "kind": BACKTEST_KIND,
                        "label": res["label"],
                        "family": fam.key,
                        "model": f"{mk}@{all_models()[mk]}",
                        "n": st["n"],
                        "mean_wis": st["mean_wis"],
                        "coverage90": st["coverage90"],
                        "winner": mk in res["winners"],
                    },
                    act,
                )
        out.append(
            {
                "family": fam.key,
                "status": "recorded",
                "backtest_sha256": sha,
                "kind": kind,
                "n_tasks": res["n_tasks"],
                "mean_wis": {k: round(v["mean_wis"], 4) for k, v in res["models"].items()},
                "winners": res["winners"],
            }
        )
    return {"origin": origin, "families": out}


def latest_backtest(conn: Any, family: str, origin: str) -> tuple[str, list[str]] | None:
    r = (
        conn.execute(
            text(
                """SELECT evaluation_manifest_sha256 AS sha, bool_or((metrics->>'winner')::boolean) AS any_w,
                      array_agg(metrics->>'model') FILTER (WHERE (metrics->>'winner')::boolean) AS winners,
                      max(recorded_at) AS at
               FROM model.evaluation WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk
                 AND task_scope->>'origin' = :o AND task_scope->>'family' = :f
               GROUP BY evaluation_manifest_sha256 ORDER BY max(recorded_at) DESC LIMIT 1"""
            ),
            {"k": BACKTEST_KIND, "sk": SCOPE_KIND, "o": origin, "f": family},
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    return str(r["sha"]), sorted(str(w).split("@", 1)[0] for w in (r["winners"] or []))


# --------------------------------------------------------------------------- issue


def issued_question_keys(conn: Any, origin: str) -> set[str]:
    rows = conn.execute(
        text(
            """SELECT DISTINCT jsonb_array_elements_text(task_scope->'question_keys') FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope->>'origin' = :o"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "o": origin},
    ).scalars()
    return set(rows)


def build_batch(
    specs: C.SpecSet,
    fams: Sequence[C.Family],
    pins: Mapping[str, Mapping[str, Any] | None],
    frames: Mapping[str, Any],
    winners: Mapping[str, tuple[str, list[str]] | None],
    *,
    issued_at: datetime,
    origin: str,
    already: set[str],
) -> dict[str, Any]:
    """Pure: questions and frozen forecasts of the current round of each family."""
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    used: dict[str, dict[str, Any]] = {}
    fam_doc: dict[str, Any] = {}
    today = issued_at.date()
    for fam in fams:
        pin = pins.get(fam.dataset_key)
        if pin is None:
            pending.append({"family": fam.key, "reason": f"table {fam.dataset_key} not registered yet"})
            continue
        series, labels, notes = C.extract_series(frames[pin["artifact_sha256"]], fam)
        qs, sk = C.generate_questions(fam, series, labels, today=today, source_pin=_short_pin(pin))
        skipped.extend(sk + notes)
        bt = winners.get(fam.key)
        issued_models = list(fam.baselines) + [m for m in (bt[1] if bt else []) if m in fam.candidates]
        ens = None
        if fam.reference:
            rp = pins.get(str(fam.reference["dataset_key"]))
            ens = C.extract_ensemble(frames[rp["artifact_sha256"]], fam) if rp else {}
            if rp:
                used[str(fam.reference["dataset_key"])] = _short_pin(rp)
        n_before = len(questions)
        for q in qs:
            if q["question_key"] in already:
                continue
            key = C.series_key(q["geo"], q["cause"])
            hist = C.as_floats(series[key])
            target = hist[-1][0] + int(q["steps_from_last_observed"])
            res = C.run_models(fam, hist, target)
            fcs = [C.forecast_record(q, m, res[m], hist) for m in issued_models]
            if all(f["status"] == "abstained" for f in fcs):
                skipped.append(
                    {"family": fam.key, "question_key": q["question_key"], "reason": "every issued model abstained"}
                )
                continue
            if fam.reference:
                fcs.append(C.reference_record(q, fam, (ens or {}).get((q["round"], q["geo"], target))))
            q["issued_models"] = issued_models + ([str(fam.reference["model_key"])] if fam.reference else [])
            q["backtest_sha256"] = bt[0] if bt else None
            questions.append(q)
            forecasts.extend(fcs)
        if len(questions) > n_before:
            used[fam.dataset_key] = _short_pin(pin)
            fam_doc[fam.key] = {
                **{k: v for k, v in fam.raw.items() if k != "panel"},
                "geos": list(fam.geos) if fam.geos is not None else "all",
                "issued_models": issued_models,
                "backtest_sha256": bt[0] if bt else None,
                "winners": bt[1] if bt else [],
            }
    return {
        "schema": BATCH_SCHEMA,
        "label": LABEL,
        "origin": origin,
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "These questions and quantile forecasts were frozen at issued_at (database clock) from the pinned "
        "atlas table versions, sealed by the sha256 of this manifest, and are scored only against values published in "
        "table versions registered after issued_at. Hub ensemble forecasts are attributed to their hubs.",
        "specs": {"version": specs.version, "sha256": specs.sha256},
        "levels": [C.num(lv) for lv in C.LEVELS],
        "families": fam_doc,
        "models": all_models(),
        "data": {"tables": used, "selection_rule": "per dataset_key: latest evidence.dataset version at issued_at"},
        "code_digest": _code_digest(),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": skipped, "pending_families": pending},
    }


def issue_round(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    families: Sequence[str] | None = None,
    specs: C.SpecSet | None = None,
    actor: str = "worker:health_registry",
    at: datetime | None = None,
) -> dict[str, Any]:
    """Generate, forecast and seal the current round's questions not issued yet (idempotent per round).
    ``at`` replaces the database clock for test_fixture runs only (time-independent tests)."""
    _check_origin(origin)
    specs = specs or C.load_specs()
    fams = _families(specs, families)
    with engine.connect() as conn:
        issued_at = _clock(conn, origin, at)
        mv = model_version_ids(conn)
        already = issued_question_keys(conn, origin)
        pins: dict[str, dict[str, Any] | None] = {}
        for fam in fams:
            for k in [fam.dataset_key] + ([str(fam.reference["dataset_key"])] if fam.reference else []):
                if k not in pins:
                    pins[k] = latest_version(conn, k, origin=origin, cutoff=issued_at)
        winners = {fam.key: latest_backtest(conn, fam.key, origin) for fam in fams}
    cache = FrameCache(store, size=len(pins) + 1)
    frames = {p["artifact_sha256"]: cache.get(p["artifact_sha256"]) for p in pins.values() if p}
    manifest = C.finite(
        build_batch(specs, fams, pins, frames, winners, issued_at=issued_at, origin=origin, already=already)
    )
    report = {
        "origin": origin,
        "issued_at": issued_at.isoformat(),
        "n_questions": len(manifest["questions"]),
        "n_forecasts": sum(1 for f in manifest["forecasts"] if f["status"] == "issued"),
        "n_abstained": sum(1 for f in manifest["forecasts"] if f["status"] == "abstained"),
        "pending_families": manifest["generation"]["pending_families"],
        "n_skipped": len(manifest["generation"]["skipped"]),
        "by_family": dict(_count(q["family"] for q in manifest["questions"])),
    }
    if not manifest["questions"]:
        return {"status": "nothing_to_issue", **report}
    keys = sorted(q["question_key"] for q in manifest["questions"])
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        if issued_question_keys(conn, origin) & set(keys):
            raise ValidationProblem("another health issue overlapped; re-run to issue what is left")
        sha, ctx, act = _seal(
            conn,
            store,
            manifest,
            schema_version="health-batch/1",
            activity="evaluation.health_registry_issue",
            actor=actor,
            params={"origin": origin, "questions": len(keys), "label": LABEL},
            inputs=[
                (t["artifact_sha256"], {"role": "atlas_table", "dataset_key": k})
                for k, t in sorted(manifest["data"]["tables"].items())
            ],
            origin=origin,
            cutoff=issued_at,
        )
        scope = {"kind": SCOPE_KIND, "origin": origin, "batch_sha256": sha, "question_keys": keys}
        ids: dict[str, str] = {}
        for mk, ver in sorted(all_models().items()):
            mine = [f for f in manifest["forecasts"] if f["model"] == mk]
            if not mine:
                continue
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
                        "label": LABEL,
                        "model": f"{mk}@{ver}",
                        "issued_at": issued_at.isoformat(),
                        "batch_sha256": sha,
                        "n_questions": len(keys),
                        "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                        "n_abstained": sum(1 for f in mine if f["status"] == "abstained"),
                    },
                    act,
                )
            )
        from reva.adapters.postgres import repositories as repo

        repo.emit_outbox(conn, "evaluation.health_registry_issued", {"batch_sha256": sha, "origin": origin})
    return {"status": "issued", "batch_sha256": sha, "context_id": str(ctx), "evaluation_ids": ids, **report}


def _count(xs: Any) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return out


# --------------------------------------------------------------------------- resolve


def _batches(conn: Any, origin: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """SELECT DISTINCT ON (evaluation_manifest_sha256) evaluation_manifest_sha256 AS sha,
                      context_id::text AS context_id, recorded_at
               FROM model.evaluation WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk
                 AND task_scope->>'origin' = :o
               ORDER BY evaluation_manifest_sha256, recorded_at"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "o": origin},
    ).mappings()
    return sorted((dict(r) for r in rows), key=lambda r: r["recorded_at"])


def handled_questions(conn: Any, batch_sha: str) -> set[str]:
    rows = conn.execute(
        text(
            """SELECT DISTINCT jsonb_array_elements_text(task_scope->'question_ids') FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'batch_sha256' = :s"""
        ),
        {"k": SCORE_KIND, "s": batch_sha},
    ).scalars()
    return set(rows)


def _value_for(q: Mapping[str, Any], series: C.Series, fam: C.Family) -> C.Cell | None:
    cells = series.get(C.series_key(str(q["geo"]), q.get("cause")))
    if not cells:
        return None
    s = date.fromisoformat(str(q["period_start"]))
    e = date.fromisoformat(str(q["period_end"]))
    idx = C.idx_of_cell(fam, s, e)
    return None if idx is None else cells.get(idx)


def resolve_batch(
    manifest: Mapping[str, Any],
    handled: set[str],
    issue_series: Mapping[str, C.Series],
    later: Mapping[str, Sequence[tuple[Mapping[str, Any], C.Series]]],
    fams: Mapping[str, C.Family],
    *,
    today: date,
) -> dict[str, Any]:
    """Pure: outcomes and scores of one sealed batch's pending, ended questions."""
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in manifest["forecasts"]:
        by_q[f["question_id"]].append(f)
    outcomes: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for q in manifest["questions"]:
        qid = q["question_id"]
        if qid in handled or date.fromisoformat(q["period_end"]) > today:
            continue
        fam = fams[q["family"]]
        if _value_for(q, issue_series.get(fam.key, {}), fam) is not None:
            outcomes.append(
                {
                    "question_id": qid,
                    "status": "not_prospective",
                    "reason": "the pinned issue version already published this value",
                }
            )
            continue
        found = None
        for pin, series in later.get(fam.key, ()):
            cell = _value_for(q, series, fam)
            if cell is not None:
                found = (pin, cell)
                break
        if found is None:
            if today >= date.fromisoformat(q["resolution"]["void_after"]):
                outcomes.append(
                    {"question_id": qid, "status": "void", "reason": "no observed value published by the void date"}
                )
            continue
        pin, cell = found
        outcomes.append(
            {
                "question_id": qid,
                "status": "resolved",
                "value": cell.value,
                "source": {**_short_pin(pin), "cells": [dict(r) for r in cell.refs]},
            }
        )
        y = float(cell.value)
        for f in by_q[qid]:
            if f["status"] != "issued":
                continue
            sc = HQ.score([float(x) for x in f["levels"]], [float(x) for x in f["quantiles"]], y)
            scores.append({"question_id": qid, "model": f["model"], **sc})
    return {"outcomes": outcomes, "scores": scores}


def resolve_due(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    actor: str = "worker:health_registry",
    at: datetime | None = None,
) -> dict[str, Any]:
    """Resolve and score every sealed batch's ended questions not handled before (``at``: test_fixture clock)."""
    _check_origin(origin)
    cache = FrameCache(store, size=4)
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        now = _clock(conn, origin, at)
        batches = _batches(conn, origin)
        mv = model_version_ids(conn) if batches else {}
    for b in batches:
        manifest = load_frozen_manifest(store, b["sha"])
        if manifest.get("schema") != BATCH_SCHEMA:
            raise IntegrityFailure("not a health registry batch", sha256=b["sha"])
        issued_at = datetime.fromisoformat(manifest["issued_at"])
        fams = {k: C._family(v, {}) for k, v in manifest["families"].items()}
        with engine.connect() as conn:
            handled = handled_questions(conn, b["sha"])
            due = [
                q
                for q in manifest["questions"]
                if q["question_id"] not in handled and date.fromisoformat(q["period_end"]) <= now.date()
            ]
            if not due:
                out.append({"batch_sha256": b["sha"], "status": "nothing_due"})
                continue
            fks = {q["family"] for q in due}
            versions = {
                fk: versions_between(conn, fams[fk].dataset_key, origin=origin, after=issued_at, until=now)
                for fk in fks
            }
        issue_series = {
            fk: C.extract_series(
                cache.get(manifest["data"]["tables"][fams[fk].dataset_key]["artifact_sha256"]), fams[fk]
            )[0]
            for fk in fks
        }
        later = {
            fk: [(v, C.extract_series(cache.get(v["artifact_sha256"]), fams[fk])[0]) for v in versions[fk]]
            for fk in fks
        }
        res = resolve_batch(manifest, handled, issue_series, later, fams, today=now.date())
        if not res["outcomes"]:
            out.append({"batch_sha256": b["sha"], "status": "awaiting_outcomes", "due": len(due)})
            continue
        doc = C.finite(
            {
                "schema": RESOLUTION_SCHEMA,
                "label": LABEL,
                "batch_sha256": b["sha"],
                "issued_at": manifest["issued_at"],
                "resolved_at": now.isoformat(),
                "rule": "first table version registered after issued_at publishing the cell as an observation",
                **res,
            }
        )
        qids = sorted(o["question_id"] for o in res["outcomes"])
        srcs = sorted({o["source"]["artifact_sha256"] for o in res["outcomes"] if o.get("source")})
        with engine.begin() as conn:
            sha, ctx, act = _seal(
                conn,
                store,
                doc,
                schema_version="health-resolution/1",
                activity="evaluation.health_registry_resolve",
                actor=actor,
                params={"batch_sha256": b["sha"], "resolved": len(qids)},
                inputs=[(b["sha"], {"role": "sealed_batch"}), *[(s, {"role": "outcome_atlas_table"}) for s in srcs]],
                origin=origin,
                cutoff=now,
                parent=UUID(b["context_id"]),
            )
            scope = {
                "kind": SCOPE_KIND,
                "origin": origin,
                "batch_sha256": b["sha"],
                "resolution_sha256": sha,
                "question_ids": qids,
            }
            by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for s in res["scores"]:
                by_model[s["model"]].append(s)
            statuses = _count(o["status"] for o in res["outcomes"])
            for mk in sorted(by_model) or ["health.persistence"]:  # one row carries the handled ids if nothing scored
                ss = by_model.get(mk, [])
                _insert_eval(
                    conn,
                    mv[mk],
                    ctx,
                    sha,
                    "operational_as_known",
                    scope,
                    {
                        "kind": SCORE_KIND,
                        "model": f"{mk}@{all_models()[mk]}",
                        "n_scored": len(ss),
                        "mean_wis": (sum(s["wis"] for s in ss) / len(ss)) if ss else None,
                        "outcomes": dict(statuses),
                    },
                    act,
                )
        out.append(
            {"batch_sha256": b["sha"], "status": "scored", "outcomes": dict(statuses), "scores": len(res["scores"])}
        )
    return {"origin": origin, "batches": out}


def status(engine: Engine, *, origin: str = "live") -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """SELECT metrics->>'kind' AS kind, count(DISTINCT evaluation_manifest_sha256) AS docs, count(*) AS rows
                   FROM model.evaluation WHERE task_scope->>'kind' = :sk AND task_scope->>'origin' = :o GROUP BY 1"""
            ),
            {"sk": SCOPE_KIND, "o": origin},
        ).mappings()
        return {"origin": origin, "documents": {r["kind"]: {"documents": r["docs"], "rows": r["rows"]} for r in rows}}


def handle_health_registry(payload: Mapping[str, Any], ctx: Any) -> dict[str, Any]:
    action = payload.get("action")
    origin = str(payload.get("origin", "live"))
    fams = payload.get("families")
    if action == "issue":
        return issue_round(ctx.worker_engine, ctx.store, origin=origin, families=fams)
    if action == "resolve":
        return resolve_due(ctx.worker_engine, ctx.store, origin=origin)
    if action == "backtest":
        return run_backtests(ctx.worker_engine, ctx.store, origin=origin, families=fams)
    if action == "status":
        return status(ctx.worker_engine, origin=origin)
    raise ValidationProblem("unknown health registry action", action=str(action)[:32])


__all__ = [
    "build_backtest",
    "build_batch",
    "handle_health_registry",
    "issue_round",
    "latest_backtest",
    "resolve_batch",
    "resolve_due",
    "run_backtests",
    "status",
]
