"""Rolling-origin forecast tournament with a conservative, dependence-aware promotion rule.

Procedure
---------
1. Every model (the naive ``forecast.last_value`` baseline is always added if
   absent) is fitted at each expanding-window origin of each series, using only
   a :class:`~reva.evaluation.splits.CutoffScopedSeries` view whose cutoff is
   the origin year. Origins start at the 3rd observation
   (:data:`MIN_ORIGIN_TRAIN`), the minimum for a defined MASE scale; models
   needing more data return ``IneligibleReport``\\ s, which are recorded, not
   hidden.
2. Each forecast is scored against the value released for the target year:
   absolute error; the MASE-scaled absolute error (scale from the training
   window only, >= 3 consecutive years, ``None`` when undefined); and, for
   probabilistic forecasts only, a coverage indicator, the scaled interval
   score (MSIS) and the log-scale CRPS (proper scores; Gneiting & Raftery
   2007). Models without a probabilistic forecast get ``"not_applicable"``
   proper scores, never 0.
3. Summary metrics are reported per model on its own eligible cases
   (``metrics``, secondary) **and** on the common cases where every compared
   model has a defined scaled error (``paired_metrics``, primary for any
   cross-model ranking; review R3-06).
4. Promotion of each challenger over the incumbent uses the paired
   ``(series, origin, horizon)`` cases where both have a defined scaled error
   and loss differential ``d = challenger - incumbent`` (scaled absolute error):

   * at least ``min_origin_series_pairs`` paired cases (else
     ``insufficient_evaluation``);
   * **paired test per horizon** (:func:`dm_hln_test`): for each horizon ``h``
     the differentials are averaged across series *within each origin year*,
     giving one time series ``dbar_t``. Averaging within the origin makes the
     test robust to arbitrary cross-series (contemporaneous) dependence: the
     origin year, not the series-case, is the unit of inference. ``dbar_t`` is
     tested with the Diebold–Mariano (1995) statistic using the long-run
     variance with lag ``h - 1`` (an h-step forecast error is MA(h-1); the
     overlap of expanding windows enters through the same lag structure under
     the null of equal conditional expected loss) and the Harvey, Leybourne &
     Newbold (1997) small-sample correction with Student-t(T-1) reference
     quantiles. A horizon needs ``paired_test.min_origins_per_horizon`` origin
     years to be tested;
   * the one-sided p-values (H1: challenger has lower expected loss) across
     the tested horizons are combined with Holm's (1979) step-down procedure
     at ``paired_test.alpha`` (family-wise error over horizons); at least one
     horizon must be rejected. Any horizon where the challenger is
     *significantly worse* (one-sided p <= alpha, uncorrected, i.e.
     protective) keeps the incumbent;
   * **subgroup degradation with uncertainty** (review R3-07): for each
     prespecified subgroup (each series, each horizon) the hypothesis "the
     challenger's loss exceeds ``(1 + max_subgroup_degradation)`` times the
     incumbent's" is tested with the same origin-aggregated DM-HLN machinery
     on the contrast ``c - (1 + delta) i``. A *credible* breach (point estimate
     above delta and one-sided p <= ``subgroup_test.alpha``), or a point
     breach in a subgroup too small to test (< ``subgroup_test.min_origins``
     origins), routes the challenger to ``needs_review`` (spec: "no
     prespecified subgroup with more than 10% loss degradation unless
     reviewed"). A point breach that is not credible is reported but does not
     block, so the gate no longer gets stricter as the panel grows.

   Otherwise the decision is ``keep_incumbent`` with explicit reasons.

   Why not the earlier moving-block bootstrap over cases ordered
   ``(series, origin, horizon)`` with block length 3: it ignores dependence
   across series and the long memory of expanding windows; under an exact
   null it promoted 11-15% of the time at a nominal 2.5% (review R3-02). A
   moving-block bootstrap over *origins* (all series jointly) is still computed
   for a **descriptive** interval of the mean loss differential
   (``bootstrap_interval``), with block length
   ``max(policy block_length, ceil(T^(1/3)), max horizon)`` (Künsch 1989;
   Lahiri 2003; Politis & White 2004 give the T^(1/3) rate); it is not the
   decision rule. ``tests/scientific/test_tournament_calibration.py`` checks
   the false-promotion rate of the decision rule under exact-null panels.

5. :func:`holdout_non_inferiority` is the confirmation check for a locked
   holdout (review R3-18): paired cells, explicit margin from policy
   (``holdout_non_inferiority.margin``, default 0 = never accept a challenger
   whose holdout loss is worse than the incumbent's), plus a one-sided
   series-clustered test reported alongside (required only when
   ``require_statistical`` is true).

``replay_mode`` (e.g. ``"revised_data_retrospective"``) is carried into the
result unchanged: this module cannot tell whether the panel holds vintages or
revised data, so the caller must state it.

References: Diebold & Mariano (1995) JBES 13:253-263; Harvey, Leybourne &
Newbold (1997) IJF 13:281-291; Holm (1979) Scand J Stat 6:65-70; Künsch (1989)
Ann Stat 17:1217-1241; Politis & White (2004) Econometric Rev 23:53-70;
Gneiting & Raftery (2007) JASA 102:359-378; Hyndman & Koehler (2006) IJF
22:679-688.
"""

from __future__ import annotations

import math
import zlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from scipy.special import stdtr

from reva.evaluation.splits import CutoffScopedSeries, rolling_origins
from reva.models.forecasting.base import (
    Forecast,
    ForecastModel,
    IneligibleReport,
    Observation,
    PredictiveDistribution,
    to_float,
)
from reva.models.forecasting.last_value import KEY as NAIVE_KEY
from reva.models.forecasting.last_value import LastValue
from reva.models.metrics import (
    MIN_SCALE_POINTS,
    absolute_error,
    aggregate_coverage,
    crps_gaussian,
    crps_student_t,
    interval_covers,
    interval_score,
    mase_scale,
    mean_interval_width,
)

METHOD_VERSION = "reva.models.tournament@2.0.0"
MIN_ORIGIN_TRAIN = MIN_SCALE_POINTS
NOT_APPLICABLE = "not_applicable"
TEST_METHOD = "dm_hln_holm"

DEFAULT_POLICY: dict[str, Any] = {
    "min_origin_series_pairs": 30,
    # Descriptive interval only (moving-block bootstrap over origin years).
    "bootstrap": {"method": "moving_block", "block_length": 3, "replicates": 1000, "level": 0.95},
    # Decision rule (R3-02).
    "paired_test": {"method": TEST_METHOD, "alpha": 0.025, "min_origins_per_horizon": 8},
    # Subgroup degradation gate (R3-07).
    "subgroup_test": {"alpha": 0.05, "min_origins": 5},
    # Locked-holdout confirmation (R3-18), applied by the evaluation service.
    "holdout_non_inferiority": {"margin": 0.0, "alpha": 0.025, "require_statistical": False},
    "primary_loss": "mase",
    "max_subgroup_degradation": 0.10,
    "keep_naive_baseline": True,
    "interval_level": 0.8,
}

_NESTED_KEYS = ("bootstrap", "paired_test", "subgroup_test", "holdout_non_inferiority")


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
    interval_score_scaled: float | None = None  # interval score / MASE scale (MSIS)
    crps_log: float | None = None  # CRPS of the log-scale predictive at log(target)
    scale_reason: str | None = None


@dataclass(frozen=True)
class IneligibleRecord:
    series_id: str
    origin: int
    report: IneligibleReport


ScoreValue = float | str | None  # float, "not_applicable", or None (applicable but undefined)


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
    nominal_level: float | None = None  # stated level of the prediction intervals
    coverage_gap: float | None = None  # empirical coverage - nominal level
    interval_score_scaled: ScoreValue = NOT_APPLICABLE  # mean MSIS (proper)
    n_interval_scores: int = 0
    crps_log: ScoreValue = NOT_APPLICABLE  # mean log-scale CRPS (proper)
    n_crps: int = 0
    n_without_probabilistic: int = 0  # predictions that carry no probabilistic forecast


@dataclass(frozen=True)
class DMTest:
    """Diebold–Mariano test with the HLN small-sample correction on a time series."""

    n_obs: int  # number of time points (origin years)
    mean: float
    statistic: float | None  # HLN-corrected statistic; None when degenerate
    df: int
    lag: int
    kernel: str  # "rectangular" (DM/HLN) or "bartlett" (fallback when the former is <= 0)
    p_less: float  # one-sided p-value, H1: mean < 0
    p_greater: float  # one-sided p-value, H1: mean > 0


@dataclass(frozen=True)
class HorizonTest:
    horizon: int
    n_pairs: int
    n_origins: int
    mean_loss_difference: float
    tested: bool
    test: DMTest | None
    holm_threshold: float | None
    rejected: bool  # challenger significantly better after Holm
    significantly_worse: bool


@dataclass(frozen=True)
class SubgroupTest:
    group: str
    n_pairs: int
    n_origins: int
    relative_degradation: float
    threshold: float
    p_value: float | None  # one-sided, H1: degradation > threshold
    status: Literal["ok", "not_credible", "credible_degradation", "too_few_origins"]


@dataclass(frozen=True)
class PromotionDecision:
    challenger: str
    incumbent: str
    decision: Literal["promote", "keep_incumbent", "needs_review"]
    reasons: tuple[str, ...]
    n_pairs: int
    mean_loss_difference: float | None
    bootstrap_interval: tuple[float, float] | None  # descriptive (origin-block bootstrap)
    subgroup_degradation: Mapping[str, float]
    test_method: str = TEST_METHOD
    alpha: float | None = None
    horizon_tests: Mapping[int, HorizonTest] = field(default_factory=dict)
    subgroup_tests: Mapping[str, SubgroupTest] = field(default_factory=dict)
    n_origins: int = 0  # distinct origin years: the effective unit of inference
    n_series: int = 0
    n_subgroups_tested: int = 0
    paired_mase: tuple[float, float] | None = None  # (challenger, incumbent) on the paired cases
    bootstrap_block_length: int | None = None


@dataclass(frozen=True)
class NonInferiorityCheck:
    """Locked-holdout confirmation (R3-18); see :func:`holdout_non_inferiority`."""

    passed: bool
    rule: Literal["point_estimate", "one_sided_test"]
    margin: float
    alpha: float
    n_pairs: int
    n_clusters: int
    challenger_mean: float | None
    incumbent_mean: float | None
    relative_change: float | None
    p_value: float | None  # one-sided, H1: challenger loss < (1 + margin) * incumbent loss
    statistically_established: bool
    reasons: tuple[str, ...]


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
    # Metrics on the common cases where every compared model has a defined
    # scaled error (use these, not ``metrics``, to rank models; R3-06).
    paired_metrics: Mapping[str, ModelMetrics] = field(default_factory=dict)
    paired_models: tuple[str, ...] = ()
    n_paired_cases: int = 0


# --------------------------------------------------------------------------- policy


def resolve_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a promotion policy (or its ``model_promotion`` block) over the code defaults."""
    src = policy.get("model_promotion", policy) if isinstance(policy, Mapping) else {}
    out = {**DEFAULT_POLICY, **dict(src)}
    for k in _NESTED_KEYS:
        out[k] = {**DEFAULT_POLICY[k], **dict(src.get(k, {}) or {})}
    if "policy_version" in policy:
        out["policy_version"] = policy["policy_version"]
    if out["primary_loss"] != "mase":
        raise ValueError(f"unsupported primary_loss {out['primary_loss']!r}")
    if out["bootstrap"]["method"] != "moving_block":
        raise ValueError(f"unsupported bootstrap method {out['bootstrap']['method']!r}")
    if int(out["bootstrap"]["block_length"]) < 1 or int(out["bootstrap"]["replicates"]) < 1:
        raise ValueError("bootstrap block_length and replicates must be >= 1")
    if out["paired_test"]["method"] != TEST_METHOD:
        raise ValueError(f"unsupported paired_test method {out['paired_test']['method']!r}")
    for blk, key in (("paired_test", "alpha"), ("subgroup_test", "alpha"), ("holdout_non_inferiority", "alpha")):
        a = float(out[blk][key])
        if not (0.0 < a < 0.5):
            raise ValueError(f"{blk}.{key} must be in (0, 0.5)")
    if int(out["paired_test"]["min_origins_per_horizon"]) < 3:
        raise ValueError("paired_test.min_origins_per_horizon must be >= 3")
    if int(out["subgroup_test"]["min_origins"]) < 3:
        raise ValueError("subgroup_test.min_origins must be >= 3")
    if float(out["holdout_non_inferiority"]["margin"]) < 0.0:
        raise ValueError("holdout_non_inferiority.margin must be >= 0")
    return out


_resolve_policy = resolve_policy  # backward-compatible private alias


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


# --------------------------------------------------------------------------- metrics


def _mean_or_none(xs: Sequence[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def _metrics(model: str, recs: Sequence[PredictionRecord], baseline: str) -> ModelMetrics:
    scaled = [r.mase_scaled_error for r in recs if r.mase_scaled_error is not None]
    with_int = [r for r in recs if r.interval is not None]
    levels = sorted({r.level for r in with_int if r.level is not None})
    nominal = levels[0] if len(levels) == 1 else None
    coverage = aggregate_coverage([(r.interval_kind or "", bool(r.covered)) for r in with_int]) if with_int else None
    msis = [r.interval_score_scaled for r in with_int if r.interval_score_scaled is not None]
    crps = [r.crps_log for r in recs if r.crps_log is not None]
    has_predictive = any(r.crps_log is not None for r in recs)
    return ModelMetrics(
        model=model,
        n_predictions=len(recs),
        mae=_mean_or_none([r.abs_error for r in recs]),
        mase=_mean_or_none(scaled),
        n_scaled=len(scaled),
        n_undefined_scale=len(recs) - len(scaled),
        coverage=coverage,
        n_intervals=len(with_int),
        mean_interval_width=mean_interval_width(
            [(r.interval_kind or "", r.interval) for r in with_int if r.interval is not None]
        )
        if with_int
        else None,
        is_baseline=model == baseline,
        nominal_level=nominal,
        coverage_gap=None if coverage is None or nominal is None else coverage - nominal,
        interval_score_scaled=(_mean_or_none(msis) if with_int else NOT_APPLICABLE),
        n_interval_scores=len(msis),
        crps_log=(_mean_or_none(crps) if has_predictive else NOT_APPLICABLE),
        n_crps=len(crps),
        n_without_probabilistic=len(recs) - len(with_int),
    )


def _crps_log(pred: PredictiveDistribution | None, actual: float) -> float | None:
    if pred is None or actual <= 0.0:
        return None
    z = math.log(actual)
    if pred.family == "normal":
        return crps_gaussian(z, pred.loc, pred.scale)
    if pred.df is None or pred.df <= 1.0:
        return None  # infinite CRPS; never reported as a number
    return crps_student_t(z, pred.loc, pred.scale, pred.df)


# --------------------------------------------------------------------------- inference


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


def auto_block_length(n: int, max_horizon: int, floor: int = 1) -> int:
    """Block length ``max(floor, ceil(n^(1/3)), max_horizon)``, capped at ``n``."""
    return max(1, min(n, max(int(floor), math.ceil(n ** (1.0 / 3.0)), int(max_horizon))))


_DEGENERATE_VAR = 1e-24


def dm_hln_test(x: Sequence[float], horizon: int) -> DMTest:
    """Diebold–Mariano test of ``E[x] = 0`` with the HLN small-sample correction.

    ``x`` is a time-ordered loss-differential series (here: one value per
    origin year) for ``horizon``-step forecasts. The long-run variance uses
    the DM rectangular kernel with lag ``h - 1`` (autocovariances with divisor
    T); if that estimate is not positive the Bartlett (Newey–West) kernel with
    the same lag is used instead (always non-negative). The statistic is
    multiplied by ``sqrt((T + 1 - 2h + h(h-1)/T) / T)`` and referred to
    Student-t with ``T - 1`` df (Harvey, Leybourne & Newbold 1997).
    """
    a = np.asarray(x, dtype=float)
    T = int(a.size)
    h = int(horizon)
    if T < 2:
        raise ValueError("the DM test needs at least 2 time points")
    if h < 1:
        raise ValueError("horizon must be >= 1")
    mean = float(a.mean())
    e = a - mean
    lag = min(h - 1, T - 1)
    gam = [float(np.dot(e[k:], e[: T - k])) / T for k in range(lag + 1)]
    kernel = "rectangular"
    lrv = gam[0] + 2.0 * math.fsum(gam[1:])
    if lrv <= 0.0:
        kernel = "bartlett"
        lrv = gam[0] + 2.0 * math.fsum((1.0 - k / (lag + 1.0)) * gam[k] for k in range(1, lag + 1))
    factor_sq = (T + 1.0 - 2.0 * h + h * (h - 1.0) / T) / T
    df = T - 1
    if lrv <= _DEGENERATE_VAR or factor_sq <= 0.0:
        # No variability (or too short for the correction): decide on the sign only
        # when the mean is clearly non-zero, otherwise no evidence either way.
        if lrv <= _DEGENERATE_VAR and factor_sq > 0.0 and abs(mean) > 1e-12:
            p_less, p_greater = (0.0, 1.0) if mean < 0 else (1.0, 0.0)
        else:
            p_less = p_greater = 1.0
        return DMTest(T, mean, None, df, lag, kernel, p_less, p_greater)
    stat = math.sqrt(factor_sq) * mean / math.sqrt(lrv / T)
    return DMTest(
        T,
        mean,
        stat,
        df,
        lag,
        kernel,
        float(stdtr(df, stat)),
        float(stdtr(df, -stat)),
    )


def _by_origin(items: Sequence[tuple[int, float]]) -> list[float]:
    """Average values within each origin year; return them in origin order."""
    acc: dict[int, list[float]] = defaultdict(list)
    for origin, v in items:
        acc[origin].append(v)
    return [math.fsum(acc[o]) / len(acc[o]) for o in sorted(acc)]


def holm_rejections(p_values: Mapping[int, float], alpha: float) -> dict[int, float]:
    """Holm step-down: map of rejected hypothesis -> the threshold it met."""
    order = sorted(p_values.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(order)
    out: dict[int, float] = {}
    for j, (k, p) in enumerate(order):
        thr = alpha / (m - j)
        if p <= thr:
            out[k] = thr
        else:
            break
    return out


def _relative_degradation(ch: float, inc: float) -> float:
    if inc == 0.0:
        return 0.0 if ch == 0.0 else math.inf
    return ch / inc - 1.0


def _subgroup_test(
    group: str,
    items: Sequence[tuple[int, float, float]],
    hac_horizon: int,
    delta: float,
    alpha: float,
    min_origins: int,
) -> SubgroupTest:
    c_mean = math.fsum(c for _, c, _ in items) / len(items)
    i_mean = math.fsum(i for _, _, i in items) / len(items)
    deg = _relative_degradation(c_mean, i_mean)
    contrast = _by_origin([(o, c - (1.0 + delta) * i) for o, c, i in items])
    n_o = len(contrast)
    if deg <= delta:
        status: Literal["ok", "not_credible", "credible_degradation", "too_few_origins"] = "ok"
        p = None
        if n_o >= min_origins:
            p = dm_hln_test(contrast, hac_horizon).p_greater
        return SubgroupTest(group, len(items), n_o, deg, delta, p, status)
    if n_o < min_origins:
        return SubgroupTest(group, len(items), n_o, deg, delta, None, "too_few_origins")
    p = dm_hln_test(contrast, hac_horizon).p_greater
    status = "credible_degradation" if p <= alpha else "not_credible"
    return SubgroupTest(group, len(items), n_o, deg, delta, p, status)


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
    pt = pol["paired_test"]
    alpha = float(pt["alpha"])
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
            alpha=alpha,
        )
    diffs = [c - i for *_, c, i in paired]
    mean_diff = math.fsum(diffs) / n
    paired_mase = (
        math.fsum(c for *_, c, _ in paired) / n,
        math.fsum(i for *_, i in paired) / n,
    )
    origins = sorted({o for _, o, _, _, _ in paired})
    n_series = len({s for s, *_ in paired})
    max_h = max(h for _, _, h, _, _ in paired)

    # Descriptive moving-block bootstrap over origin years (all series jointly).
    b = pol["bootstrap"]
    pooled = np.asarray(_by_origin([(o, c - i) for _, o, _, c, i in paired]), dtype=float)
    block = auto_block_length(int(pooled.size), max_h, int(b["block_length"]))
    ss = np.random.SeedSequence(seed, spawn_key=(zlib.crc32(challenger.encode("utf-8")),))
    means = moving_block_bootstrap_means(pooled, block, int(b["replicates"]), np.random.default_rng(ss))
    a2 = (1.0 - float(b["level"])) / 2.0
    lo, hi = np.quantile(means, [a2, 1.0 - a2])
    ci = (float(lo), float(hi))

    # Prespecified subgroups: each series and each horizon (point estimates kept
    # in ``subgroup_degradation`` for backward compatibility).
    sg_items: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    sg_hac: dict[str, int] = {}
    for sid, origin, h, c, i in paired:
        sg_items[f"series_id={sid}"].append((origin, c, i))
        sg_hac[f"series_id={sid}"] = max_h  # per-origin average over horizons 1..H: MA(H-1)
        sg_items[f"horizon={h}"].append((origin, c, i))
        sg_hac[f"horizon={h}"] = h
    delta = float(pol["max_subgroup_degradation"])
    st = pol["subgroup_test"]
    subgroup_tests = {
        g: _subgroup_test(g, v, sg_hac[g], delta, float(st["alpha"]), int(st["min_origins"]))
        for g, v in sorted(sg_items.items())
    }
    degradation = {g: t.relative_degradation for g, t in subgroup_tests.items()}

    # Paired test per horizon + Holm across horizons.
    min_o = int(pt["min_origins_per_horizon"])
    by_h: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for _sid, origin, h, c, i in paired:
        by_h[h].append((origin, c - i))
    raw: dict[int, tuple[int, int, float, DMTest | None]] = {}
    for h, items in sorted(by_h.items()):
        series_t = _by_origin(items)
        m = math.fsum(v for _, v in items) / len(items)
        test = dm_hln_test(series_t, h) if len(series_t) >= min_o else None
        raw[h] = (len(items), len(series_t), m, test)
    pvals = {h: r[3].p_less for h, r in raw.items() if r[3] is not None}
    rejected = holm_rejections(pvals, alpha)
    horizon_tests = {
        h: HorizonTest(
            horizon=h,
            n_pairs=r[0],
            n_origins=r[1],
            mean_loss_difference=r[2],
            tested=r[3] is not None,
            test=r[3],
            holm_threshold=rejected.get(h),
            rejected=h in rejected,
            significantly_worse=r[3] is not None and r[3].p_greater <= alpha,
        )
        for h, r in raw.items()
    }

    common = dict(
        n_pairs=n,
        mean_loss_difference=mean_diff,
        bootstrap_interval=ci,
        subgroup_degradation=degradation,
        test_method=TEST_METHOD,
        alpha=alpha,
        horizon_tests=horizon_tests,
        subgroup_tests=subgroup_tests,
        n_origins=len(origins),
        n_series=n_series,
        n_subgroups_tested=len(subgroup_tests),
        paired_mase=paired_mase,
        bootstrap_block_length=block,
    )

    def out(decision: str, reasons: list[str]) -> PromotionDecision:
        return PromotionDecision(challenger, incumbent, decision, tuple(reasons), **common)  # type: ignore[arg-type]

    keep: list[str] = []
    if n < min_pairs:
        keep.append(f"insufficient_evaluation: n_pairs={n} < {min_pairs}")
    elif not pvals:
        keep.append(f"insufficient_evaluation: no horizon has >= {min_o} origin years for the paired test")
    elif not rejected:
        detail = ", ".join(f"h={h}: p={p:.3g}" for h, p in sorted(pvals.items()))
        keep.append(
            f"loss_difference_not_significant: no horizon significant after Holm at "
            f"alpha={alpha:g} (DM-HLN one-sided {detail}); descriptive origin-block "
            f"bootstrap interval [{ci[0]:.6g}, {ci[1]:.6g}]"
        )
    worse = sorted(h for h, t in horizon_tests.items() if t.significantly_worse)
    if pvals and worse:
        keep.append(
            "significantly_worse_at_horizon: "
            + ", ".join(f"h={h} (p={horizon_tests[h].test.p_greater:.3g})" for h in worse)  # type: ignore[union-attr]
        )
    if keep:
        return out("keep_incumbent", keep)

    review: list[str] = []
    notes: list[str] = []
    for g, t in subgroup_tests.items():
        if t.status == "credible_degradation":
            review.append(
                f"subgroup_degradation: {g} loss +{t.relative_degradation:.1%} > {delta:.0%} "
                f"and credible (one-sided p={t.p_value:.3g} <= {float(st['alpha']):g}); "
                "promotion requires review"
            )
        elif t.status == "too_few_origins":
            review.append(
                f"subgroup_degradation_untestable: {g} loss +{t.relative_degradation:.1%} > "
                f"{delta:.0%} with only {t.n_origins} origin years (< {int(st['min_origins'])}); "
                "promotion requires review"
            )
        elif t.status == "not_credible":
            notes.append(
                f"subgroup_degradation_not_credible: {g} point +{t.relative_degradation:.1%} > "
                f"{delta:.0%} but one-sided p={t.p_value:.3g} > {float(st['alpha']):g}"
            )
    sig = ", ".join(
        f"h={h}: p={horizon_tests[h].test.p_less:.3g} <= {thr:.3g}"  # type: ignore[union-attr]
        for h, thr in sorted(rejected.items())
    )
    evidence = [
        f"n_pairs={n} >= {min_pairs} over {len(origins)} origin years and {n_series} series",
        f"DM-HLN paired test with Holm across {len(pvals)} horizons at alpha={alpha:g}: {sig}",
    ]
    subgroup_note = f"{len(subgroup_tests)} prespecified subgroups checked for credible degradation > {delta:.0%}"
    if review:
        return out("needs_review", [*evidence, *review, *notes, subgroup_note])
    return out(
        "promote",
        [*evidence, f"no credible subgroup degradation > {delta:.0%}", *notes, subgroup_note],
    )


def holdout_non_inferiority(
    pairs: Sequence[tuple[str, float | None, float | None]],
    *,
    margin: float = 0.0,
    alpha: float = 0.025,
    require_statistical: bool = False,
) -> NonInferiorityCheck:
    """One-sided non-inferiority confirmation on paired locked-holdout cells.

    ``pairs`` holds ``(cluster_id, challenger_scaled_error, incumbent_scaled_error)``
    per holdout cell (cluster = series). Only cells where both errors are
    defined are used (paired comparison on common cells).

    * Rule (always): the challenger's mean holdout loss must not exceed
      ``(1 + margin)`` times the incumbent's. With the default ``margin = 0`` a
      challenger that is worse on the holdout is never confirmed.
    * Reported (and required when ``require_statistical``): a one-sided test of
      H0 ``E[c - (1 + margin) i] >= 0`` against H1 ``< 0`` on per-series means
      (Student-t, ``n_series - 1`` df). The holdout has one origin per series,
      so the test assumes series are independent; cross-series correlation in
      the shared holdout years makes it anti-conservative. With a few dozen
      cells it has little power, which is why it is not required by default:
      the development-period test already established superiority.
    """
    m = float(margin)
    if m < 0 or not math.isfinite(m):
        raise ValueError("margin must be finite and >= 0")
    a = float(alpha)
    rule: Literal["point_estimate", "one_sided_test"] = "one_sided_test" if require_statistical else "point_estimate"
    used = [(str(s), float(c), float(i)) for s, c, i in pairs if c is not None and i is not None]
    if not used:
        return NonInferiorityCheck(
            False,
            rule,
            m,
            a,
            0,
            0,
            None,
            None,
            None,
            None,
            False,
            ("locked_holdout_undefined: no paired holdout cells with a defined scaled error",),
        )
    mc = math.fsum(c for _, c, _ in used) / len(used)
    mi = math.fsum(i for _, _, i in used) / len(used)
    rel = _relative_degradation(mc, mi)
    by_cluster: dict[str, list[float]] = defaultdict(list)
    for s, c, i in used:
        by_cluster[s].append(c - (1.0 + m) * i)
    cm = np.asarray([math.fsum(v) / len(v) for _, v in sorted(by_cluster.items())], dtype=float)
    k = int(cm.size)
    p: float | None = None
    if k >= 2:
        sd = float(cm.std(ddof=1))
        if sd > 0:
            p = float(stdtr(k - 1, float(cm.mean()) / (sd / math.sqrt(k))))
        else:
            p = 0.0 if cm.mean() < 0 else 1.0
    established = p is not None and p <= a
    point_ok = mc <= (1.0 + m) * mi
    reasons: list[str] = []
    stat_txt = (
        f"one-sided series-clustered p={p:.3g} (alpha={a:g}, {k} series)"
        if p is not None
        else f"one-sided test not computable ({k} series)"
    )
    if not point_ok:
        reasons.append(
            f"locked_holdout_regression: challenger holdout MASE {mc:.4g} vs incumbent {mi:.4g} "
            f"({rel:+.1%} > margin {m:.0%}); {stat_txt}"
        )
    elif require_statistical and not established:
        reasons.append(
            f"locked_holdout_non_inferiority_not_established: challenger {mc:.4g} vs incumbent "
            f"{mi:.4g} ({rel:+.1%}); {stat_txt}"
        )
    passed = point_ok and (established or not require_statistical)
    if passed:
        reasons.append(
            f"locked_holdout_confirmed: challenger holdout MASE {mc:.4g} <= (1+{m:g}) x incumbent "
            f"{mi:.4g} on {len(used)} paired cells; {stat_txt}"
            + ("" if require_statistical else " (non-inferiority screen; holdout sample is small)")
        )
    return NonInferiorityCheck(passed, rule, m, a, len(used), k, mc, mi, rel, p, established, tuple(reasons))


# --------------------------------------------------------------------------- tournament


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
    pol = resolve_policy(policy)
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
            scale = mase_scale([to_float(v) for _, v in train], years=[y for y, _ in train])
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
                    msis = None
                    if fc.interval is not None and fc.level is not None and scale.value is not None:
                        msis = interval_score(fc.interval, y, fc.level) / scale.value
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
                            covered=None if fc.interval is None else interval_covers(fc.interval, y),
                            interval_score_scaled=msis,
                            crps_log=_crps_log(fc.predictive, y),
                            scale_reason=scale.reason,
                        )
                    )

    by_model: dict[str, list[PredictionRecord]] = defaultdict(list)
    by_group: dict[tuple[str, str], dict[str, list[PredictionRecord]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_model[r.model].append(r)
        by_group[("series_id", r.series_id)][r.model].append(r)
        by_group[("horizon", str(r.horizon))][r.model].append(r)
    metrics = {k: _metrics(k, by_model.get(k, []), NAIVE_KEY) for k in model_keys}
    subgroup_metrics = {
        g: {k: _metrics(k, recs, NAIVE_KEY) for k, recs in sorted(per.items())} for g, per in sorted(by_group.items())
    }

    # Common-case (paired) metrics over the models that produced any scored case.
    scored_keys: dict[str, set[tuple[str, int, int]]] = {
        k: {(r.series_id, r.origin, r.horizon) for r in by_model.get(k, []) if r.mase_scaled_error is not None}
        for k in model_keys
    }
    paired_models = tuple(k for k in model_keys if scored_keys[k])
    common: set[tuple[str, int, int]] = (
        set.intersection(*(scored_keys[k] for k in paired_models)) if paired_models else set()
    )
    paired_metrics = {
        k: _metrics(
            k,
            [r for r in by_model.get(k, []) if (r.series_id, r.origin, r.horizon) in common],
            NAIVE_KEY,
        )
        for k in paired_models
    }

    by_key = {(r.series_id, r.origin, r.horizon, r.model): r for r in records}
    decisions = {k: _decide(k, incumbent_key, by_key, cases, pol, seed) for k in model_keys if k != incumbent_key}
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
        paired_metrics=paired_metrics,
        paired_models=paired_models,
        n_paired_cases=len(common),
    )
