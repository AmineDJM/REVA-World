"""Fair density benchmark for densities wrapped around an official point forecast (FIX7, QA-PRED finding X1).

PRED-MODELS registered three densities centred on the IMF WEO / SPF point (``forecast.weo_conformal``,
``forecast.weo_bias_corrected``, ``forecast.spf_conformal``) after beating ``benchmark.imf_weo_gauss`` /
``benchmark.spf_gauss`` = N(official point, sigma of REVA's *persistence* predictive). That spread is not the
official forecaster's: its 80 % interval covered 93-100 % of holdout outcomes. The standard "error-based fan" is
the fair benchmark: **N(official point, sigma = root mean square of the official forecaster's own past errors
whose outcome was published at the origin)**. Two variants:

* ``own`` — per series (country / US): the series' own past errors at the same horizon (>= ``MIN_OWN``);
* ``pooled`` — errors pooled across the task's series, each normalised by its series scale (``Case.scale``) and
  rescaled by the target series' scale (>= ``MIN_POOLED``): the Gaussian twin of the conformal density, i.e. the
  hardest benchmark (only the Gaussian shape differs).

Only past errors with ``outcome_known_at <= origin`` are used (same rule as ``derived``). This module is kept out
of ``service.SOURCE_FILES``: the locked-holdout protocol's code digest is unchanged, and the holdout (opened once,
spent) is never re-scored here — :func:`fair_development` builds and scores **development origins only**.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import polars as pl

from reva.evaluation.pred_models import gate as G
from reva.evaluation.pred_models import realtime as R
from reva.evaluation.pred_models.derived import conf_key
from reva.models.forecasting.registry_baselines import Abstain, Predictive

MIN_OWN = 8
MIN_POOLED = 15
OFFICIAL = {"weo": R.WEO, "us": R.SPF}
OLD_BENCH = {"weo": R.WEO_GAUSS, "us": R.SPF_GAUSS}
OWN = {"weo": "benchmark.imf_weo_errfan", "us": "benchmark.spf_errfan"}
POOLED = {"weo": "benchmark.imf_weo_errfan_pooled", "us": "benchmark.spf_errfan_pooled"}
#: density candidates compared with the benchmarks (the three registered ones first)
CANDIDATES = {
    "weo": (conf_key(R.WEO), "forecast.weo_bias_corrected", "forecast.combo_weo_ar"),
    "us": (conf_key(R.SPF), "forecast.combo_spf_bvar", "forecast.combo_spf_ar"),
}
REGISTERED = {
    ("weo", "real_gdp_growth", "h0", "forecast.weo_conformal"),
    ("weo", "real_gdp_growth", "h0", "forecast.weo_bias_corrected"),
    ("us", "real_gdp_growth", "spf2", "forecast.spf_conformal"),
}
CLAIM = (
    "calibrated intervals around the official (IMF WEO / SPF) point - not a better point forecast; the point is the "
    "official forecaster's, attributed"
)


def error_fan(point: float, errs: Sequence[float], min_n: int, scale: float = 1.0) -> Predictive | Abstain:
    """N(point, scale * RMS(errs)); abstains with fewer than ``min_n`` past errors or a zero spread."""
    if len(errs) < min_n:
        return Abstain(f"fewer than {min_n} past official errors known at the origin ({len(errs)})")
    rms = math.sqrt(math.fsum(e * e for e in errs) / len(errs)) * scale
    if not rms > 0:
        return Abstain("zero past error spread")
    return Predictive("normal", point, mu=point, sigma=rms)


class _Book:
    def __init__(self) -> None:
        self.known: list[float] = []
        self.errs: list[float] = []

    def add(self, t: float, e: float) -> None:
        i = bisect.bisect_right(self.known, t)
        self.known.insert(i, t)
        self.errs.insert(i, e)

    def until(self, t: float) -> list[float]:
        return self.errs[: bisect.bisect_right(self.known, t)]


def add_error_fans(cases: Iterable[R.Case]) -> None:
    """Adds the ``own`` and ``pooled`` error-fan benchmarks to the WEO / US cases in place."""
    groups: dict[tuple[str, str, str], list[R.Case]] = defaultdict(list)
    for c in cases:
        if c.task in OFFICIAL:
            groups[(c.task, c.variable, c.horizon)].append(c)
    for (task, _v, _h), grp in groups.items():
        off = OFFICIAL[task]
        own: dict[str, _Book] = defaultdict(_Book)
        pooled = _Book()
        by_origin: dict[float, list[R.Case]] = defaultdict(list)
        for c in grp:
            by_origin[c.origin.timestamp()].append(c)
        for ts in sorted(by_origin):
            batch = by_origin[ts]
            for c in batch:
                p = c.preds.get(off)
                if not isinstance(p, Predictive):
                    c.preds[OWN[task]] = c.preds[POOLED[task]] = Abstain("no official point")
                    continue
                c.preds[OWN[task]] = error_fan(p.point, own[c.geo].until(ts), MIN_OWN)
                c.preds[POOLED[task]] = (
                    error_fan(p.point, pooled.until(ts), MIN_POOLED, c.scale)
                    if c.scale
                    else Abstain("no series scale at the origin")
                )
            for c in batch:  # this origin's errors become usable once their outcome is published
                p = c.preds.get(off)
                if not isinstance(p, Predictive) or c.outcome is None or c.outcome_known_at is None:
                    continue
                k = c.outcome_known_at.timestamp()
                own[c.geo].add(k, c.outcome - p.point)
                if c.scale:
                    pooled.add(k, (c.outcome - p.point) / c.scale)


def compare(
    cases: Sequence[R.Case], task: str, split_date: str, *, purge: bool = False, alpha: float = G.ALPHA
) -> dict[str, Any]:
    """Development-only comparison of the density candidates with the old and the fair benchmarks (CRPS).

    ``purge``: also drop development cases whose outcome was published on/after the split date (QA-PRED X5:
    dev h1 targets overlap the holdout's h0 targets)."""
    split = datetime.fromisoformat(split_date).replace(tzinfo=UTC)
    dev = [c for c in cases if c.origin < split]
    if purge:
        dev = [c for c in dev if c.outcome_known_at is not None and c.outcome_known_at < split]
    scored = G.score_cases(dev)
    cells: dict[tuple[str, str], list[G.Scored]] = defaultdict(list)
    for s in scored:
        cells[(s.case.variable, s.case.horizon)].append(s)
    benches = (OLD_BENCH[task], OWN[task], POOLED[task])
    summaries = []
    for (var, hz), rows in sorted(cells.items()):
        for m in (OFFICIAL[task], *benches, *CANDIDATES[task]):
            sm = G.summary(rows, m)
            if sm["n"]:
                summaries.append({"variable": var, "horizon": hz, "model": m, **sm})
    tests: list[dict[str, Any]] = []
    for b in benches:
        fam = []
        for cand in CANDIDATES[task]:
            for (var, hz), rows in sorted(cells.items()):
                t = G.paired_test(rows, cand, b, "crps", G.dm_lag(task, hz))
                if t["n_pairs"]:
                    fam.append({"task": task, "variable": var, "horizon": hz, **t,
                                "registered_scope": (task, var, hz, cand) in REGISTERED})  # fmt: skip
        G.holm(fam, alpha)
        tests.extend(fam)
    return {
        "task": task,
        "split": split_date,
        "purged_overlap": purge,
        "n_cases_scored": len(scored),
        "n_origins": len({s.case.origin for s in scored}),
        "summaries": summaries,
        "tests": tests,
    }


def fair_development(cells: pl.DataFrame, *, tasks: Sequence[str] = ("weo", "us")) -> dict[str, Any]:
    """Builds development cases only (``service.build_cases(stage="development")``: no holdout origin is
    forecast or scored), adds the fair benchmarks and compares, with and without the X5 purge."""
    from reva.evaluation.pred_models import service as S

    out = []
    for t in tasks:
        cases = S.build_cases(cells, t, "development")
        add_error_fans(cases)
        out.append(compare(cases, t, S.SPLITS[t]))
        out.append(compare(cases, t, S.SPLITS[t], purge=True))
    return {
        "schema": "reva-pred-models-fair-benchmark/1",
        "stage": "development",
        "label": S.LABEL,
        "statement": "Re-run on the DEVELOPMENT sample only (the locked holdout was opened once and is spent). "
        "Fair benchmark = N(official point, RMS of the official forecaster's own past errors known at the origin). "
        "Informational: no registration follows from it; future issuance of the registered densities requires "
        "beating the fair benchmark on the next prospective evaluation (registered_models.yaml).",
        "benchmarks": {
            "old": "N(official point, sigma of REVA's persistence predictive) - QA-PRED X1 strawman",
            "own": f"N(official point, RMS of that series' own past official errors), >= {MIN_OWN} errors",
            "pooled": f"N(official point, series scale x RMS of pooled normalised past official errors), "
            f">= {MIN_POOLED} errors",
        },
        "claim_of_candidates": CLAIM,
        "code_sha256_protocol_files": S.code_sha256(),
        "tasks": out,
    }


def registered_rows(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The registered scopes' rows of a :func:`fair_development` result (for reports)."""
    rows = []
    for t in doc["tasks"]:
        for x in t["tests"]:
            if x["registered_scope"]:
                rows.append({"purged": t["purged_overlap"], **{k: x.get(k) for k in (
                    "task", "variable", "horizon", "candidate", "benchmark", "n_pairs", "n_origins", "relative",
                    "p_better", "holm_rejected", "holm_m")}})  # fmt: skip
    return rows


__all__ = [
    "CANDIDATES",
    "CLAIM",
    "MIN_OWN",
    "MIN_POOLED",
    "OWN",
    "POOLED",
    "REGISTERED",
    "add_error_fans",
    "compare",
    "error_fan",
    "fair_development",
    "registered_rows",
]
