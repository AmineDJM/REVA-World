"""Rolling-origin forecast tournament with a conservative promotion rule.

Procedure
---------
1. Every model (the naive ``forecast.last_value`` baseline is always added if
   absent) is fitted at each expanding-window origin of each series, using only
   a :class:`~reva.evaluation.splits.CutoffScopedSeries` view whose cutoff is
   the origin year. Origins start at the 2nd observation (MASE needs >= 2
   training values); models needing more data return ``IneligibleReport``\\ s,
   which are recorded, not hidden.
2. Each forecast is scored against the value released for the target year:
   absolute error, the MASE-scaled absolute error (scale from the training
   window only; ``None`` when undefined) and, for prediction intervals only,
   a coverage indicator.
3. Promotion of each challenger over the incumbent uses paired
   ``(series, origin, horizon)`` cases where both have a defined scaled error:

   * at least ``min_origin_series_pairs`` paired cases (else
     ``insufficient_evaluation``);
   * the moving-block bootstrap (block length, replicates and level from the
     policy; ``numpy.random.Generator`` seeded from ``seed`` and the challenger
     key) percentile interval of the mean loss difference
     ``challenger - incumbent`` lies entirely below zero. Cases are ordered by
     ``(series_id, origin, horizon)`` so blocks preserve serial dependence
     within a series;
   * no prespecified subgroup (each series, each horizon) has mean challenger
     loss more than ``max_subgroup_degradation`` (relative) above the
     incumbent's.

   Otherwise the decision is ``keep_incumbent`` with explicit reasons.

``replay_mode`` (e.g. ``"revised_data_retrospective"``) is carried into the
result unchanged: this module cannot tell whether the panel holds vintages or
revised data, so the caller must state it.
"""

from __future__ import annotations

import math
import zlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from reva.evaluation.splits import CutoffScopedSeries, rolling_origins
from reva.models.forecasting.base import (
    Forecast,
    ForecastModel,
    IneligibleReport,
    Observation,
    to_float,
)
from reva.models.forecasting.last_value import KEY as NAIVE_KEY
from reva.models.forecasting.last_value import LastValue
from reva.models.metrics import (
    absolute_error,
    aggregate_coverage,
    interval_covers,
    mase_scale,
    mean_interval_width,
)

METHOD_VERSION = "reva.models.tournament@1.0.0"
MIN_ORIGIN_TRAIN = 2

DEFAULT_POLICY: dict[str, Any] = {
    "min_origin_series_pairs": 30,
    "bootstrap": {"method": "moving_block", "block_length": 3, "replicates": 1000, "level": 0.95},
    "primary_loss": "mase",
    "max_subgroup_degradation": 0.10,
    "keep_naive_baseline": True,
    "interval_level": 0.8,
}


@dataclass(frozen=True)
class PredictionRecord:
    series_id: str
    origin: int
    horizon: int
    target_year: int
    model: str
    model_version: str
    point: float
    interval: tuple[float, float] | None
    interval_kind: str | None
    level: float | None
    target: float
    abs_error: float
    mase_scaled_error: float | None
    scale_flag: str | None
    covered: bool | None


@dataclass(frozen=True)
class IneligibleRecord:
    series_id: str
    origin: int
    report: IneligibleReport


@dataclass(frozen=True)
class ModelMetrics:
    model: str
    n_predictions: int
    mae: float | None  # pooled across series; scale-dependent, descriptive only
    mase: float | None  # mean scaled absolute error over cases with a defined scale
    n_scaled: int
    n_undefined_scale: int
    coverage: float | None  # prediction intervals only
    n_intervals: int
    mean_interval_width: float | None
    is_baseline: bool


@dataclass(frozen=True)
class PromotionDecision:
    challenger: str
    incumbent: str
    decision: Literal["promote", "keep_incumbent"]
    reasons: tuple[str, ...]
    n_pairs: int
    mean_loss_difference: float | None
    bootstrap_interval: tuple[float, float] | None
    subgroup_degradation: Mapping[str, float]


@dataclass(frozen=True)
class TournamentResult:
    replay_mode: str
    seed: int
    method_version: str
    policy: Mapping[str, Any]
    horizons: tuple[int, ...]
    incumbent: str
    baseline: str
    records: tuple[PredictionRecord, ...]
    metrics: Mapping[str, ModelMetrics]
    subgroup_metrics: Mapping[tuple[str, str], Mapping[str, ModelMetrics]]
    ineligible: tuple[IneligibleRecord, ...]
    decisions: Mapping[str, PromotionDecision]


def _resolve_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    src = policy.get("model_promotion", policy) if isinstance(policy, Mapping) else {}
    out = {**DEFAULT_POLICY, **dict(src)}
    out["bootstrap"] = {**DEFAULT_POLICY["bootstrap"], **dict(src.get("bootstrap", {}))}
    if "policy_version" in policy:
        out["policy_version"] = policy["policy_version"]
    if out["primary_loss"] != "mase":
        raise ValueError(f"unsupported primary_loss {out['primary_loss']!r}")
    if out["bootstrap"]["method"] != "moving_block":
        raise ValueError(f"unsupported bootstrap method {out['bootstrap']['method']!r}")
    if int(out["bootstrap"]["block_length"]) < 1 or int(out["bootstrap"]["replicates"]) < 1:
        raise ValueError("bootstrap block_length and replicates must be >= 1")
    return out


def _prepare_panel(
    panel: Mapping[str, Sequence[Observation]],
) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for sid in sorted(panel):
        obs = sorted(((int(y), to_float(v)) for y, v in panel[sid]), key=lambda o: o[0])
        years = [y for y, _ in obs]
        if len(set(years)) != len(years):
            raise ValueError(f"series {sid!r} has duplicate years")
        out[sid] = obs
    return out


def _metrics(model: str, recs: Sequence[PredictionRecord], baseline: str) -> ModelMetrics:
    scaled = [r.mase_scaled_error for r in recs if r.mase_scaled_error is not None]
    with_int = [r for r in recs if r.interval is not None]
    return ModelMetrics(
        model=model,
        n_predictions=len(recs),
        mae=math.fsum(r.abs_error for r in recs) / len(recs) if recs else None,
        mase=math.fsum(scaled) / len(scaled) if scaled else None,
        n_scaled=len(scaled),
        n_undefined_scale=len(recs) - len(scaled),
        coverage=aggregate_coverage([(r.interval_kind or "", bool(r.covered)) for r in with_int])
        if with_int
        else None,
        n_intervals=len(with_int),
        mean_interval_width=mean_interval_width(
            [(r.interval_kind or "", r.interval) for r in with_int if r.interval is not None]
        )
        if with_int
        else None,
        is_baseline=model == baseline,
    )


def moving_block_bootstrap_means(
    x: np.ndarray, block_length: int, replicates: int, rng: np.random.Generator
) -> np.ndarray:
    """Means of ``replicates`` moving-block bootstrap resamples of ``x`` (in order)."""
    n = int(x.size)
    if n == 0:
        raise ValueError("cannot bootstrap an empty sample")
    L = min(block_length, n)
    n_blocks = -(-n // L)
    starts = rng.integers(0, n - L + 1, size=(replicates, n_blocks))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]).reshape(replicates, -1)[:, :n]
    return np.asarray(x[idx].mean(axis=1), dtype=float)


def _relative_degradation(ch: float, inc: float) -> float:
    if inc == 0.0:
        return 0.0 if ch == 0.0 else math.inf
    return ch / inc - 1.0


def _decide(
    challenger: str,
    incumbent: str,
    by_key: Mapping[tuple[str, int, int, str], PredictionRecord],
    cases: Sequence[tuple[str, int, int]],
    pol: Mapping[str, Any],
    seed: int,
) -> PromotionDecision:
    paired: list[tuple[str, int, int, float, float]] = []
    for sid, origin, h in cases:
        rc = by_key.get((sid, origin, h, challenger))
        ri = by_key.get((sid, origin, h, incumbent))
        if rc is None or ri is None:
            continue
        if rc.mase_scaled_error is None or ri.mase_scaled_error is None:
            continue
        paired.append((sid, origin, h, rc.mase_scaled_error, ri.mase_scaled_error))
    paired.sort(key=lambda p: (p[0], p[1], p[2]))
    n = len(paired)
    min_pairs = int(pol["min_origin_series_pairs"])
    reasons: list[str] = []
    if n == 0:
        return PromotionDecision(
            challenger,
            incumbent,
            "keep_incumbent",
            (f"insufficient_evaluation: n_pairs=0 < {min_pairs}",),
            0,
            None,
            None,
            {},
        )
    diffs = np.array([c - i for *_, c, i in paired], dtype=float)
    mean_diff = float(diffs.mean())

    # Prespecified subgroups: each series and each horizon.
    groups: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for sid, _origin, h, c, i in paired:
        groups[f"series_id={sid}"].append((c, i))
        groups[f"horizon={h}"].append((c, i))
    degradation = {
        g: _relative_degradation(
            math.fsum(c for c, _ in v) / len(v), math.fsum(i for _, i in v) / len(v)
        )
        for g, v in sorted(groups.items())
    }

    ci: tuple[float, float] | None = None
    if n < min_pairs:
        reasons.append(f"insufficient_evaluation: n_pairs={n} < {min_pairs}")
    else:
        b = pol["bootstrap"]
        ss = np.random.SeedSequence(seed, spawn_key=(zlib.crc32(challenger.encode("utf-8")),))
        rng = np.random.default_rng(ss)
        means = moving_block_bootstrap_means(
            diffs, int(b["block_length"]), int(b["replicates"]), rng
        )
        alpha = 1.0 - float(b["level"])
        lo, hi = np.quantile(means, [alpha / 2.0, 1.0 - alpha / 2.0])
        ci = (float(lo), float(hi))
        if not ci[1] < 0.0:
            reasons.append(
                f"loss_difference_not_significant: bootstrap {b['level']} interval "
                f"[{ci[0]:.6g}, {ci[1]:.6g}] does not lie entirely below zero"
            )
    max_deg = float(pol["max_subgroup_degradation"])
    for g, d in degradation.items():
        if d > max_deg:
            reasons.append(f"subgroup_degradation: {g} loss +{d:.1%} > {max_deg:.0%}")
    if reasons:
        return PromotionDecision(
            challenger, incumbent, "keep_incumbent", tuple(reasons), n, mean_diff, ci, degradation
        )
    assert ci is not None
    return PromotionDecision(
        challenger,
        incumbent,
        "promote",
        (
            f"n_pairs={n} >= {min_pairs}",
            f"bootstrap interval [{ci[0]:.6g}, {ci[1]:.6g}] entirely below zero",
            f"no prespecified subgroup degrades by more than {max_deg:.0%}",
        ),
        n,
        mean_diff,
        ci,
        degradation,
    )


def run_tournament(
    panel: Mapping[str, Sequence[Observation]],
    candidates: Sequence[ForecastModel],
    incumbent_key: str = NAIVE_KEY,
    horizons: Sequence[int] = (1, 2, 3),
    *,
    replay_mode: str,
    seed: int,
    policy: Mapping[str, Any],
) -> TournamentResult:
    if not replay_mode:
        raise ValueError("replay_mode must be stated explicitly")
    pol = _resolve_policy(policy)
    hs = tuple(sorted(set(horizons)))
    level = float(pol["interval_level"])

    models: dict[str, ForecastModel] = {}
    for m in candidates:
        if m.key in models:
            raise ValueError(f"duplicate candidate {m.key!r}")
        models[m.key] = m
    if NAIVE_KEY not in models:  # the naive baseline is always evaluated
        models[NAIVE_KEY] = LastValue()
    if incumbent_key not in models:
        raise ValueError(f"incumbent {incumbent_key!r} is not among the candidates")
    model_keys = sorted(models)

    data = _prepare_panel(panel)
    records: list[PredictionRecord] = []
    ineligible: list[IneligibleRecord] = []
    cases: list[tuple[str, int, int]] = []
    for sid, obs in data.items():
        actual = dict(obs)
        for ro in rolling_origins(actual.keys(), MIN_ORIGIN_TRAIN, hs):
            origin_year = ro.train_years[-1]
            scoped = CutoffScopedSeries(obs, cutoff=origin_year)
            train = scoped.observations()
            scale = mase_scale([to_float(v) for _, v in train])
            for h in ro.steps:
                cases.append((sid, origin_year, h))
            for key in model_keys:
                model = models[key]
                fitted = model.fit(train)  # type: ignore[arg-type]
                if isinstance(fitted, IneligibleReport):
                    ineligible.append(IneligibleRecord(sid, origin_year, fitted))
                    continue
                fcs: list[Forecast] = fitted.predict(ro.steps, level=level)
                for fc in fcs:
                    y = actual[fc.target_year]
                    ae = absolute_error(fc.point, y)
                    records.append(
                        PredictionRecord(
                            series_id=sid,
                            origin=origin_year,
                            horizon=fc.horizon,
                            target_year=fc.target_year,
                            model=fc.model_key,
                            model_version=fc.model_version,
                            point=fc.point,
                            interval=fc.interval,
                            interval_kind=fc.interval_kind,
                            level=fc.level,
                            target=y,
                            abs_error=ae,
                            mase_scaled_error=None if scale.value is None else ae / scale.value,
                            scale_flag=scale.flag,
                            covered=None
                            if fc.interval is None
                            else interval_covers(fc.interval, y),
                        )
                    )

    by_model: dict[str, list[PredictionRecord]] = defaultdict(list)
    by_group: dict[tuple[str, str], dict[str, list[PredictionRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in records:
        by_model[r.model].append(r)
        by_group[("series_id", r.series_id)][r.model].append(r)
        by_group[("horizon", str(r.horizon))][r.model].append(r)
    metrics = {k: _metrics(k, by_model.get(k, []), NAIVE_KEY) for k in model_keys}
    subgroup_metrics = {
        g: {k: _metrics(k, recs, NAIVE_KEY) for k, recs in sorted(per.items())}
        for g, per in sorted(by_group.items())
    }

    by_key = {(r.series_id, r.origin, r.horizon, r.model): r for r in records}
    decisions = {
        k: _decide(k, incumbent_key, by_key, cases, pol, seed)
        for k in model_keys
        if k != incumbent_key
    }
    return TournamentResult(
        replay_mode=replay_mode,
        seed=seed,
        method_version=METHOD_VERSION,
        policy=pol,
        horizons=hs,
        incumbent=incumbent_key,
        baseline=NAIVE_KEY,
        records=tuple(records),
        metrics=metrics,
        subgroup_metrics=subgroup_metrics,
        ineligible=tuple(ineligible),
        decisions=decisions,
    )
