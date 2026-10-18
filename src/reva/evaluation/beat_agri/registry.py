"""BEAT-AGRI in the forecast registry: per-release sealing (weekly job) and resolution through the registry path.

* **Issue** (:func:`issue_release`, called by the weekly ``forecast_registry`` ``issue`` job BEFORE the ensemble,
  and by ``reva.cli.beat_agri seal --seal``): on the WASDE release in force, one registry batch (``BATCH_SCHEMA``,
  task scope kind ``forecast_registry``, cycle ``beat_agri:<release date>``) for marketing year
  :data:`service.PROSPECTIVE_MY` — idempotent per release (a release already sealed is never re-sealed). Questions
  are registry questions (``reva-forecast-question/1`` fields) whose ``resolution.rule`` is the pre-registered truth
  rule; ``period_end`` = the declared truth date (the outcome is USDA's estimate published from then).
  Forecasts are registry records (``registry.forecasts._record``): WASDE point (attributed reference), WASDE fair fan,
  bc / rev / wx (normal predictive), each labelled with its holdout status.
* **Resolve** (:func:`resolve_manifest`, pure; :func:`resolve_batch_db`, called by ``registry.service.resolve_due``
  for batches declaring ``resolver = beat_agri`` and by ``reva.cli.beat_agri resolve``): the truth is read exactly
  as pre-registered — the series' value for the marketing year in the FIRST WASDE released on or after the declared
  date, from the latest ``atlas.usda_wasde_esmis.headline`` version — and never before that date (refused /
  awaiting). Scores = ``registry.scores.score_forecast`` (AE, CRPS, log score, PIT) recorded by
  ``registry.service._record_resolution`` (append-only ``registry_score`` rows read by V8 scores and the ensemble).

The first 2026/27 seal (batch ``5d903078…``, 2026-10-18, task scope kind ``beat_agri``, BEAT-AGRI's own record
format) predates this module; the same release is re-issued once in registry format (same numbers, checked) and
only the registry-format batch is resolved, so nothing is scored twice.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from reva.evaluation.beat_agri import cases as C
from reva.evaluation.beat_agri import seal as SE
from reva.evaluation.beat_agri import service as S

RESOLVER = "beat_agri"
WASDE_TABLE = "atlas.usda_wasde_esmis.headline"
WEATHER_TABLE = "atlas.ncei_cag.agbelt_monthly"
QUESTION_SCHEMA = "reva-forecast-question/1"
VOID_AFTER_DAYS = 400
LEGACY_BATCHES = ("5d903078a6e98e5e3cf34667bb27a128e2031b27a80bce9f6f6187538490c857",)
UNITS = {
    "yield": {"corn": "bushels per harvested acre", "soybeans": "bushels per harvested acre",
              "wheat": "bushels per harvested acre", "rice": "pounds per harvested acre"},
    "production": {"corn": "million bushels", "soybeans": "million bushels", "wheat": "million bushels",
                   "rice": "million hundredweight (rough equivalent)"},
}  # fmt: skip
CROP_FR = {"corn": "maïs", "soybeans": "soja", "wheat": "blé", "rice": "riz"}
ATTR_FR = {
    "yield": "rendement par acre récoltée",
    "production": "production",
    "ending_stocks": "stocks de fin de campagne",
}
LABEL_EN = {
    "yield": "yield per harvested acre",
    "production": "production",
    "ending_stocks": "ending stocks",
}


def truth_date(series: str, my: int) -> date:
    return date(my + 1, C.TRUTH_MONTH[series], 1)


def series_label(series: str) -> str:
    geo, crop, attr = series.split(".")
    return f"{'U.S.' if geo == 'us' else 'World'} {crop} {LABEL_EN[attr]}, marketing year (WASDE)"


def unit_of(series: str) -> str:
    geo, crop, attr = series.split(".")
    if geo == "world":
        return "million metric tons" + (" (milled basis)" if crop == "rice" else "")
    return UNITS.get(attr, UNITS["production"])[crop]


def label_fr(series: str) -> str:
    geo, crop, attr = series.split(".")
    return (
        f"{ATTR_FR[attr].capitalize()} de {CROP_FR[crop]}, {'États-Unis' if geo == 'us' else 'monde'}, campagne (WASDE)"
    )


def registry_question(rec: Mapping[str, Any], *, cycle: str, issue_pin: Mapping[str, Any]) -> dict[str, Any]:
    s, my = str(rec["series"]), int(rec["marketing_year"])
    td = truth_date(s, my)
    key = f"{cycle}|beat_agri.{s}|{'World' if s.startswith('world.') else 'United States'}|{my}|quantity"
    return {
        "schema": QUESTION_SCHEMA,
        "question_id": "fq_" + S.canonical_sha256(key)[:24],
        "question_key": key,
        "family": f"beat_agri.{s}",
        "type": "quantity",
        "frequency": "A",
        "reference_period": f"{my:04d}",
        "marketing_year": f"{my}/{(my + 1) % 100:02d}",
        "period_start": f"{my:04d}-01-01",
        "period_end": td.isoformat(),
        "period_note": "period_end = the declared truth date (USDA's estimate published from then), not the end of "
        "the marketing year",
        "cycle": cycle,
        "cycle_horizon": 0,
        "issue_horizon": 0,
        "concept_id": None,
        "geo": "WORLD" if s.startswith("world.") else "USA",
        "iso3": None,
        "geo_label_en": "World" if s.startswith("world.") else "United States",
        "geo_label_fr": "Monde" if s.startswith("world.") else "États-Unis",
        "label_en": series_label(s),
        "label_fr": label_fr(s),
        "unit": unit_of(s),
        "definition": f"USDA WASDE line {s} (atlas.usda_wasde_esmis), marketing year {my}/{(my + 1) % 100:02d}",
        "definition_source": "src/reva/adapters/connectors/atlas_usda_wasde_esmis.py",
        "interval_level": None,
        "text_en": f"What will USDA's estimate of {series_label(s)} {my}/{(my + 1) % 100:02d} be in the first WASDE "
        f"released on or after {td.isoformat()}?",
        "text_fr": f"Quelle sera l'estimation de l'USDA ({label_fr(s)} {my}/{(my + 1) % 100:02d}) dans le premier "
        f"WASDE publié le {td.isoformat()} ou après ?",
        "origin_release": rec["release"],
        "report_month_offset_k": rec["k"],
        "window": rec["window"],
        "resolution": {
            "rule": S.truth_rule_text(s, my),
            "resolver": RESOLVER,
            "source": WASDE_TABLE,
            "indicator_prefix": f"wasde_esmis:{s}|v=",
            "truth_date": td.isoformat(),
            "expected_date": (td + timedelta(days=14)).isoformat(),
            "void_after": (td + timedelta(days=VOID_AFTER_DAYS)).isoformat(),
            "issue_table": dict(issue_pin),
            "value_rule": "cell",
        },
    }


def _predictive(point: float | None, sd: float | None) -> Any:
    from reva.models.forecasting.registry_baselines import Predictive

    if point is None:
        return None
    if sd is not None and sd > 0:
        return Predictive("normal", float(point), mu=float(point), sigma=float(sd))
    return Predictive("point", float(point))


def build_registry_batch(
    prosp: Mapping[str, Any], *, now: datetime, confirmed: Sequence[str], pins: Mapping[str, Any], origin: str
) -> dict[str, Any]:
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.service import BATCH_SCHEMA

    cycle = f"beat_agri:{prosp['release_in_force']}"
    issue_pin = dict(pins.get(WASDE_TABLE) or {})
    qs: list[dict[str, Any]] = []
    fs: list[dict[str, Any]] = []
    fams: dict[str, Any] = {}
    for rec in prosp["records"]:
        q = registry_question(rec, cycle=cycle, issue_pin=issue_pin)
        qs.append(q)
        fams[q["family"]] = {
            "key": q["family"], "dataset_key": WASDE_TABLE, "frequency": "A", "value_rule": "cell",
            "labels": {"en": q["label_en"], "fr": q["label_fr"]}, "definition": q["definition"],
            "resolver": RESOLVER, "owner_agent": "BEAT-AGRI", "countries": [], "questions": [{"type": "quantity", "horizons": [0]}],
            "publication_lag_days": 0, "void_after_days": VOID_AFTER_DAYS, "backtest_lag_periods": 1,
        }  # fmt: skip
        for mk, (_v, m) in SE.MODELS.items():
            if m == "wasde":
                p, label = _predictive(rec["wasde_point"], None), "USDA WASDE projection (attributed reference)"
            elif m == "wasde_fan":
                p = _predictive(rec["wasde_point"], rec["sds"].get("wasde"))
                label = "BENCHMARK: N(WASDE point, RMS of WASDE's own past relative errors at this series and k)"
            else:
                keys = [k for k in confirmed if k.split("|")[2] == m and rec["series"] in C.GROUPS[k.split("|")[0]]
                        and k.split("|")[1] == rec["window"]]  # fmt: skip
                status = ("confirmed on the holdout: " + ", ".join(keys)) if keys else "NOT confirmed on the holdout"
                p = _predictive(rec["points"].get(m), rec["sds"].get(m))
                label = f"REVA correction of the WASDE projection (uses USDA's number); {status}"
            if p is None:
                fs.append(abstention(q, mk, "the pre-registered rule issues no forecast here"))
                continue
            f = _record(q, mk, p, ())
            f["label"] = label
            if m == "wasde":
                f["attribution"] = f"USDA, World Agricultural Supply and Demand Estimates, release {rec['release']}"
            if m in ("bc", "rev", "wx"):
                f["claim"] = label
            fs.append(f)
    return {
        "schema": BATCH_SCHEMA,
        "label": f"V7 BEAT-AGRI marketing year {S.PROSPECTIVE_MY}/{(S.PROSPECTIVE_MY + 1) % 100:02d} on WASDE "
        f"{prosp['release_in_force']} (registry format)",
        "origin": origin,
        "cycle": cycle,
        "resolver": RESOLVER,
        "issued_at": now.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "Sealed prospective forecasts of USDA's later estimate (truth rule in each question) for the "
        "2026/27 marketing year on the WASDE release in force; protocol registry/forecasts/beat_agri/protocol.yaml; "
        "resolved by reva.evaluation.beat_agri.registry (first WASDE on/after the declared date, never before).",
        "specs": {"protocol": "registry/forecasts/beat_agri/protocol.yaml"},
        "families": fams,
        "models": [{"model_key": k, "version": v} for k, (v, _m) in SE.MODELS.items()],
        "data": {"selection_rule": "latest version at issued_at", "tables": dict(pins)},
        "code_digest": S.code_sha256(),
        "questions": qs,
        "forecasts": fs,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


# --------------------------------------------------------------------------- inputs (engine + store passed in)


def load_inputs(
    conn: Any, store: Any, now: datetime, origin: str = "live"
) -> tuple[C.Vintages, C.Weather | None, dict[str, Any]]:
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.registry.data import latest_version

    pins = {k: latest_version(conn, k, origin=origin, cutoff=now) for k in (WASDE_TABLE, WEATHER_TABLE)}
    if pins[WASDE_TABLE] is None:
        raise LookupError(f"{WASDE_TABLE} has no {origin} table version")
    v = C.Vintages.from_frame(read_atlas_table(store, pins[WASDE_TABLE]["artifact_sha256"]))  # type: ignore[index]
    w = (C.Weather.from_frame(read_atlas_table(store, pins[WEATHER_TABLE]["artifact_sha256"]))  # type: ignore[index]
         if pins[WEATHER_TABLE] else None)  # fmt: skip
    doc = {k: {x: str(p[x]) for x in ("dataset_key", "dataset_id", "artifact_sha256", "recorded_at") if x in p}
           for k, p in pins.items() if p}  # fmt: skip
    return v, w, doc


def confirmed_scopes() -> list[str]:
    led = S.load_yaml(S.REGISTRY_DIR / "holdout_ledger.yaml")
    return sorted({k for o in led.get("openings") or [] for k in o.get("confirmed") or []})


def sealed_cycles(conn: Any, origin: str) -> set[str]:
    from reva.evaluation.registry.service import ISSUE_KIND, SCOPE_KIND

    rows = conn.execute(
        text("SELECT DISTINCT task_scope->>'cycle' FROM model.evaluation WHERE metrics->>'kind' = :k "
             "AND task_scope->>'kind' = :sk AND task_scope->>'origin' = :o AND task_scope->>'cycle' LIKE 'beat_agri:%'"),
        {"k": ISSUE_KIND, "sk": SCOPE_KIND, "o": origin},
    ).scalars()  # fmt: skip
    return {str(r) for r in rows}


def issue_release(
    engine: Any, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry", dry_run: bool = False
) -> dict[str, Any]:
    """Seal the registry batch of the WASDE release in force, once per release (idempotent)."""
    from reva.adapters.postgres import repositories as repo
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _insert_eval

    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        try:
            v, w, pins = load_inputs(conn, store, now, origin)
        except LookupError as e:
            return {"status": "no_input", "reason": str(e)}
        done = sealed_cycles(conn, origin)
    prosp = S.prospective_records(v, w, now)
    cycle = f"beat_agri:{prosp['release_in_force']}"
    if cycle in done:
        return {"status": "already_sealed", "cycle": cycle}
    if not prosp["records"]:
        return {"status": "nothing_to_issue", "cycle": cycle, "reason": "no 2026/27 question inside its window"}
    batch = build_registry_batch(prosp, now=now, confirmed=confirmed_scopes(), pins=pins, origin=origin)
    if dry_run:
        return {"status": "dry_run", "cycle": cycle, "questions": len(batch["questions"]),
                "forecasts": len(batch["forecasts"]), "batch": batch}  # fmt: skip
    from reva.evaluation.registry.service import model_version_ids

    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        if cycle in sealed_cycles(conn, origin):
            return {"status": "already_sealed", "cycle": cycle}
        mv = model_version_ids(conn)
        sha = repo.store_json_artifact(conn, store, batch, "evaluation", schema_version="forecast-registry-batch/1",
                                       rights={"licence": "internal-derived; WASDE values: U.S. public domain (USDA, "
                                               "attributed); NCEI weather: U.S. public domain"})  # fmt: skip
        act = repo.simple_activity("evaluation.forecast_registry_issue", "evaluation", actor,
                                   {"cycle": cycle, "origin": origin, "batch_sha256": sha, "resolver": RESOLVER})  # fmt: skip
        repo.record_activity(conn, act, inputs=[], outputs=[sha])
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=now)
        qids = sorted(q["question_id"] for q in batch["questions"])
        scope = {"kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha, "resolver": RESOLVER,
                 "question_keys": sorted(q["question_key"] for q in batch["questions"]), "question_ids": qids}  # fmt: skip
        for mk, (ver, _m) in SE.MODELS.items():
            mine = [f for f in batch["forecasts"] if f["model"] == mk]
            _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                         {"kind": ISSUE_KIND, "status": "sealed", "label": batch["label"], "model": f"{mk}@{ver}",
                          "issued_at": now.isoformat(), "batch_sha256": sha, "n_questions": len(qids),
                          "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                          "n_abstained": sum(1 for f in mine if f["status"] == "abstained")}, act.id)  # fmt: skip
    return {"status": "sealed", "cycle": cycle, "batch_sha256": sha, "issued_at": now.isoformat(),
            "questions": len(qids), "free_gb": round(shutil.disk_usage("/").free / 1e9, 2)}  # fmt: skip


# --------------------------------------------------------------------------- resolve


def resolve_manifest(
    manifest: Mapping[str, Any], handled: set[str], v: C.Vintages, pin: Mapping[str, Any], *, today: date
) -> dict[str, Any]:
    """Pure: outcomes and scores of the due questions of one registry-format BEAT-AGRI batch.

    A question is due only from its declared truth date (``period_end``); before it nothing is resolved, whatever
    the table holds. Truth = the value in the first WASDE released on or after that date (``Vintages.truth``); a
    truth release dated on/before the seal day is ``not_prospective``; no truth by ``void_after`` -> void."""
    from reva.evaluation.registry.scores import score_forecast

    issued_day = datetime.fromisoformat(str(manifest["issued_at"])).date()
    by_q: dict[str, list[Mapping[str, Any]]] = {}
    for f in manifest["forecasts"]:
        by_q.setdefault(f["question_id"], []).append(f)
    outcomes: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for q in manifest["questions"]:
        qid = q["question_id"]
        td = date.fromisoformat(str(q["resolution"]["truth_date"]))
        if qid in handled or today < td:
            continue
        series = q["family"].removeprefix("beat_agri.")
        my = int(q["reference_period"])
        tr_rel = v.truth_release(series, my)
        if tr_rel is None or tr_rel.release < td:
            if today >= date.fromisoformat(q["resolution"]["void_after"]):
                outcomes.append({"question_id": qid, "status": "void", "reason": "no WASDE on/after the truth date "
                                 "printing this marketing year by the void date"})  # fmt: skip
            continue
        if tr_rel.release <= issued_day:
            outcomes.append({"question_id": qid, "status": "not_prospective",
                             "reason": f"truth release {tr_rel.release} on/before the seal day {issued_day}"})  # fmt: skip
            continue
        value = format(tr_rel.values[(series, my)][0], ".10g")
        outcomes.append({
            "question_id": qid, "status": "resolved", "value": value,
            "source": {"dataset_key": pin.get("dataset_key"), "dataset_id": pin.get("dataset_id"),
                       "artifact_sha256": pin.get("artifact_sha256"), "recorded_at": pin.get("recorded_at"),
                       "cells": [{"indicator": f"wasde_esmis:{series}|v={tr_rel.release.isoformat()}",
                                  "period_start": f"{my:04d}-01-01", "release_date": tr_rel.release.isoformat()}]},
            "rule": q["resolution"]["rule"],
        })  # fmt: skip
        for f in by_q.get(qid, []):
            if f["status"] == "issued":
                scores.append(score_forecast(q, f, value))
    return {"outcomes": outcomes, "scores": scores}


def is_beat_agri(manifest: Mapping[str, Any]) -> bool:
    return manifest.get("resolver") == RESOLVER


def resolve_batch_db(
    engine: Any, store: Any, b: Mapping[str, Any], manifest: Mapping[str, Any], now: datetime, mv: Mapping[str, Any],
    origin: str, actor: str,
) -> dict[str, Any]:  # fmt: skip
    """One registry-format BEAT-AGRI batch through the registry's resolution path (``_record_resolution``)."""
    from reva.evaluation.registry.service import _record_resolution, handled_questions

    due = [q for q in manifest["questions"] if now.date() >= date.fromisoformat(q["resolution"]["truth_date"])]
    if not due:
        first = min(q["resolution"]["truth_date"] for q in manifest["questions"])
        return {"batch_sha256": b["sha"], "status": "refused_before_truth_date", "earliest_truth_date": first}
    with engine.connect() as conn:
        handled = handled_questions(conn, b["sha"])
        v, _w, pins = load_inputs(conn, store, now, origin)
    res = resolve_manifest(manifest, handled, v, pins[WASDE_TABLE], today=now.date())
    if not res["outcomes"]:
        return {"batch_sha256": b["sha"], "status": "awaiting_outcomes", "due": len(due)}
    sha, counts = _record_resolution(engine, store, b, manifest, res, now, mv, origin, actor,
                                     extra={"rule": "BEAT-AGRI pre-registered truth rule (see each outcome)"})  # fmt: skip
    return {"batch_sha256": b["sha"], "status": "scored", "resolution_sha256": sha, **dict(counts)}


def resolve(
    engine: Any, store: Any, *, origin: str = "live", actor: str = "worker:forecast_registry"
) -> dict[str, Any]:
    """Resolve every registry-format BEAT-AGRI batch (the CLI's ``resolve``; the daily job reaches the same code
    through ``registry.service.resolve_due``)."""
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry.service import _batches, model_version_ids

    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        bs = [b for b in _batches(conn, origin) if str(b.get("cycle") or "").startswith("beat_agri:")]
        mv = model_version_ids(conn) if bs else {}
    out = []
    for b in bs:
        m = load_frozen_manifest(store, b["sha"])
        if is_beat_agri(m):
            out.append(resolve_batch_db(engine, store, b, m, now, mv, origin, actor))
    return {"origin": origin, "checked_at": now.astimezone(UTC).isoformat(), "batches": out}


__all__ = [
    "LEGACY_BATCHES",
    "RESOLVER",
    "build_registry_batch",
    "is_beat_agri",
    "issue_release",
    "registry_question",
    "resolve",
    "resolve_batch_db",
    "resolve_manifest",
    "truth_date",
]
