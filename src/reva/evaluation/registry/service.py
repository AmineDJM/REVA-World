"""Issue, resolve and back-test the prospective forecast registry (worker credentials).

**Issue** (:func:`issue_cycle`): for the current quarterly cycle (database clock), pin the latest version of every
declared atlas table, generate the questions not yet issued in this cycle, forecast them with every baseline (and
the attributed IMF WEO projection where declared), and **seal** the batch: the canonical manifest (questions,
frozen forecasts, abstentions with reasons, table pins, per-series training digests, code digest, database-clock
``issued_at``) is stored content-addressed and its sha256 recorded in one ``model.evaluation`` row per model
(``metrics.kind = 'registry_issue'``, replay mode ``operational_as_known``) inside a new evaluation context. Rows
and artifacts are immutable; nothing about an issued forecast can change afterwards.

**Resolve** (:func:`resolve_due`): for every sealed batch, re-read and verify the manifest, look for each question
whose reference period has ended in the versions of the declared table registered *after* the seal (first version
publishing the cell as an observation wins), mark questions void after their void date and "not prospective" if
the pinned issue version already had the value, score every frozen forecast, and append one
``registry_score`` row per model (plus the resolution manifest). Re-runs only handle questions not yet handled.

**Back-test** (:func:`run_backtest`): pseudo-real-time replay of the baselines over history on the *final
vintage* of each table — labelled ``final-vintage pseudo-real-time`` (replay mode
``revised_data_retrospective``), never reported as real-time skill (decision 4 of the V5 roadmap).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.evaluation.ledger import load_frozen_manifest, manifest_sha256
from reva.evaluation.registry import energy as EN
from reva.evaluation.registry import finance as FN
from reva.evaluation.registry import periods as P
from reva.evaluation.registry.data import FrameCache, latest_version, versions_between
from reva.evaluation.registry.forecasts import (
    REFERENCE_WEO,
    all_models,
    forecast_baseline,
    forecast_reference,
    num,
    predictive_record,
    train_digest,
)
from reva.evaluation.registry.questions import (
    Series,
    extract_series,
    family_daily_cells,
    generate_meeting_questions,
    generate_questions,
    period_ended,
    resolution_value,
)
from reva.evaluation.registry.scores import aggregate, calibration, relative_to, score_forecast
from reva.evaluation.registry.specs import Family, SpecSet, _family, load_specs
from reva.kernel.errors import IntegrityFailure, MissingInput, ValidationProblem
from reva.models.forecasting.registry_baselines import BASELINES, Abstain, run_baseline

BATCH_SCHEMA = "reva-forecast-registry-batch/1"
RESOLUTION_SCHEMA = "reva-forecast-registry-resolution/1"
BACKTEST_SCHEMA = "reva-forecast-registry-backtest/1"
SCOPE_KIND = "forecast_registry"
ISSUE_KIND = "registry_issue"
SCORE_KIND = "registry_score"
BACKTEST_KIND = "registry_backtest"
LABEL = (
    "BASELINE forecasts only (persistence / random walk, seasonal naive, AR(p) fitted as of issue, historical "
    "quantiles) plus the attributed IMF WEO projection where declared: prospective, frozen and sealed before "
    "resolution; not validated models, not advice"
)
BACKTEST_LABEL = (
    "final-vintage pseudo-real-time back-test: every fit uses only periods up to its origin, but values are the "
    "final (revised) vintage, not the data as published at the time; never real-time skill"
)
ISSUE_LOCK = 7262105
BASELINE_KEY = "forecast.registry_persistence"
ORIGINS = ("live", "test_fixture")


# --------------------------------------------------------------------------- helpers


def _check_origin(origin: str) -> str:
    if origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    return origin


def _frozen_family(fam: Family) -> dict[str, Any]:
    raw = {k: v for k, v in fam.raw.items() if k != "panel"}
    raw["countries"] = list(fam.countries)
    return raw


def _family_of(doc: Mapping[str, Any]) -> Family:
    return _family(dict(doc), {})


def model_version_ids(conn: Any) -> dict[str, str]:
    models = all_models()
    rows = conn.execute(
        text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
        {"k": sorted(models)},
    ).mappings()
    found = {r["model_key"]: r["id"] for r in rows if models[r["model_key"]][0] == r["version"]}
    missing = sorted(set(models) - set(found))
    if missing:
        raise MissingInput("registry model versions are not registered (run `reva seed` as the owner)", missing=missing)
    return found


def _insert_eval(
    conn: Any,
    mv: str,
    ctx_id: UUID,
    sha: str,
    replay_mode: str,
    scope: Mapping[str, Any],
    metrics: Mapping[str, Any],
    act: UUID,
) -> UUID:
    import json

    from reva.evaluation.tournament_service import finite_json

    return UUID(
        str(
            conn.execute(
                text(
                    """INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256,
                         replay_mode, task_scope, metrics, provenance_activity_id)
                       VALUES (:m, :c, :s, :rm, CAST(:ts AS jsonb), CAST(:mt AS jsonb), :a) RETURNING id"""
                ),
                {
                    "m": mv,
                    "c": ctx_id,
                    "s": sha,
                    "rm": replay_mode,
                    "ts": json.dumps(finite_json(dict(scope))),
                    "mt": json.dumps(finite_json(dict(metrics))),
                    "a": act,
                },
            ).scalar_one()
        )
    )


def issued_question_keys(conn: Any, origin: str, cycle: str) -> set[str]:
    rows = conn.execute(
        text(
            """SELECT DISTINCT jsonb_array_elements_text(task_scope->'question_keys') AS k FROM model.evaluation
               WHERE metrics->>'kind' = :k AND task_scope->>'kind' = :sk AND task_scope->>'origin' = :o
                 AND task_scope->>'cycle' = :c"""
        ),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "o": origin, "c": cycle},
    ).scalars()
    return set(rows)


def _code_digest() -> str | None:
    try:
        from reva.operations.environment import code_digest

        return str(code_digest())
    except Exception:  # pragma: no cover - digest is informative only
        return None


# --------------------------------------------------------------------------- issue


def build_batch(
    specs: SpecSet,
    pins: Mapping[str, Mapping[str, Any] | None],
    frames: Mapping[str, Any],
    *,
    cycle: str,
    issued_at: datetime,
    origin: str,
    already: set[str],
    families: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pure: questions + frozen forecasts of one cycle from pinned frames (``frames``: artifact sha -> frame)."""
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    used: dict[str, dict[str, Any]] = {}
    fams_doc: dict[str, Any] = {}
    for fam in specs.families:
        if families and fam.key not in families:
            continue
        if fam.dataset_key is None:
            pending.append({"family": fam.key, "reason": f"awaiting: {fam.awaiting}"})
            continue
        pin = pins.get(fam.dataset_key)
        if pin is None:
            pending.append(
                {
                    "family": fam.key,
                    "reason": f"table {fam.dataset_key} not registered yet"
                    + (f" (connector owned by {fam.owner_agent})" if fam.owner_agent else ""),
                }
            )
            continue
        frame = frames[pin["artifact_sha256"]]
        series, n = extract_series(frame, fam)
        notes.extend(n)
        extra_pins: dict[str, dict[str, Any]] = {}
        if fam.value_rule == "meeting_change":
            assert fam.finance is not None
            cal = fam.finance["calendar"]
            cpin = pins.get(cal["dataset_key"])
            if cpin is None:
                pending.append({"family": fam.key, "reason": f"calendar table {cal['dataset_key']} not registered yet"})
                continue
            extra_pins[cal["dataset_key"]] = dict(cpin)
            qs, sk = generate_meeting_questions(
                fam,
                series,
                FN.meeting_days(frames[cpin["artifact_sha256"]], cal),
                cycle=cycle,
                today=issued_at.date(),
                source_pin=_short_pin(pin),
                calendar_pin=_short_pin(cpin),
            )
        else:
            qs, sk = generate_questions(
                fam, series, cycle=cycle, interval_level=specs.interval_level, source_pin=_short_pin(pin)
            )
        skipped.extend(sk)
        fam_used = False
        fin_ctx: tuple[dict[str, Any], dict[str, list[tuple[date, float]]]] | None = None
        en_ctx: dict[str, Any] | None = None  # energy families (PRED-ENERGY): STEO issue in force
        for q in qs:
            if q["question_key"] in already:
                continue
            s = series[q["geo"]]
            hist = s.history()
            fcs = [forecast_baseline(q, mk, hist) for mk in BASELINES] if fam.runs_generic_baselines else []
            if fam.finance is not None:
                if fin_ctx is None:
                    fin_ctx = _finance_context(fam, frame, pins, frames, extra_pins)
                own = fin_ctx[1].get(q["geo"], [])
                fcs.extend(FN.finance_forecasts(q, fam.finance, own, fin_ctx[0]))
            if EN.block(fam) is not None:
                if en_ctx is None:
                    en_ctx = EN.energy_context(fam, pins, frames, extra_pins, issued_at)
                fcs.extend(EN.energy_forecasts(q, fam, en_ctx))
            if not fcs or all(f["status"] == "abstained" for f in fcs):
                skipped.append(
                    {
                        "family": fam.key,
                        "geo": q["geo"],
                        "period": q["reference_period"],
                        "reason": "every baseline abstained: " + "; ".join(sorted({f["reason"] for f in fcs})),
                    }
                )
                continue
            if REFERENCE_WEO in fam.reference_forecasters:
                target = P.parse_label(fam.frequency, q["reference_period"])
                cell = s.projections.get(target)
                fcs.append(forecast_reference(q, REFERENCE_WEO, (cell.value, cell.refs[0]) if cell else None))
            if fam.value_rule == "meeting_change":
                q["training"] = {
                    "n": len(hist),
                    "first_day": date.fromordinal(hist[0][0]).isoformat(),
                    "last_day": date.fromordinal(hist[-1][0]).isoformat(),
                    "digest": train_digest(hist),
                }
            else:
                q["training"] = {
                    "n": len(hist),
                    "first_period": P.label_of(fam.frequency, hist[0][0]),
                    "last_period": P.label_of(fam.frequency, hist[-1][0]),
                    "digest": train_digest(hist),
                }
            questions.append(q)
            forecasts.extend(fcs)
            fam_used = True
        if fam_used:
            used[fam.dataset_key] = dict(pin)
            for k, p in extra_pins.items():
                used[k] = dict(p)
            fams_doc[fam.key] = _frozen_family(fam)
    models = all_models()
    return {
        "schema": BATCH_SCHEMA,
        "label": LABEL,
        "origin": origin,
        "cycle": cycle,
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "These questions and forecasts were frozen at issued_at (database clock) from the pinned atlas "
        "table versions, sealed by the sha256 of this manifest, and are scored only against values published "
        "in table versions registered after issued_at.",
        "specs": {"version": specs.version, "sha256": specs.sha256, "interval_level": specs.interval_level},
        "families": fams_doc,
        "models": [{"model_key": k, "version": v, "description": d} for k, (v, d) in sorted(models.items())],
        "data": {"tables": used, "selection_rule": "per dataset_key: latest evidence.dataset version at issued_at"},
        "code_digest": _code_digest(),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": skipped, "pending_families": pending, "notes": notes},
    }


def _finance_context(
    fam: Family,
    frame: Any,
    pins: Mapping[str, Mapping[str, Any] | None],
    frames: Mapping[str, Any],
    extra_pins: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[tuple[date, float]]]]:
    """Inputs of a finance family from its pinned input tables (recorded in ``extra_pins``) and the family's own
    daily series per geography (a missing input table leaves that input empty: its forecasters abstain)."""
    assert fam.finance is not None
    by_key: dict[str, Any] = {}
    for inp in FN.used_inputs(fam.finance).values():
        p = pins.get(inp["dataset_key"])
        if p is not None:
            by_key[inp["dataset_key"]] = frames[p["artifact_sha256"]]
            extra_pins[inp["dataset_key"]] = dict(p)
    inputs = FN.load_inputs(fam.finance, by_key)
    own: dict[str, list[tuple[date, float]]] = {}
    if fam.finance["kind"] != "meeting":
        for g in fam.geos:
            if fam.value_rule == "monthly_mean_of_daily":
                assert fam.indicator is not None
                cells = FN.daily_cells(frame, fam.indicator, g.provider_geo)
            else:
                cells = family_daily_cells(frame, fam, g.provider_geo)
            own[g.key] = FN.to_floats(cells)
    return inputs, own


def finance_input_keys(fam: Family) -> list[str]:
    """Tables a finance family reads besides its own (inputs and the meeting calendar)."""
    if fam.finance is None:
        return []
    keys = [inp["dataset_key"] for inp in FN.used_inputs(fam.finance).values()]
    if fam.finance.get("calendar"):
        keys.append(fam.finance["calendar"]["dataset_key"])
    return keys


def _short_pin(pin: Mapping[str, Any]) -> dict[str, Any]:
    return {k: pin[k] for k in ("dataset_key", "dataset_id", "artifact_sha256", "recorded_at")}


def issue_cycle(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    families: Sequence[str] | None = None,
    specs: SpecSet | None = None,
    actor: str = "worker:forecast_registry",
) -> dict[str, Any]:
    """Generate, forecast and seal the questions of the current cycle not issued yet. Idempotent per cycle."""
    from reva.adapters.postgres import repositories as repo

    _check_origin(origin)
    specs = specs or load_specs()
    with engine.connect() as conn:
        issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        cycle = P.cycle_of(issued_at.date())
        mv = model_version_ids(conn)
        already = issued_question_keys(conn, origin, cycle)
        pins: dict[str, dict[str, Any] | None] = {}
        for fam in specs.families:
            if not fam.dataset_key or (families and fam.key not in families):
                continue
            for dk in (fam.dataset_key, *finance_input_keys(fam), *EN.input_keys(fam)):
                if dk not in pins:
                    pins[dk] = latest_version(conn, dk, origin=origin, cutoff=issued_at)
    cache = FrameCache(store, size=len(pins) + 1)
    frames = {p["artifact_sha256"]: cache.get(p["artifact_sha256"]) for p in pins.values() if p}
    manifest = build_batch(
        specs, pins, frames, cycle=cycle, issued_at=issued_at, origin=origin, already=already, families=families
    )
    report = {
        "cycle": cycle,
        "origin": origin,
        "issued_at": issued_at.isoformat(),
        "n_questions": len(manifest["questions"]),
        "n_forecasts": sum(1 for f in manifest["forecasts"] if f["status"] == "issued"),
        "n_abstained": sum(1 for f in manifest["forecasts"] if f["status"] == "abstained"),
        "already_issued_in_cycle": len(already),
        "pending_families": manifest["generation"]["pending_families"],
        "n_skipped": len(manifest["generation"]["skipped"]),
    }
    if not manifest["questions"]:
        return {"status": "nothing_to_issue", **report}
    keys = sorted(q["question_key"] for q in manifest["questions"])
    by_type: dict[str, int] = defaultdict(int)
    for q in manifest["questions"]:
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
            rights={"licence": "internal-derived; each pinned atlas table keeps its source rights"},
        )
        if sha != manifest_sha256(manifest):
            raise IntegrityFailure("stored batch manifest hash differs from its canonical hash")
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue",
            "evaluation",
            actor,
            {"cycle": cycle, "origin": origin, "batch_sha256": sha, "label": LABEL, "questions": len(keys)},
        )
        repo.record_activity(
            conn,
            act,
            inputs=[
                (t["artifact_sha256"], {"role": "atlas_table", "dataset_key": k})
                for k, t in sorted(manifest["data"]["tables"].items())
            ],
            outputs=[sha],
        )
        ctx = repo.create_context(
            conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=issued_at
        )
        scope = {"kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha, "question_keys": keys}
        ids: dict[str, str] = {}
        for mk, (ver, _d) in sorted(all_models().items()):
            mine = [f for f in manifest["forecasts"] if f["model"] == mk]
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
                        "by_type": dict(by_type),
                    },
                    act.id,
                )
            )
        repo.emit_outbox(
            conn, "evaluation.forecast_registry_issued", {"batch_sha256": sha, "cycle": cycle, "origin": origin}
        )
    return {"status": "issued", "batch_sha256": sha, "context_id": str(ctx), "evaluation_ids": ids, **report}


# --------------------------------------------------------------------------- resolve


def _batches(conn: Any, origin: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """SELECT DISTINCT ON (evaluation_manifest_sha256) evaluation_manifest_sha256 AS sha,
                      context_id::text AS context_id, task_scope->>'cycle' AS cycle, recorded_at
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


def resolve_batch(
    manifest: Mapping[str, Any],
    handled: set[str],
    issue_series: Mapping[str, Mapping[str, Series]],
    later: Mapping[str, Sequence[tuple[Mapping[str, Any], Mapping[str, Series]]]],
    *,
    today: date,
) -> dict[str, Any]:
    """Pure: outcomes and scores of the pending questions of one sealed batch.

    ``issue_series``: family -> series of the pinned issue version; ``later``: family -> [(version pin, series)]
    of versions registered after the seal, oldest first."""
    outcomes: list[dict[str, Any]] = []
    by_q = defaultdict(list)
    for f in manifest["forecasts"]:
        by_q[f["question_id"]].append(f)
    scores: list[dict[str, Any]] = []
    for q in manifest["questions"]:
        qid = q["question_id"]
        if qid in handled or not period_ended(q, today):
            continue
        fam = q["family"]
        if resolution_value(q, issue_series.get(fam, {})) is not None:
            outcomes.append(
                {
                    "question_id": qid,
                    "status": "not_prospective",
                    "reason": "the pinned issue version already published this value",
                }
            )
            continue
        found = None
        for pin, series in later.get(fam, ()):
            cell = resolution_value(q, series)
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
                "source": {
                    "dataset_key": pin["dataset_key"],
                    "dataset_id": pin["dataset_id"],
                    "artifact_sha256": pin["artifact_sha256"],
                    "recorded_at": pin["recorded_at"],
                    "cells": list(cell.refs),
                },
            }
        )
        for f in by_q[qid]:
            if f["status"] == "issued":
                scores.append(score_forecast(q, f, cell.value))
    return {"outcomes": outcomes, "scores": scores}


def resolve_due(
    engine: Engine, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry"
) -> dict[str, Any]:
    """Resolve and score every sealed batch's due questions (only those not handled before)."""
    from reva.adapters.postgres import repositories as repo

    _check_origin(origin)
    cache = FrameCache(store, size=4)
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        batches = _batches(conn, origin)
        mv = model_version_ids(conn) if batches else {}
    for b in batches:
        manifest = load_frozen_manifest(store, b["sha"])
        if manifest.get("schema") != BATCH_SCHEMA:
            raise IntegrityFailure("not a forecast registry batch", sha256=b["sha"])
        issued_at = datetime.fromisoformat(manifest["issued_at"])
        with engine.connect() as conn:
            handled = handled_questions(conn, b["sha"])
            due = [q for q in manifest["questions"] if q["question_id"] not in handled and period_ended(q, now.date())]
            if not due:
                out.append({"batch_sha256": b["sha"], "status": "nothing_due"})
                continue
            fams = {q["family"] for q in due}
            versions = {
                fk: versions_between(
                    conn,
                    manifest["families"][fk]["dataset_key"],
                    origin=origin,
                    after=issued_at,
                    until=now,
                )
                for fk in fams
            }
        issue_series: dict[str, dict[str, Series]] = {}
        later: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Series]]]] = {}
        for fk in fams:
            fam = _family_of(manifest["families"][fk])
            pin = manifest["data"]["tables"][fam.dataset_key]
            issue_series[fk] = extract_series(cache.get(pin["artifact_sha256"]), fam)[0]
            later[fk] = [(v, extract_series(cache.get(v["artifact_sha256"]), fam)[0]) for v in versions[fk]]
        res = resolve_batch(manifest, handled, issue_series, later, today=now.date())
        if not res["outcomes"]:
            out.append({"batch_sha256": b["sha"], "status": "awaiting_outcomes", "due": len(due)})
            continue
        doc = {
            "schema": RESOLUTION_SCHEMA,
            "label": LABEL,
            "batch_sha256": b["sha"],
            "issued_at": manifest["issued_at"],
            "resolved_at": now.isoformat(),
            "rule": "first table version registered after issued_at publishing the cell as an observation",
            **res,
        }
        qids = sorted(o["question_id"] for o in res["outcomes"])
        with engine.begin() as conn:
            sha = repo.store_json_artifact(
                conn, store, doc, "evaluation", schema_version="forecast-registry-resolution/1"
            )
            act = repo.simple_activity(
                "evaluation.forecast_registry_resolve",
                "evaluation",
                actor,
                {"batch_sha256": b["sha"], "resolved": len(qids), "label": LABEL},
            )
            srcs = sorted({o["source"]["artifact_sha256"] for o in res["outcomes"] if o.get("source")})
            repo.record_activity(
                conn,
                act,
                inputs=[(b["sha"], {"role": "sealed_batch"}), *[(s, {"role": "outcome_atlas_table"}) for s in srcs]],
                outputs=[sha],
            )
            ctx = repo.create_context(
                conn,
                mode="evaluation",
                origin=origin,
                manifest_sha256=sha,
                knowledge_cutoff=now,
                parent_context_id=UUID(b["context_id"]),
            )
            scope = {
                "kind": SCOPE_KIND,
                "origin": origin,
                "cycle": b["cycle"],
                "batch_sha256": b["sha"],
                "question_ids": qids,
            }
            counts: dict[str, int] = defaultdict(int)
            for o in res["outcomes"]:
                counts[o["status"]] += 1
            for mk, (ver, _d) in sorted(all_models().items()):
                mine = [s for s in res["scores"] if s["model"] == mk]
                _insert_eval(
                    conn,
                    mv[mk],
                    ctx,
                    sha,
                    "operational_as_known",
                    scope,
                    {
                        "kind": SCORE_KIND,
                        "label": LABEL,
                        "model": f"{mk}@{ver}",
                        "resolution_sha256": sha,
                        "resolved_at": now.isoformat(),
                        "n_resolved": counts["resolved"],
                        "n_void": counts["void"],
                        "n_not_prospective": counts["not_prospective"],
                        "summary": aggregate(mine),
                        "scores": mine,
                    },
                    act.id,
                )
        out.append({"batch_sha256": b["sha"], "status": "scored", "resolution_sha256": sha, **dict(counts)})
    return {"origin": origin, "checked_at": now.isoformat(), "batches": out}


# --------------------------------------------------------------------------- pseudo-real-time back-test


def backtest_family(fam: Family, series: Mapping[str, Series], *, years: int = 8, level: float = 0.8) -> dict[str, Any]:
    """Pure: rolling-origin replay of the baselines on one family's final-vintage series."""
    season = P.season_of(fam.frequency)
    step = {12: 3, 4: 1, 1: 1}[season]
    horizons = sorted(
        {fam.backtest_lag_periods + h for r in fam.questions for h in r.horizons if fam.backtest_lag_periods + h >= 1}
    )
    rows: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for s in series.values():
        if len(s.observed) < 12:
            continue
        hist_all = s.history()
        vals = dict(hist_all)
        last = hist_all[-1][0]
        first_origin = last - years * season
        for o in range(first_origin, last, step):
            hist = [(i, v) for i, v in hist_all if i <= o]
            if len(hist) < 12 or hist[-1][0] != o:
                continue
            for h in horizons:
                target = o + h
                if target not in vals:
                    continue
                y = vals[target]
                for mk in BASELINES:
                    p = run_baseline(mk, hist, target, season)
                    if isinstance(p, Abstain):
                        continue
                    q = {
                        "question_id": f"{s.geo}:{o}:{h}",
                        "type": "interval",
                        "family": fam.key,
                        "interval_level": level,
                        "threshold": num(hist[-1][1]),
                    }
                    iv = p.interval(level)
                    pr = p.prob_above(hist[-1][1])
                    fc = {
                        "model": mk,
                        "point": num(p.point),
                        "predictive": predictive_record(p),
                        "interval": [num(iv[0]), num(iv[1])] if iv else None,
                    }
                    one = score_forecast(q, fc, num(y))
                    if pr is not None:
                        o_bin = 1 if y > hist[-1][1] else 0
                        one["prob_above"] = num(pr)
                        one["outcome_binary"] = o_bin
                        one["brier"] = num((pr - o_bin) ** 2)
                    rows[(mk, h)].append(one)
    results: list[dict[str, Any]] = []
    for h in horizons:
        base = rows.get((BASELINE_KEY, h), [])
        for mk in BASELINES:
            sc = rows.get((mk, h), [])
            if not sc:
                continue
            results.append(
                {
                    "model": mk,
                    "horizon_periods": h,
                    **aggregate(sc),
                    "mae_vs_persistence": relative_to(sc, base, "abs_error"),
                    "crps_vs_persistence": relative_to(sc, base, "crps"),
                    "calibration": calibration(sc),
                }
            )
    return {
        "family": fam.key,
        "frequency": fam.frequency,
        "horizons": horizons,
        "geos": len(series),
        "results": results,
    }


def run_backtest(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    families: Sequence[str] | None = None,
    years: int = 8,
    actor: str = "worker:forecast_registry",
) -> dict[str, Any]:
    from reva.adapters.postgres import repositories as repo

    _check_origin(origin)
    specs = load_specs()
    with engine.connect() as conn:
        cutoff: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        mv = model_version_ids(conn)
        pins = {
            f.dataset_key: latest_version(conn, f.dataset_key, origin=origin, cutoff=cutoff)
            for f in specs.families
            if f.dataset_key and (not families or f.key in families)
        }
    cache = FrameCache(store, size=3)
    fam_results: list[dict[str, Any]] = []
    used: dict[str, Any] = {}
    for fam in specs.families:
        if not fam.dataset_key or (families and fam.key not in families):
            continue
        if not fam.runs_generic_baselines:
            continue  # finance meeting families: back-tested by reva.evaluation.finance_backtest
        pin = pins.get(fam.dataset_key)
        if pin is None:
            continue
        series, _ = extract_series(cache.get(pin["artifact_sha256"]), fam)
        fam_results.append(backtest_family(fam, series, years=years, level=specs.interval_level))
        used[fam.dataset_key] = _short_pin(pin)
    if not fam_results:
        return {"status": "nothing_to_backtest", "origin": origin}
    manifest = {
        "schema": BACKTEST_SCHEMA,
        "label": BACKTEST_LABEL,
        "vintage_mode": "final_vintage_pseudo_real_time",
        "origin": origin,
        "run_at": cutoff.isoformat(),
        "years": years,
        "data": {"tables": used},
        "specs": {"version": specs.version, "sha256": specs.sha256},
        "families": fam_results,
        "code_digest": _code_digest(),
    }
    with engine.begin() as conn:
        sha = repo.store_json_artifact(
            conn, store, manifest, "evaluation", schema_version="forecast-registry-backtest/1"
        )
        act = repo.simple_activity(
            "evaluation.forecast_registry_backtest", "evaluation", actor, {"label": BACKTEST_LABEL, "sha256": sha}
        )
        repo.record_activity(
            conn, act, inputs=[(t["artifact_sha256"], {"role": "atlas_table"}) for t in used.values()], outputs=[sha]
        )
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=cutoff)
        scope = {
            "kind": SCOPE_KIND,
            "origin": origin,
            "backtest": True,
            "families": [f["family"] for f in fam_results],
            "vintage_mode": "final_vintage_pseudo_real_time",
        }
        for mk, (ver, _d) in sorted(BASELINES.items()):
            per = [
                {"family": f["family"], **{k: r[k] for k in r if k != "calibration"}}
                for f in fam_results
                for r in f["results"]
                if r["model"] == mk
            ]
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
    n = sum(r["n_scored"] for f in fam_results for r in f["results"])
    return {
        "status": "recorded",
        "backtest_sha256": sha,
        "families": len(fam_results),
        "n_scored": n,
        "label": BACKTEST_LABEL,
    }


# --------------------------------------------------------------------------- job


def handle_forecast_registry(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Worker job ``forecast_registry``: payload ``{"action": "issue"|"resolve"|"backtest", "origin"}``.

    Scheduled: ``issue`` weekly (a no-op within a cycle unless a new table landed: questions are issued once per
    quarterly cycle), ``resolve`` daily."""
    unknown = set(payload) - {"action", "origin", "families", "years"}
    if unknown:
        raise ValidationProblem("unknown forecast_registry payload keys", keys=sorted(unknown)[:5])
    action = payload.get("action")
    origin = _check_origin(str(payload.get("origin", "live")))
    fams = payload.get("families")
    if fams is not None and (not isinstance(fams, list) or not all(isinstance(x, str) for x in fams)):
        raise ValidationProblem("families must be a list of family keys")
    if action == "issue":
        res = issue_cycle(ctx.worker_engine, ctx.store, origin=origin, families=fams)
        if not fams:  # V6 PRED-MODELS: locked-holdout winners on the sealed open questions of this cycle
            from reva.evaluation.pred_models.issue import issue_supplements

            res["pred_models"] = issue_supplements(ctx.worker_engine, ctx.store, origin=origin)
        return res
    if action == "resolve":
        return resolve_due(ctx.worker_engine, ctx.store, origin=origin)
    if action == "backtest":
        years = int(payload.get("years", 8))
        if not 1 <= years <= 30:
            raise ValidationProblem("years must be in 1..30")
        return run_backtest(ctx.worker_engine, ctx.store, origin=origin, families=fams, years=years)
    raise ValidationProblem("action must be issue, resolve or backtest", action=str(action)[:32])
