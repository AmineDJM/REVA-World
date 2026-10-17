"""Scoring of frozen registry forecasts against resolved outcomes (pure).

Every distributional forecast gets the CRPS (closed form for a Gaussian; ``E|X-y| - E|X-X'|/2`` for an
empirical sample), the absolute error of its point, the PIT value ``F(y)`` (calibration) and, for a Gaussian, the
log predictive density. Interval questions add coverage, width and the interval score (Gneiting & Raftery 2007,
eq. 43); binary questions add the Brier score and the log score ``log p(outcome)``. A point-only forecast gets
the absolute error only; every other metric is ``None`` (reported "not applicable", never 0).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from reva.evaluation.registry.forecasts import num, parse_num, predictive_from_record
from reva.evaluation.registry.questions import binary_outcome
from reva.models.forecasting.registry_baselines import Predictive
from reva.models.metrics import crps_gaussian, interval_covers, interval_score, interval_width, log_score_gaussian


def crps_empirical(sample: Sequence[float], y: float) -> float:
    xs = sorted(sample)
    n = len(xs)
    e1 = math.fsum(abs(x - y) for x in xs) / n
    e2 = 2.0 * math.fsum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs)) / (n * n)
    return e1 - 0.5 * e2


def crps(p: Predictive, y: float) -> float | None:
    if p.family == "normal":
        assert p.mu is not None and p.sigma is not None
        return crps_gaussian(y, p.mu, p.sigma)
    if p.family == "empirical":
        return crps_empirical(p.sample, y)
    return None


def log_score(p: Predictive, y: float) -> float | None:
    if p.family == "normal":
        assert p.mu is not None and p.sigma is not None
        return log_score_gaussian(y, p.mu, p.sigma)
    return None


def brier(prob: float, outcome: int) -> float:
    return (prob - outcome) ** 2


def binary_log_score(prob: float, outcome: int) -> float | None:
    q = prob if outcome else 1.0 - prob
    return math.log(q) if q > 0 else None


def score_forecast(question: Mapping[str, Any], forecast: Mapping[str, Any], outcome: str) -> dict[str, Any]:
    """Scores of one issued forecast; numbers as decimal strings (``num``), ``None`` = not applicable."""
    y = float(Decimal(outcome))
    p = predictive_from_record(str(forecast["point"]), forecast.get("predictive"))
    c = crps(p, y)
    ls = log_score(p, y)
    pit = p.cdf(y)
    out: dict[str, Any] = {
        "question_id": question["question_id"],
        "model": forecast["model"],
        "type": question["type"],
        "family": question["family"],
        "abs_error": num(abs(p.point - y)),
        "crps": num(c) if c is not None else None,
        "log_score": num(ls) if ls is not None else None,
        "pit": num(pit) if pit is not None else None,
    }
    if question["type"] == "interval":
        iv = forecast.get("interval")
        if iv:
            lo, hi = parse_num(iv[0]), parse_num(iv[1])
            lvl = float(question["interval_level"])
            out.update(
                covered=interval_covers((lo, hi), y),
                width=num(interval_width((lo, hi))),
                interval_score=num(interval_score((lo, hi), y, lvl)),
                level=lvl,
            )
    if question["type"] == "binary":
        o = binary_outcome(question, y)
        out["outcome_binary"] = o
        pr = forecast.get("prob_above")
        if pr is not None:
            pv = parse_num(pr)
            out["prob_above"] = pr
            out["brier"] = num(brier(pv, o))
            bl = binary_log_score(pv, o)
            out["binary_log_score"] = num(bl) if bl is not None else None
    return out


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _mean(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return math.fsum(vals) / len(vals) if vals else None


def aggregate(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Mean metrics of a set of scored forecasts (``None`` when no forecast has the metric)."""
    cov = [bool(s["covered"]) for s in scores if s.get("covered") is not None]
    return {
        "n_scored": len(scores),
        "mae": _mean(_f(s.get("abs_error")) for s in scores),
        "crps": _mean(_f(s.get("crps")) for s in scores),
        "log_score": _mean(_f(s.get("log_score")) for s in scores),
        "brier": _mean(_f(s.get("brier")) for s in scores),
        "binary_log_score": _mean(_f(s.get("binary_log_score")) for s in scores),
        "interval_score": _mean(_f(s.get("interval_score")) for s in scores),
        "coverage": (sum(cov) / len(cov)) if cov else None,
        "n_intervals": len(cov),
    }


def relative_to(
    scores: Sequence[Mapping[str, Any]], baseline: Sequence[Mapping[str, Any]], metric: str
) -> dict[str, Any]:
    """Ratio mean(metric of model) / mean(metric of baseline) on the questions both scored (None if none)."""
    b = {s["question_id"]: _f(s.get(metric)) for s in baseline}
    pairs = [(_f(s.get(metric)), b.get(s["question_id"])) for s in scores]
    pairs2 = [(a, c) for a, c in pairs if a is not None and c is not None]
    if not pairs2:
        return {"n_common": 0, "ratio": None, "skill": None}
    ma = math.fsum(a for a, _ in pairs2) / len(pairs2)
    mb = math.fsum(c for _, c in pairs2) / len(pairs2)
    ratio = ma / mb if mb > 0 else None
    return {"n_common": len(pairs2), "ratio": ratio, "skill": (1.0 - ratio) if ratio is not None else None}


def calibration(scores: Sequence[Mapping[str, Any]], bins: int = 10) -> dict[str, Any]:
    """PIT histogram (quantity/interval) and reliability table (binary)."""
    pits = [float(s["pit"]) for s in scores if s.get("pit") is not None]
    hist = [0] * bins
    for v in pits:
        hist[min(int(v * bins), bins - 1)] += 1
    rel: list[dict[str, Any]] = []
    pr = [(float(s["prob_above"]), int(s["outcome_binary"])) for s in scores if s.get("prob_above") is not None]
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        cell = [(p, o) for p, o in pr if (lo <= p < hi) or (i == bins - 1 and p == 1.0)]
        rel.append(
            {
                "bin": [lo, hi],
                "n": len(cell),
                "mean_forecast": math.fsum(p for p, _ in cell) / len(cell) if cell else None,
                "observed_frequency": sum(o for _, o in cell) / len(cell) if cell else None,
            }
        )
    cov = [bool(s["covered"]) for s in scores if s.get("covered") is not None]
    return {
        "pit_histogram": {"bins": bins, "counts": hist, "n": len(pits)},
        "reliability": {"bins": rel, "n": len(pr)},
        "interval_coverage": {"nominal": 0.8, "empirical": (sum(cov) / len(cov)) if cov else None, "n": len(cov)},
    }


__all__ = [
    "aggregate",
    "binary_log_score",
    "brier",
    "calibration",
    "crps",
    "crps_empirical",
    "log_score",
    "relative_to",
    "score_forecast",
]
