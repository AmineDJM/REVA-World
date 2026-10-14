"""Model evaluation service: rolling-origin forecast tournament as a durable job.

``handle_evaluation(payload, ctx)`` (job type ``evaluation``):

1. Builds a panel of strictly positive annual series (WDI population and GDP
   for FRA/DZA/CHN/USA, 2000-2024 by default) from the latest published belief
   release (or an explicit ``release_id``), or, with ``source="observations"``,
   from the latest eligible pre-cutoff revisions selected in memory with
   ``source_select_v1``. Missing values are dropped and recorded; series with a
   non-positive value are excluded and recorded (never clipped).
2. Reserves a **locked final holdout** (the last ``locked_holdout.years`` calendar
   years of the panel, 2 by default). The challenger selection
   (:func:`reva.models.tournament.run_tournament`) sees only the development
   years; the holdout is opened only to confirm a challenger that already
   passed every development gate (or for an explicit ``final_report``), at most
   ``max_accesses_per_model_version`` times (1) per model version and holdout
   cell. Reuse is refused and recorded.
3. Stores an evaluation manifest (namespace ``evaluation``) with predictions,
   targets, scores, ineligible/dropped cases, policy, seed, a researcher
   degrees-of-freedom declaration and holdout usage counts; creates an
   evaluation-mode context; inserts one ``model.evaluation`` row per model and
   period, and one ``model.promotion`` decision per challenger (reviewer
   ``gate-evaluator:tournament``). All writes and the job completion commit in
   one fenced transaction.

Every artefact carries the label :data:`LABEL`: this is a revised-data
retrospective, not a real-time backtest. Insufficient evidence keeps the
incumbent; the naive ``forecast.last_value`` baseline is always evaluated.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from reva.evaluation.splits import CutoffScopedSeries
from reva.kernel.errors import CapabilityGap, MissingInput, PolicyDenied, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.models.forecasting.base import ForecastModel, IneligibleReport, to_float
from reva.models.forecasting.drift import LogDrift
from reva.models.forecasting.last_value import LastValue
from reva.models.forecasting.local_level import LocalLevelLog
from reva.models.metrics import absolute_error, interval_covers, mase_scale
from reva.models.registry import PromotionEvent, current_pointer
from reva.models.tournament import METHOD_VERSION as TOURNAMENT_METHOD
from reva.models.tournament import (
    ModelMetrics,
    PredictionRecord,
    TournamentResult,
    dm_hln_test,
    holdout_non_inferiority,
    holm_rejections,
    run_tournament,
)

LABEL = "revised-data retrospective — not a real-time backtest; forecasts were not issued at these origins"
REPLAY_MODE = "revised_data_retrospective"
HONESTY_STATEMENT = (
    "Scores use today's revised historical values split by year. REVA did not exist at these origins and "
    "issued no forecasts then; data available at each origin may have differed (first releases are not "
    "archived in V1). This is research evidence, not a genuine real-time backtest."
)
SCHEMA = "reva-evaluation-manifest/1"
SERVICE_VERSION = "reva.evaluation.tournament_service@1.0.0"
REVIEWER = "gate-evaluator:tournament"
HOLDOUT_LOCK = 7262031

POP = "demography.population.wdi_midyear_total"
GDP = "economics.gdp.nominal_current_usd"
SUPPORTED_CONCEPTS = (POP, GDP)
DEFAULT_COUNTRIES = ("FRA", "DZA", "CHN", "USA")
DEFAULT_YEARS = (2000, 2024)
DEFAULT_HORIZONS = (1, 2, 3)
DEFAULT_SEED = 20261014
MAX_HORIZON = 5

NAIVE_KEY = LastValue.key
CANDIDATES: dict[str, Any] = {
    LastValue.key: LastValue,
    LogDrift.key: LogDrift,
    LocalLevelLog.key: LocalLevelLog,
}
HOLDOUT_DEFAULTS: dict[str, Any] = {"years": 2, "max_accesses_per_model_version": 1}

HoldoutMode = Literal["on_promotion", "final_report"]


def model_ref(m: ForecastModel) -> str:
    return f"{m.key}@{m.version}"


def series_id(concept: str, iso3: str) -> str:
    return f"{iso3}:{concept}"


# --------------------------------------------------------------------------- panel


@dataclass
class Panel:
    series: dict[str, list[tuple[int, Decimal]]]
    meta: dict[str, dict[str, Any]]
    dropped: list[dict[str, Any]]
    source: dict[str, Any]

    def digest(self) -> str:
        return logical_hash({sid: [[y, v] for y, v in obs] for sid, obs in sorted(self.series.items())})


def annual_year_of(temporal: Mapping[str, Any]) -> int | None:
    """Calendar year of an annual support, or None if the support is not annual."""
    start = temporal.get("start")
    if not start:
        return None
    s = date.fromisoformat(str(start))
    if temporal.get("kind") == "instant":
        return s.year
    end = temporal.get("end")
    if end and s.month == 1 and s.day == 1 and date.fromisoformat(str(end)) == date(s.year + 1, 1, 1):
        return s.year
    return None


def panel_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    concepts: Sequence[str],
    countries: Sequence[str],
    years: tuple[int, int],
) -> tuple[dict[str, list[tuple[int, Decimal]]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Pure: normalised rows -> strictly positive annual series.

    Each row needs ``concept_id``, ``spatial_spec`` (``key`` like ``WB:FRA``),
    ``temporal_extent``, ``aggregation`` and ``value`` (typed value dict).
    """
    want_c, want_k = set(concepts), set(countries)
    raw: dict[str, dict[int, Decimal]] = defaultdict(dict)
    meta: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    for r in rows:
        concept = str(r["concept_id"])
        key = str((r.get("spatial_spec") or {}).get("key", ""))
        iso3 = key.split(":", 1)[1] if ":" in key else key
        if concept not in want_c or iso3 not in want_k:
            continue
        sid = series_id(concept, iso3)
        year = annual_year_of(r.get("temporal_extent") or {})
        if year is None:
            dropped.append({"series_id": sid, "reason": "non_annual_support"})
            continue
        if not (years[0] <= year <= years[1]):
            continue
        value = r.get("value") or {}
        if value.get("kind") != "decimal":
            dropped.append({"series_id": sid, "year": year, "reason": f"value_kind_{value.get('kind')}"})
            continue
        try:
            v = Decimal(str(value["value"]))
        except (InvalidOperation, KeyError):
            dropped.append({"series_id": sid, "year": year, "reason": "unparseable_value"})
            continue
        if not v.is_finite():
            dropped.append({"series_id": sid, "year": year, "reason": "non_finite_value"})
            continue
        m = meta.setdefault(
            sid,
            {"concept_id": concept, "iso3": iso3, "unit_ref": value.get("unit_ref"),
             "aggregation": r.get("aggregation")},
        )
        if m["unit_ref"] != value.get("unit_ref") or m["aggregation"] != r.get("aggregation"):
            m["inconsistent"] = True
        if year in raw[sid]:
            m["duplicate_years"] = True
        raw[sid][year] = v
    series: dict[str, list[tuple[int, Decimal]]] = {}
    for sid in sorted(raw):
        m = meta[sid]
        obs = sorted(raw[sid].items())
        if m.pop("inconsistent", False):
            dropped.append({"series_id": sid, "reason": "inconsistent_unit_or_support"})
            continue
        if m.pop("duplicate_years", False):
            dropped.append({"series_id": sid, "reason": "duplicate_years"})
            continue
        bad = [y for y, v in obs if v <= 0]
        if bad:
            dropped.append({"series_id": sid, "reason": "non_positive_values", "years": bad,
                            "note": "strictly positive series required; values are never clipped"})
            continue
        m.update({"n": len(obs), "first_year": obs[0][0], "last_year": obs[-1][0],
                  "gaps": [y for y in range(obs[0][0], obs[-1][0] + 1) if y not in raw[sid]]})
        series[sid] = obs
    for sid in sorted(set(meta) - set(series)):
        meta.pop(sid)
    return series, meta, dropped


def load_release_panel(
    conn: Connection,
    release: Mapping[str, Any],
    *,
    concepts: Sequence[str],
    countries: Sequence[str],
    years: tuple[int, int],
) -> Panel:
    rows: list[dict[str, Any]] = []
    for c in concepts:
        rows.extend(
            dict(r)
            for r in conn.execute(
                text(
                    """SELECT c.concept_id, s.spatial_spec, s.temporal_extent, s.aggregation, e.value
                       FROM belief.estimate e
                       JOIN ontology.concept_version c ON c.id = e.concept_version_id
                       JOIN world.support s ON s.id = e.support_id
                       WHERE e.release_id = :r AND c.concept_id = :c AND e.modality = 'selected_report'"""
                ),
                {"r": release["id"], "c": c},
            ).mappings()
        )
    series, meta, dropped = panel_from_rows(rows, concepts=concepts, countries=countries, years=years)
    source = {
        "kind": "belief_release",
        "release_id": str(release["id"]),
        "release_context_id": str(release["context_id"]),
        "release_manifest_sha256": release["manifest_sha256"],
        "publication_sequence": release.get("sequence"),
        "knowledge_cutoff": release["knowledge_cutoff"].isoformat(),
        "origin": release["origin"],
        "method": release.get("method"),
    }
    return Panel(series, meta, dropped, source)


def load_observation_panel(
    conn: Connection,
    *,
    origin: str,
    cutoff: datetime,
    concepts: Sequence[str],
    countries: Sequence[str],
    years: tuple[int, int],
) -> Panel:
    from reva.belief.selection import eligible_latest, select
    from reva.ontology.registry import load_registry

    obs_rows = eligible_latest(conn, cutoff, [origin], list(concepts))
    sels = select(obs_rows, load_registry().policies["selection"]["preferences"])
    supports = {
        r["id"]: dict(r)
        for r in conn.execute(
            text("SELECT id, spatial_spec, temporal_extent, aggregation FROM world.support WHERE id = ANY(:ids)"),
            {"ids": list({s.support_id for s in sels})},
        ).mappings()
    }
    rows = [
        {"concept_id": s.concept_id, **{k: supports[s.support_id][k]
                                        for k in ("spatial_spec", "temporal_extent", "aggregation")},
         "value": s.value}
        for s in sels
    ]
    series, meta, dropped = panel_from_rows(rows, concepts=concepts, countries=countries, years=years)
    source = {
        "kind": "observations_latest_revision",
        "note": "latest eligible pre-cutoff revision per lineage, source_select_v1 applied in memory; "
        "not a published release",
        "knowledge_cutoff": cutoff.isoformat(),
        "origin": origin,
        "observation_ids_digest": logical_hash(sorted(str(s.observation_id) for s in sels)),
        "n_observations": len(sels),
    }
    return Panel(series, meta, dropped, source)


# --------------------------------------------------------------------------- locked holdout


@dataclass(frozen=True)
class HoldoutSpec:
    years: tuple[int, ...]
    last_dev_year: int
    max_accesses: int
    cells: tuple[str, ...]  # "series_id:year" cells whose values are locked
    key: str


def holdout_spec(series: Mapping[str, Sequence[tuple[int, Any]]], n_years: int, max_accesses: int) -> HoldoutSpec:
    if n_years < 1:
        raise ValidationProblem("the locked holdout must reserve at least one year")
    last = max(y for obs in series.values() for y, _ in obs)
    years = tuple(range(last - n_years + 1, last + 1))
    cells = tuple(sorted(f"{sid}:{y}" for sid, obs in series.items() for y, _ in obs if y in years))
    key = logical_hash({"schema": "locked-holdout/1", "cells": cells})
    return HoldoutSpec(years, years[0] - 1, int(max_accesses), cells, key)


def development_panel(
    series: Mapping[str, Sequence[tuple[int, Any]]], spec: HoldoutSpec
) -> dict[str, list[tuple[int, object]]]:
    """Series truncated at the last development year (the holdout is invisible)."""
    return {sid: CutoffScopedSeries(obs, cutoff=spec.last_dev_year).observations() for sid, obs in series.items()}


def score_holdout(
    series: Mapping[str, Sequence[tuple[int, Any]]],
    spec: HoldoutSpec,
    models: Sequence[ForecastModel],
    level: float,
) -> dict[str, dict[str, Any]]:
    """Fit each model on all development years and score the locked years."""
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        recs: list[dict[str, Any]] = []
        inel: list[dict[str, Any]] = []
        for sid, obs in sorted(series.items()):
            actual = {y: to_float(v) for y, v in obs}
            targets = [y for y in spec.years if y in actual]
            if not targets:
                continue
            train = CutoffScopedSeries(obs, cutoff=spec.last_dev_year).observations()
            if not train:
                inel.append({"series_id": sid, "reason": "no_development_data"})
                continue
            origin = train[-1][0]
            fitted = m.fit(train)  # type: ignore[arg-type]
            if isinstance(fitted, IneligibleReport):
                inel.append({"series_id": sid, "reason": fitted.reason, "n_train": fitted.n_train})
                continue
            scale = mase_scale([to_float(v) for _, v in train])
            for fc in fitted.predict([t - origin for t in targets], level=level):
                y = actual[fc.target_year]
                ae = absolute_error(fc.point, y)
                recs.append({
                    "series_id": sid, "origin": origin, "horizon": fc.horizon, "target_year": fc.target_year,
                    "model": fc.model_key, "model_version": fc.model_version, "point": fc.point,
                    "interval": list(fc.interval) if fc.interval else None, "level": fc.level, "target": y,
                    "abs_error": ae, "mase_scaled_error": None if scale.value is None else ae / scale.value,
                    "covered": None if fc.interval is None else interval_covers(fc.interval, y),
                })
        scaled = [r["mase_scaled_error"] for r in recs if r["mase_scaled_error"] is not None]
        ints = [r for r in recs if r["covered"] is not None]
        out[model_ref(m)] = {
            "predictions": recs,
            "ineligible": inel,
            "metrics": {
                "n_predictions": len(recs),
                "mase": math.fsum(scaled) / len(scaled) if scaled else None,
                "n_scaled": len(scaled),
                "mae": math.fsum(r["abs_error"] for r in recs) / len(recs) if recs else None,
                "coverage": sum(bool(r["covered"]) for r in ints) / len(ints) if ints else None,
                "n_intervals": len(ints),
            },
        }
    return out


# --------------------------------------------------------------------------- paired comparisons


def _case_key(r: Any) -> tuple[str, int, int]:
    if isinstance(r, Mapping):
        return (str(r["series_id"]), int(r["origin"]), int(r["horizon"]))
    return (r.series_id, r.origin, r.horizon)


def paired_common_metrics(records: Sequence[PredictionRecord], model_keys: Sequence[str]) -> dict[str, Any]:
    """Headline metrics on the intersection of cases where *every* model has a defined scaled error.

    Per-model MASE over each model's own eligible cases is not comparable across
    models (different case sets can flip the ranking); these paired figures are
    the ones used for reporting and selection.
    """
    by_case: dict[tuple[str, int, int], dict[str, PredictionRecord]] = defaultdict(dict)
    for r in records:
        if r.model in model_keys and r.mase_scaled_error is not None:
            by_case[_case_key(r)][r.model] = r
    common = sorted(c for c, d in by_case.items() if all(k in d for k in model_keys))
    metrics: dict[str, Any] = {}
    for k in model_keys:
        recs = [by_case[c][k] for c in common]
        ints = [r for r in recs if r.interval is not None]
        metrics[k] = {
            "mase": math.fsum(r.mase_scaled_error for r in recs if r.mase_scaled_error is not None) / len(recs)
            if recs else None,
            "mae": math.fsum(r.abs_error for r in recs) / len(recs) if recs else None,
            "coverage": sum(bool(r.covered) for r in ints) / len(ints) if ints else None,
            "n_intervals": len(ints),
            "mean_interval_width": math.fsum(r.interval[1] - r.interval[0] for r in ints if r.interval) / len(ints)
            if ints else None,
        }
    return {"models": list(model_keys), "n_common_cases": len(common),
            "definition": "cases (series, origin, horizon) where every listed model has a defined scaled error",
            "metrics": metrics}


def _headline_paired(result: TournamentResult, keys: Sequence[str]) -> dict[str, Any]:
    """Paired common-case metrics: the tournament's own when provided, else computed here."""
    pm = getattr(result, "paired_metrics", None)
    if pm:
        return {"models": list(getattr(result, "paired_models", ()) or keys),
                "n_common_cases": int(getattr(result, "n_paired_cases", 0)),
                "definition": "cases (series, origin, horizon) where every compared model has a defined "
                "scaled error (reva.models.tournament paired_metrics)",
                "metrics": {k: _jsonable(m) for k, m in pm.items()}}
    return paired_common_metrics(result.records, keys)


def choose_among_challengers(
    records: Sequence[PredictionRecord], keys: Sequence[str], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Paired selection among challengers that all passed every gate.

    On the common cases of ``keys`` the challenger with the lowest paired MASE
    is selected only if, against *each* other challenger, the origin-aggregated
    Diebold-Mariano/HLN test (one-sided, Holm across horizons at
    ``paired_test.alpha``; the same machinery as the promotion rule) rejects
    equal loss for at least one horizon and no horizon is significantly worse.
    Otherwise ``winner`` is ``None``: keep the incumbent and route to review.
    """
    pm = paired_common_metrics(records, keys)
    n = pm["n_common_cases"]
    out: dict[str, Any] = {"n_common": n, "paired_mase": {k: pm["metrics"][k]["mase"] for k in keys},
                           "comparisons": {}, "winner": None, "test": "dm_hln_holm (origin-aggregated)"}
    if n == 0:
        return out
    pt = {"alpha": 0.025, "min_origins_per_horizon": 8, **dict(policy.get("paired_test") or {})}
    alpha, min_o = float(pt["alpha"]), int(pt["min_origins_per_horizon"])
    best = min(keys, key=lambda k: (pm["metrics"][k]["mase"], k))
    by_case: dict[tuple[str, int, int], dict[str, float]] = defaultdict(dict)
    for r in records:
        if r.model in keys and r.mase_scaled_error is not None:
            by_case[_case_key(r)][r.model] = r.mase_scaled_error
    common = sorted(c for c, d in by_case.items() if all(k in d for k in keys))
    all_sig = True
    for other in sorted(set(keys) - {best}):
        per_h: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        for sid, origin, h in common:
            per_h[h][origin].append(by_case[(sid, origin, h)][best] - by_case[(sid, origin, h)][other])
        p_less: dict[int, float] = {}
        worse = False
        tests: dict[str, Any] = {}
        for h, by_o in sorted(per_h.items()):
            xs = [math.fsum(v) / len(v) for _, v in sorted(by_o.items())]
            if len(xs) < max(min_o, 2):
                tests[str(h)] = {"tested": False, "n_origins": len(xs)}
                continue
            dm = dm_hln_test(xs, h)
            p_less[h] = dm.p_less
            worse = worse or dm.p_greater <= alpha
            tests[str(h)] = {"tested": True, "n_origins": len(xs), "mean": dm.mean, "p_less": dm.p_less,
                             "p_greater": dm.p_greater}
        rejected = holm_rejections(p_less, alpha) if p_less else {}
        sig = bool(rejected) and not worse
        out["comparisons"][f"{best} vs {other}"] = {"horizons": tests, "holm_rejected": sorted(rejected),
                                                    "significant": sig}
        all_sig = all_sig and sig
    out["winner"] = best if all_sig else None
    return out


# --------------------------------------------------------------------------- pure evaluation


@dataclass
class EvaluationOutcome:
    tournament: TournamentResult
    holdout: HoldoutSpec
    holdout_status: str  # locked_unopened | scored | blocked
    holdout_scores: dict[str, dict[str, Any]]
    holdout_blocked: dict[str, int]
    access_before: dict[str, int]
    access_after: dict[str, int]
    decisions: dict[str, dict[str, Any]]  # challenger ref -> {decision, reasons, ...}
    status: str  # promoted | incumbent_kept | insufficient_evaluation
    runtime_seconds: float
    models: dict[str, ForecastModel] = field(default_factory=dict)
    paired: dict[str, Any] = field(default_factory=dict)  # headline metrics on common cases
    challenger_selection: dict[str, Any] | None = None


def evaluate(
    series: Mapping[str, Sequence[tuple[int, Any]]],
    *,
    incumbent_key: str,
    horizons: Sequence[int],
    seed: int,
    policy: Mapping[str, Any],
    holdout_years: int,
    max_holdout_accesses: int,
    prior_holdout_accesses: Mapping[str, int],
    holdout_mode: HoldoutMode = "on_promotion",
) -> EvaluationOutcome:
    """Pure: development tournament + locked-holdout confirmation + final decisions.

    ``prior_holdout_accesses`` maps ``key@version`` to how many earlier
    evaluations already scored that model version on any cell of this holdout.
    """
    t0 = time.perf_counter()
    if not series:
        raise MissingInput("the evaluation panel is empty")
    models: dict[str, ForecastModel] = {k: cls() for k, cls in CANDIDATES.items()}
    if incumbent_key not in models:
        raise CapabilityGap(f"incumbent {incumbent_key!r} is not an available forecast model")
    spec = holdout_spec(series, holdout_years, max_holdout_accesses)
    dev = {sid: obs for sid, obs in development_panel(series, spec).items() if obs}
    if not dev:
        raise MissingInput("no development years remain after reserving the locked holdout")
    result = run_tournament(  # Decimal values are converted explicitly by the tournament (to_float)
        dev, list(models.values()), incumbent_key, horizons,  # type: ignore[arg-type]
        replay_mode=REPLAY_MODE, seed=seed, policy=policy,
    )
    pol = result.policy
    inc_ref = model_ref(models[incumbent_key])
    passing = sorted(k for k, d in result.decisions.items() if d.decision == "promote")

    needed: list[str] = []
    if holdout_mode == "final_report":
        needed = sorted(models)
    elif passing:
        needed = sorted({*passing, incumbent_key})
    access_before = {model_ref(models[k]): int(prior_holdout_accesses.get(model_ref(models[k]), 0)) for k in models}
    blocked = {model_ref(models[k]): access_before[model_ref(models[k])] for k in needed
               if access_before[model_ref(models[k])] + 1 > spec.max_accesses}
    holdout_scores: dict[str, dict[str, Any]] = {}
    if not needed:
        status_h = "locked_unopened"
    elif blocked:
        status_h = "blocked"
    else:
        status_h = "scored"
        holdout_scores = score_holdout(series, spec, [models[k] for k in needed], float(pol["interval_level"]))
    access_after = {r: n + (1 if r in holdout_scores else 0) for r, n in access_before.items()}

    decisions: dict[str, dict[str, Any]] = {}
    confirmed: list[str] = []
    for k, d in sorted(result.decisions.items()):
        ci = getattr(d, "bootstrap_interval", None)
        base = {
            "challenger": model_ref(models[k]), "incumbent": inc_ref, "n_pairs": getattr(d, "n_pairs", None),
            "mean_loss_difference": getattr(d, "mean_loss_difference", None),
            "bootstrap_interval": list(ci) if ci else None,
            "subgroup_degradation": dict(getattr(d, "subgroup_degradation", None) or {}),
            "development_decision": d.decision,
        }
        reasons = [LABEL, *d.reasons]
        if d.decision == "needs_review":
            # model.promotion has no review state: the incumbent stays until a named reviewer decides.
            reasons.append("needs_review: routed to named review; incumbent kept until then")
            decisions[k] = {**base, "decision": "keep_incumbent", "reasons": reasons, "needs_review": True}
            continue
        if d.decision != "promote":
            decisions[k] = {**base, "decision": "keep_incumbent", "reasons": reasons}
            continue
        if status_h == "blocked":
            reasons += [f"locked_holdout_reuse_blocked: {r} already scored {n}x on this holdout "
                        f"(limit {spec.max_accesses} per model version)" for r, n in sorted(blocked.items())]
            decisions[k] = {**base, "decision": "keep_incumbent", "reasons": reasons}
            continue
        ch_ref = model_ref(models[k])
        ch_cells = {_case_key(r): r for r in holdout_scores[ch_ref]["predictions"]}
        in_cells = {_case_key(r): r for r in holdout_scores[inc_ref]["predictions"]}
        pairs = [(c[0], ch_cells[c]["mase_scaled_error"], in_cells[c]["mase_scaled_error"])
                 for c in sorted(set(ch_cells) & set(in_cells))]
        ni_pol = dict(pol.get("holdout_non_inferiority") or {})
        ni = holdout_non_inferiority(
            pairs, margin=float(ni_pol.get("margin", 0.0)), alpha=float(ni_pol.get("alpha", 0.025)),
            require_statistical=bool(ni_pol.get("require_statistical", False)),
        )
        base["holdout_non_inferiority"] = _jsonable(ni)
        reasons.extend(ni.reasons)
        if not ni.passed:
            decisions[k] = {**base, "decision": "keep_incumbent", "reasons": reasons}
            continue
        decisions[k] = {**base, "decision": "promote", "reasons": reasons}
        confirmed.append(k)
    selection: dict[str, Any] | None = None
    if len(confirmed) > 1:
        selection = choose_among_challengers(result.records, confirmed, pol)
        winner = selection["winner"]
        for k in confirmed:
            if winner is None:
                decisions[k]["decision"] = "keep_incumbent"
                decisions[k]["reasons"].append(
                    "requires_review: several challengers passed every gate but none is distinguishable from the "
                    f"others on {selection['n_common']} paired common development cases; incumbent kept")
            elif k != winner:
                decisions[k]["decision"] = "reject"
                decisions[k]["reasons"].append(
                    f"passed all gates but not selected: {model_ref(models[winner])} has a lower paired MASE on "
                    f"{selection['n_common']} common development cases (block-bootstrap interval excludes zero)")
    if any(d["decision"] == "promote" for d in decisions.values()):
        status = "promoted"
    elif any(d.get("needs_review") or any(r.startswith("requires_review") for r in d["reasons"])
             for d in decisions.values()):
        status = "needs_review"
    elif decisions and all(
        any(r.startswith("insufficient_evaluation") for r in d["reasons"]) for d in decisions.values()
    ):
        status = "insufficient_evaluation"
    else:
        status = "incumbent_kept"
    return EvaluationOutcome(
        tournament=result, holdout=spec, holdout_status=status_h, holdout_scores=holdout_scores,
        holdout_blocked=blocked, access_before=access_before, access_after=access_after, decisions=decisions,
        status=status, runtime_seconds=time.perf_counter() - t0, models=models,
        paired=_headline_paired(result, sorted(models)), challenger_selection=selection,
    )


# --------------------------------------------------------------------------- manifest


def finite_json(obj: Any) -> Any:
    """Replace non-finite floats (e.g. an infinite relative degradation) with strings.

    Canonical JSON and jsonb forbid NaN/Infinity; the value is kept explicit
    rather than dropped.
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return "NaN" if math.isnan(obj) else ("Infinity" if obj > 0 else "-Infinity")
    if isinstance(obj, Mapping):
        return {k: finite_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite_json(v) for v in obj]
    return obj


def _jsonable(obj: Any) -> Any:
    """Dataclasses (e.g. tournament paired metrics, when provided) to plain data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _metrics_dict(m: ModelMetrics) -> dict[str, Any]:
    return dataclasses.asdict(m)


def build_manifest(
    outcome: EvaluationOutcome,
    panel: Panel,
    *,
    task_scope: Mapping[str, Any],
    task_key: str,
    seed: int,
    policy_version: str,
    holdout_mode: str,
    prior_runs: int,
    model_version_ids: Mapping[str, str],
    job_id: str | None,
) -> dict[str, Any]:
    from reva.operations.environment import code_digest, environment_digest

    t = outcome.tournament
    inc_ref = model_ref(outcome.models[t.incumbent])
    sub = {f"{g}={v}": {k: _metrics_dict(m) for k, m in per.items()} for (g, v), per in t.subgroup_metrics.items()}
    dev_targets = sorted({(r.series_id, r.target_year, r.target) for r in t.records})
    manifest: dict[str, Any] = finite_json({
        "schema": SCHEMA,
        "kind": "forecast_tournament",
        "label": LABEL,
        "replay_mode": REPLAY_MODE,
        "honesty_statement": HONESTY_STATEMENT,
        "service_version": SERVICE_VERSION,
        "tournament_method": TOURNAMENT_METHOD,
        "job_id": job_id,
        "task_scope": dict(task_scope),
        "task_key": task_key,
        "evaluation_status": outcome.status,
        "inputs": {
            "source": panel.source,
            "panel_digest": panel.digest(),
            "series": panel.meta,
            "dropped": panel.dropped,
        },
        "models": {
            model_ref(m): {
                "model_version_id": model_version_ids[model_ref(m)],
                "role": "incumbent" if k == t.incumbent else "challenger",
                "is_naive_baseline": k == NAIVE_KEY,
            }
            for k, m in sorted(outcome.models.items())
        },
        "incumbent": inc_ref,
        "naive_baseline": model_ref(outcome.models[NAIVE_KEY]),
        "policy": {**dict(t.policy), "policy_version": policy_version},
        "seed": seed,
        "horizons": list(t.horizons),
        "development": {
            "years": [min(y for obs in panel.series.values() for y, _ in obs), outcome.holdout.last_dev_year],
            "predictions": [dataclasses.asdict(r) for r in t.records],
            "targets": [{"series_id": s, "year": y, "value": v} for s, y, v in dev_targets],
            "target_vintage": "latest revised values only; first-release targets unavailable (not archived in V1)",
            "paired_scores": outcome.paired,
            "scores_unpaired": {k: _metrics_dict(m) for k, m in t.metrics.items()},
            "scores_unpaired_note": "each model over its own eligible cases; not comparable across models "
            "(use paired_scores)",
            "challenger_selection": outcome.challenger_selection,
            "subgroup_scores": sub,
            "ineligible": [
                {"series_id": i.series_id, "origin": i.origin, "model": i.report.model_key,
                 "reason": i.report.reason, "n_train": i.report.n_train}
                for i in t.ineligible
            ],
            "tournament_decisions": {
                k: _jsonable(d) for k, d in t.decisions.items()
            },
        },
        "locked_holdout": {
            "key": outcome.holdout.key,
            "years": list(outcome.holdout.years),
            "cells": list(outcome.holdout.cells),
            "mode": holdout_mode,
            "status": outcome.holdout_status,
            "max_accesses_per_model_version": outcome.holdout.max_accesses,
            "access_count_before": outcome.access_before,
            "access_count_after": outcome.access_after,
            "blocked": outcome.holdout_blocked,
            "scores": outcome.holdout_scores,
            "used_for_challenger_selection": False,
        },
        "holdout_usage_count": outcome.access_after,
        "final_decisions": outcome.decisions,
        "researcher_degrees_of_freedom": {
            "candidate_models": "fixed by specification (last_value, log_drift, local_level_log); no search",
            "hyperparameter_tuning_budget": 0,
            "preprocessing": "log transform inside each model's fit, within the training window only",
            "metrics_predeclared": ["mase (primary loss)", "mae (descriptive)", "interval coverage", "interval width"],
            "subgroups_predeclared": ["series_id", "horizon"],
            "horizons": list(t.horizons),
            "holdout_definition": f"last {len(outcome.holdout.years)} calendar years of the panel, fixed by policy",
            "policy_version": policy_version,
            "seed": seed,
            "task_scope_choice": "payload-selected concepts/countries/years (recorded in task_scope)",
            "prior_development_runs_on_this_task": prior_runs,
            "repeated_holdout_use": {r: n for r, n in outcome.access_before.items() if n},
            "multiple_comparisons": "each challenger compared with the incumbent separately; no correction",
        },
        "execution": {
            "code_digest": code_digest(),
            "environment_digest": environment_digest(),
            "tournament_runtime_seconds": round(outcome.runtime_seconds, 3),
        },
    })
    return manifest


# --------------------------------------------------------------------------- DB helpers


def _model_version_ids(conn: Connection, models: Mapping[str, ForecastModel]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in models.values():
        row = conn.execute(
            text("SELECT id, approval_status FROM model.version WHERE model_key = :k AND version = :v"),
            {"k": m.key, "v": m.version},
        ).one_or_none()
        if row is None:
            raise MissingInput(f"model version {model_ref(m)} is not registered (seed the registry)",
                               model=model_ref(m))
        out[model_ref(m)] = str(row.id)
    return out


def holdout_access_counts(conn: Connection, cells: Sequence[str]) -> dict[str, int]:
    """Earlier holdout evaluations, per model version, that touched any of ``cells``."""
    rows = conn.execute(
        text(
            """SELECT v.model_key || '@' || v.version AS ref, count(*) AS n
               FROM model.evaluation e JOIN model.version v ON v.id = e.model_version_id
               WHERE e.task_scope->>'period' = 'locked_holdout'
                 AND e.metrics->>'holdout_status' = 'scored'
                 AND (e.task_scope->'holdout_cells') ?| CAST(:cells AS text[])
               GROUP BY 1"""
        ),
        {"cells": list(cells)},
    ).all()
    return {r.ref: int(r.n) for r in rows}


def incumbent_for_task(conn: Connection, task_key: str) -> str:
    rows = conn.execute(
        text(
            """SELECT v.model_key, v.version, p.decision FROM model.promotion p
               JOIN model.version v ON v.id = p.model_version_id
               WHERE p.task_scope->>'task_key' = :k ORDER BY p.recorded_at, p.id"""
        ),
        {"k": task_key},
    ).all()
    events = [PromotionEvent(f"{r.model_key}@{r.version}", r.decision, task_key, i) for i, r in enumerate(rows)]
    default = model_ref(CANDIDATES[NAIVE_KEY]())
    ref = current_pointer(events, task_key, default)
    key, _, version = ref.partition("@")
    cls = CANDIDATES.get(key)
    if cls is None or cls.version != version:
        raise CapabilityGap(f"incumbent {ref} for this task is not executable in this build", incumbent=ref)
    return key


def _prior_runs(conn: Connection, task_key: str) -> int:
    return int(
        conn.execute(
            text("SELECT count(DISTINCT context_id) FROM model.evaluation "
                 "WHERE task_scope->>'task_key' = :k AND task_scope->>'period' = 'development'"),
            {"k": task_key},
        ).scalar_one()
    )


def _evaluation_policy() -> tuple[dict[str, Any], str, dict[str, Any]]:
    from reva.ontology.registry import load_registry

    promo = load_registry().policies["promotion"]
    mp = dict(promo.get("model_promotion") or {})
    holdout = {**HOLDOUT_DEFAULTS, **dict(mp.get("locked_holdout") or {})}
    return {"model_promotion": mp}, str(promo.get("policy_version", "unversioned")), holdout


# --------------------------------------------------------------------------- payload


def _parse_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    known = {"source", "release_id", "origin", "concepts", "countries", "years", "horizons", "seed", "holdout_mode"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValidationProblem(f"unknown evaluation payload fields {unknown}")
    source = payload.get("source", "belief_release")
    if source not in ("belief_release", "observations"):
        raise ValidationProblem("source must be 'belief_release' or 'observations'")
    concepts = list(payload.get("concepts") or SUPPORTED_CONCEPTS)
    if not concepts or any(c not in SUPPORTED_CONCEPTS for c in concepts):
        raise CapabilityGap(f"concepts must be among {list(SUPPORTED_CONCEPTS)} (positive annual series)")
    countries = list(payload.get("countries") or DEFAULT_COUNTRIES)
    if not countries or any(not (isinstance(c, str) and len(c) == 3 and c.isalpha() and c.isupper())
                            for c in countries):
        raise ValidationProblem("countries must be ISO3 codes")
    years = tuple(payload.get("years") or DEFAULT_YEARS)
    if len(years) != 2 or not all(isinstance(y, int) and not isinstance(y, bool) for y in years) or years[0] > years[1]:
        raise ValidationProblem("years must be [first, last] integers")
    horizons = list(payload.get("horizons") or DEFAULT_HORIZONS)
    if any(not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= MAX_HORIZON for h in horizons):
        raise ValidationProblem(f"horizons must be integers in [1, {MAX_HORIZON}]")
    seed = payload.get("seed", DEFAULT_SEED)
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValidationProblem("seed must be a non-negative integer")
    mode = payload.get("holdout_mode", "on_promotion")
    if mode not in ("on_promotion", "final_report"):
        raise ValidationProblem("holdout_mode must be 'on_promotion' or 'final_report'")
    origin = payload.get("origin", "live")
    if origin not in ("live", "test_fixture"):
        raise ValidationProblem("origin must be 'live' or 'test_fixture'")
    release_id = payload.get("release_id")
    if release_id is not None:
        release_id = UUID(str(release_id))
    return {"source": source, "concepts": sorted(set(concepts)), "countries": sorted(set(countries)),
            "years": (int(years[0]), int(years[1])), "horizons": sorted(set(horizons)), "seed": int(seed),
            "holdout_mode": mode, "origin": origin, "release_id": release_id}


def task_scope_of(p: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    scope = {
        "task": "annual_point_forecast.positive_series",
        "concepts": p["concepts"],
        "countries": p["countries"],
        "years": list(p["years"]),
        "horizons": p["horizons"],
        "replay_mode": REPLAY_MODE,
    }
    return scope, logical_hash(scope)


# --------------------------------------------------------------------------- job handler


def handle_evaluation(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Job handler for ``evaluation`` (see module docstring)."""
    from reva.adapters.postgres import repositories as repo
    from reva.belief.snapshots import release_as_of, release_by_id
    from reva.kernel.context import Origin, assert_publishable

    p = _parse_payload(payload or {})
    scope, task_key = task_scope_of(p)
    policy, policy_version, holdout_cfg = _evaluation_policy()

    with ctx.worker_engine.connect() as conn:
        release: dict[str, Any] | None = None
        if p["source"] == "belief_release":
            release = release_by_id(conn, p["release_id"]) if p["release_id"] else release_as_of(conn, None)
            if release is None or release.get("sequence") is None:
                raise MissingInput("no published belief release is available for evaluation",
                                   release_id=str(p["release_id"]) if p["release_id"] else None)
            panel = load_release_panel(conn, release, concepts=p["concepts"], countries=p["countries"],
                                       years=p["years"])
            origin = str(release["origin"])
            cutoff: datetime = release["knowledge_cutoff"]
        else:
            origin = p["origin"]
            cutoff = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            panel = load_observation_panel(conn, origin=origin, cutoff=cutoff, concepts=p["concepts"],
                                           countries=p["countries"], years=p["years"])
        assert_publishable(Origin(origin), ctx.settings.reva_env)
        if not panel.series:
            raise MissingInput("no strictly positive annual series in the requested scope",
                               dropped=panel.dropped[:20], scope=scope)
        incumbent_key = incumbent_for_task(conn, task_key)
        spec = holdout_spec(panel.series, int(holdout_cfg["years"]), int(holdout_cfg["max_accesses_per_model_version"]))
        prior_access = holdout_access_counts(conn, spec.cells)
        prior_runs = _prior_runs(conn, task_key)
        version_ids = _model_version_ids(conn, {k: cls() for k, cls in CANDIDATES.items()})

    if ctx.heartbeat({"stage": "tournament", "series": len(panel.series)}) == "cancel_requested":
        return {"status": "cancelled"}
    outcome = evaluate(
        panel.series, incumbent_key=incumbent_key, horizons=p["horizons"], seed=p["seed"], policy=policy,
        holdout_years=int(holdout_cfg["years"]),
        max_holdout_accesses=int(holdout_cfg["max_accesses_per_model_version"]),
        prior_holdout_accesses=prior_access, holdout_mode=p["holdout_mode"],
    )
    if ctx.heartbeat({"stage": "persist"}) == "cancel_requested":
        return {"status": "cancelled"}
    manifest = build_manifest(
        outcome, panel, task_scope=scope, task_key=task_key, seed=p["seed"], policy_version=policy_version,
        holdout_mode=p["holdout_mode"], prior_runs=prior_runs, model_version_ids=version_ids,
        job_id=str(ctx.job_id),
    )
    t = outcome.tournament
    actor = f"worker:{ctx.worker_id}"

    with ctx.worker_engine.begin() as conn:
        ctx.assert_lease(conn)
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": HOLDOUT_LOCK})
        if outcome.holdout_scores:
            now_counts = holdout_access_counts(conn, spec.cells)
            raced = {r: n for r, n in now_counts.items()
                     if r in outcome.holdout_scores and n + 1 > spec.max_accesses}
            if raced:
                raise PolicyDenied("locked holdout was scored concurrently; refusing repeated holdout use",
                                   holdout_key=spec.key, counts=raced)
        sha = repo.store_json_artifact(conn, ctx.store, manifest, "evaluation", schema_version="evaluation-manifest/1")
        act = repo.simple_activity(
            "evaluation.tournament", "evaluation", actor,
            {"task_key": task_key, "replay_mode": REPLAY_MODE, "seed": p["seed"], "policy_version": policy_version,
             "holdout_mode": p["holdout_mode"], "job_id": str(ctx.job_id), "label": LABEL},
        )
        inputs = [(release["manifest_sha256"], {"role": "panel_source_release"})] if release else []
        repo.record_activity(conn, act, inputs=inputs, outputs=[sha])
        context_id = repo.create_context(
            conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=cutoff,
            parent_context_id=UUID(str(release["context_id"])) if release else None,
        )
        dev_years = manifest["development"]["years"]
        eval_ids: dict[str, str] = {}
        sub_by_model: dict[str, dict[str, Any]] = defaultdict(dict)
        for (g, v), per in t.subgroup_metrics.items():
            for k, mm in per.items():
                sub_by_model[k][f"{g}={v}"] = _metrics_dict(mm)
        inel_by_model = Counter((i.report.model_key, i.report.reason) for i in t.ineligible)
        for k, m in sorted(outcome.models.items()):
            ref = model_ref(m)
            role = "incumbent" if k == incumbent_key else "challenger"
            metrics = {
                "label": LABEL, "period": "development", "role": role, "is_naive_baseline": k == NAIVE_KEY,
                "paired": {**outcome.paired["metrics"][k], "n_common_cases": outcome.paired["n_common_cases"],
                           "models": outcome.paired["models"]},
                "unpaired": _metrics_dict(t.metrics[k]),
                "unpaired_note": "over this model's own eligible cases; compare models with 'paired'",
                "subgroups": sub_by_model.get(k, {}),
                "ineligible": {reason: n for (mk, reason), n in sorted(inel_by_model.items()) if mk == k},
                "decision": outcome.decisions.get(k, {}).get("decision"),
                "evaluation_status": outcome.status,
            }
            eval_ids[k] = _insert_evaluation(
                conn, version_ids[ref], context_id, sha,
                {**scope, "task_key": task_key, "period": "development", "years_scored": dev_years}, metrics, act.id)
        holdout_eval_ids: dict[str, str] = {}
        for ref, h in sorted(outcome.holdout_scores.items()):
            holdout_eval_ids[ref] = _insert_evaluation(
                conn, version_ids[ref], context_id, sha,
                {**scope, "task_key": task_key, "period": "locked_holdout", "holdout_key": spec.key,
                 "holdout_years": list(spec.years), "holdout_cells": list(spec.cells)},
                {"label": LABEL, "period": "locked_holdout", "holdout_status": "scored",
                 "access_count": outcome.access_after[ref], **h["metrics"],
                 "ineligible": h["ineligible"]},
                act.id)
        promotion_ids: dict[str, str] = {}
        for k, d in sorted(outcome.decisions.items()):
            promotion_ids[k] = str(conn.execute(
                text(
                    """INSERT INTO model.promotion(model_version_id, task_scope, decision, evaluation_id,
                         policy_version, reviewer, reasons)
                       VALUES (:m, CAST(:s AS jsonb), :d, :e, :pv, :rv, CAST(:r AS jsonb)) RETURNING id"""
                ),
                {"m": version_ids[model_ref(outcome.models[k])],
                 "s": json.dumps({**scope, "task_key": task_key}), "d": d["decision"], "e": eval_ids[k],
                 "pv": policy_version, "rv": REVIEWER, "r": json.dumps(d["reasons"])},
            ).scalar_one())
        if outcome.holdout_status == "blocked":
            repo.audit(conn, actor, "evaluation.holdout_reuse_blocked",
                       {"holdout_key": spec.key, "task_key": task_key},
                       {"blocked": outcome.holdout_blocked, "limit": spec.max_accesses, "manifest_sha256": sha})
        result = {
            "status": "succeeded",
            "evaluation_status": outcome.status,
            "label": LABEL,
            "replay_mode": REPLAY_MODE,
            "manifest_sha256": sha,
            "context_id": str(context_id),
            "task_key": task_key,
            "incumbent": model_ref(outcome.models[incumbent_key]),
            "series": len(panel.series),
            "dropped": len(panel.dropped),
            "evaluation_ids": {model_ref(outcome.models[k]): v for k, v in eval_ids.items()},
            "holdout_evaluation_ids": holdout_eval_ids,
            "promotion_ids": {model_ref(outcome.models[k]): v for k, v in promotion_ids.items()},
            "decisions": {model_ref(outcome.models[k]): d["decision"] for k, d in outcome.decisions.items()},
            "holdout": {"status": outcome.holdout_status, "years": list(spec.years),
                        "access_count": outcome.access_after, "blocked": outcome.holdout_blocked},
            "tournament_runtime_seconds": round(outcome.runtime_seconds, 3),
        }
        repo.emit_outbox(conn, "evaluation.tournament_completed", {"job_id": str(ctx.job_id), **result})
        ctx.complete_in(conn, "succeeded", result)
    return result


def _insert_evaluation(
    conn: Connection,
    model_version_id: str,
    context_id: UUID,
    manifest_sha: str,
    task_scope: Mapping[str, Any],
    metrics: Mapping[str, Any],
    activity_id: UUID,
    replay_mode: str = REPLAY_MODE,
) -> str:
    return str(conn.execute(
        text(
            """INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256, replay_mode,
                 task_scope, metrics, provenance_activity_id)
               VALUES (:m, :c, :s, :rm, CAST(:ts AS jsonb), CAST(:mt AS jsonb), :a) RETURNING id"""
        ),
        {"m": model_version_id, "c": context_id, "s": manifest_sha, "rm": replay_mode,
         "ts": json.dumps(finite_json(dict(task_scope)), default=str, allow_nan=False),
         "mt": json.dumps(finite_json(dict(metrics)), default=str, allow_nan=False), "a": activity_id},
    ).scalar_one())


__all__ = [
    "LABEL",
    "REPLAY_MODE",
    "EvaluationOutcome",
    "HoldoutSpec",
    "Panel",
    "build_manifest",
    "evaluate",
    "handle_evaluation",
    "holdout_access_counts",
    "holdout_spec",
    "load_observation_panel",
    "load_release_panel",
    "panel_from_rows",
    "score_holdout",
]
