"""Scores, paired tests and the development / locked-holdout gate (V6, agent PRED-MODELS).

Scores per case and model (``None`` = not applicable, never 0):

* ``ae`` absolute error of the point;
* ``crps``: closed form for a Gaussian, ``E|X-y| - E|X-X'|/2`` for a sample (``registry.scores.crps``);
* ``log``: log predictive density — Gaussian closed form; for a sample, a Gaussian kernel density with
  Silverman's bandwidth (the convention of ``scoringRules::logs_sample``);
* ``pit``, ``cov80`` (central 80 % interval covers the outcome), ``is80`` (interval score, G&R 2007 eq. 43).

Paired comparison of a candidate with a benchmark on one (task, variable, horizon) cell: common cases with a
defined loss for both; the loss differential is averaged across series **within each origin** (origin = unit of
inference, robust to cross-country correlation) and tested with the Diebold–Mariano statistic with the Harvey,
Leybourne & Newbold correction (``reva.models.tournament.dm_hln_test``, the existing tournament's test); one-sided
H1: the candidate has the lower expected loss. Multiplicity: Holm (``reva.models.tournament.holm_rejections``)
within each development family and over **all** holdout hypotheses jointly.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from reva.evaluation.pred_models.realtime import Case
from reva.evaluation.registry.scores import crps
from reva.models.forecasting.registry_baselines import Predictive
from reva.models.metrics import interval_score, log_score_gaussian
from reva.models.tournament import dm_hln_test, holm_rejections

LEVEL = 0.8
ALPHA = 0.05


def kde_log_density(sample: Sequence[float], y: float) -> float | None:
    x = np.asarray(sample, dtype=float)
    n = len(x)
    sd = float(x.std(ddof=1)) if n > 1 else 0.0
    iqr = float(np.subtract(*np.percentile(x, [75, 25])))
    s = min(sd, iqr / 1.34) if iqr > 0 else sd
    if s <= 0:
        return None
    h = 0.9 * s * n ** (-0.2)
    z = (y - x) / h
    m = float(np.max(-0.5 * z * z))
    dens = math.exp(m) * float(np.mean(np.exp(-0.5 * z * z - m))) / (h * math.sqrt(2 * math.pi))
    return math.log(dens) if dens > 0 else None


def score(p: object, y: float) -> dict[str, Any] | None:
    if not isinstance(p, Predictive):
        return None
    out: dict[str, Any] = {"ae": abs(p.point - y), "crps": None, "log": None, "pit": None, "cov80": None, "is80": None}
    if not p.distributional:
        return out
    out["crps"] = crps(p, y)
    if p.family == "normal":
        assert p.mu is not None and p.sigma is not None
        out["log"] = log_score_gaussian(y, p.mu, p.sigma)
    else:
        out["log"] = kde_log_density(p.sample, y)
    out["pit"] = p.cdf(y)
    iv = p.interval(LEVEL)
    if iv is not None:
        out["cov80"] = iv[0] <= y <= iv[1]
        out["is80"] = interval_score(iv, y, LEVEL)
    return out


@dataclass
class Scored:
    case: Case
    scores: dict[str, dict[str, Any]]


def score_cases(cases: Iterable[Case]) -> list[Scored]:
    out = []
    for c in cases:
        if c.outcome is None:
            continue
        sc = {k: s for k, p in c.preds.items() if (s := score(p, c.outcome)) is not None}
        out.append(Scored(c, sc))
    return out


def dm_lag(task: str, horizon: str) -> int:
    """The DM-HLN ``horizon`` argument (lag = value - 1) per task cell, chosen for overlapping origins."""
    if task == "weo":
        return 2 if horizon == "h0" else 3
    if task == "us":
        return int(horizon.removeprefix("spf"))
    if task == "g7":
        return int(horizon.removeprefix("k")) + 2
    raise ValueError(task)


def split_of(task: str, origin: datetime, splits: Mapping[str, str]) -> str:
    return "holdout" if origin.date().isoformat() >= splits[task] else "development"


def summary(rows: Sequence[Scored], model: str) -> dict[str, Any]:
    vals = [r.scores[model] for r in rows if model in r.scores]

    def mean(k: str) -> float | None:
        xs = [v[k] for v in vals if v.get(k) is not None]
        return math.fsum(float(x) for x in xs) / len(xs) if xs else None

    pits = [float(v["pit"]) for v in vals if v.get("pit") is not None]
    hist = [0] * 10
    for u in pits:
        hist[min(int(u * 10), 9)] += 1
    ae = [v["ae"] for v in vals]
    return {
        "n": len(vals),
        "mae": mean("ae"),
        "median_ae": float(np.median(ae)) if ae else None,
        "crps": mean("crps"),
        "log": mean("log"),
        "coverage80": mean("cov80"),
        "is80": mean("is80"),
        "pit_hist10": hist if pits else None,
    }


def paired_test(rows: Sequence[Scored], cand: str, bench: str, loss: str, lag: int) -> dict[str, Any]:
    by_o: dict[datetime, list[float]] = defaultdict(list)
    lc, lb = [], []
    for r in rows:
        a, b = r.scores.get(cand), r.scores.get(bench)
        if a is None or b is None or a.get(loss) is None or b.get(loss) is None:
            continue
        x, z = float(a[loss]), float(b[loss])
        if loss == "log":  # log score is positively oriented: loss = -log density
            x, z = -x, -z
        by_o[r.case.origin].append(x - z)
        lc.append(x)
        lb.append(z)
    d = [math.fsum(v) / len(v) for _o, v in sorted(by_o.items())]
    res: dict[str, Any] = {
        "candidate": cand,
        "benchmark": bench,
        "loss": loss,
        "n_pairs": len(lc),
        "n_origins": len(d),
        "loss_candidate": math.fsum(lc) / len(lc) if lc else None,
        "loss_benchmark": math.fsum(lb) / len(lb) if lb else None,
    }
    res["relative"] = (
        res["loss_candidate"] / res["loss_benchmark"]
        if lc and res["loss_benchmark"] and res["loss_benchmark"] > 0
        else None
    )
    if len(d) < 6:
        res.update(tested=False, p_better=None, p_worse=None, reason="fewer than 6 origins")
        return res
    t = dm_hln_test(d, lag)
    res.update(
        tested=True, dm_stat=t.statistic, dm_lag=t.lag, kernel=t.kernel, p_better=t.p_less, p_worse=t.p_greater
    )
    return res


def holm(tests: list[dict[str, Any]], alpha: float = ALPHA) -> None:
    """Annotates ``tests`` (in place) with the Holm decision over the tested ones."""
    pv = {i: float(t["p_better"]) for i, t in enumerate(tests) if t.get("tested")}
    rej = holm_rejections(pv, alpha)
    for i, t in enumerate(tests):
        t["holm_m"] = len(pv)
        t["holm_rejected"] = i in rej
        t["holm_threshold"] = rej.get(i)


__all__ = [
    "ALPHA",
    "LEVEL",
    "Scored",
    "dm_lag",
    "holm",
    "kde_log_density",
    "paired_test",
    "score",
    "score_cases",
    "split_of",
    "summary",
]
