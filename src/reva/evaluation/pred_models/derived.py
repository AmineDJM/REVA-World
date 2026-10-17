"""Second pass over the cases: combinations, conformal densities, bias corrections (V6, agent PRED-MODELS).

For a case at origin ``t``, a derived model may use the out-of-sample errors ``outcome - point`` of earlier cases
of the same (task, variable, horizon) **whose outcome was published at or before t** (``outcome_known_at <= t``);
nothing else about outcomes. Errors are pooled across the series of the task and normalised by each series' own
scale (``Case.scale``, the RMS of its 1-step changes as published at that case's origin), then rescaled by the
target series' scale ("locally weighted" split conformal). Expanding window, no tuning on the evaluation sample.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from reva.evaluation.pred_models.realtime import BVAR, DFM, DIRECT_AR, PERSIST, SPF, WEO, Case
from reva.models.forecasting import combination as CB
from reva.models.forecasting.registry_baselines import Abstain, Predictive

MAX_ERR_SAMPLE = 200


@dataclass(frozen=True)
class DerivedSpec:
    key: str
    kind: str  # "conformal" | "bias_conformal" | "combo_conformal" | "pool"
    inputs: tuple[str, ...]
    description: str


def conf_key(base: str) -> str:
    return f"{base}+conformal"


TASK_DERIVED: dict[str, tuple[DerivedSpec, ...]] = {
    "weo": (
        DerivedSpec(conf_key(WEO), "conformal", (WEO,), "WEO point + scaled past WEO errors (density for the WEO)"),
        DerivedSpec("forecast.weo_bias_corrected", "bias_conformal", (WEO,),
                    "WEO + median past WEO error (intercept correction); centred conformal density"),
        DerivedSpec("forecast.combo_weo_ar", "combo_conformal", (WEO, DIRECT_AR),
                    "equal-weight WEO and direct AR; conformal density from past combination errors"),
        DerivedSpec("forecast.combo_weo_ar_persist", "combo_conformal", (WEO, DIRECT_AR, PERSIST),
                    "equal-weight WEO, direct AR and persistence; conformal density"),
        DerivedSpec(conf_key(DIRECT_AR), "conformal", (DIRECT_AR,), "direct AR point + scaled past AR errors"),
    ),
    "us": (
        DerivedSpec(conf_key(SPF), "conformal", (SPF,), "SPF median + scaled past SPF errors (density for the SPF)"),
        DerivedSpec("forecast.combo_spf_bvar", "combo_conformal", (SPF, BVAR),
                    "equal-weight SPF median and BVAR median; conformal density"),
        DerivedSpec("forecast.combo_spf_ar", "combo_conformal", (SPF, DIRECT_AR),
                    "equal-weight SPF median and direct AR; conformal density"),
        DerivedSpec("forecast.pool_bvar_ar", "pool", (BVAR, DIRECT_AR), "equal-weight linear pool of BVAR and direct AR"),
        DerivedSpec(conf_key(BVAR), "conformal", (BVAR,), "BVAR median + scaled past BVAR errors"),
    ),
    "g7": (
        DerivedSpec("forecast.pool_bvar_ar", "pool", (BVAR, DIRECT_AR), "equal-weight linear pool of BVAR and direct AR"),
        DerivedSpec(conf_key(BVAR), "conformal", (BVAR,), "BVAR median + scaled past BVAR errors"),
        DerivedSpec(conf_key(DIRECT_AR), "conformal", (DIRECT_AR,), "direct AR point + scaled past AR errors"),
        DerivedSpec("forecast.combo_dfm_bvar", "combo_conformal", (DFM, BVAR),
                    "nowcast quarter only: equal-weight DFM nowcast and BVAR; conformal density"),
        DerivedSpec(conf_key(DFM), "conformal", (DFM,), "DFM nowcast + scaled past DFM errors"),
    ),
}  # fmt: skip


def _point(p: object) -> float | None:
    return p.point if isinstance(p, Predictive) else None


class _ErrorBook:
    """Normalised past errors of one input combination, ordered by the instant their outcome became known."""

    def __init__(self) -> None:
        self.known: list[float] = []
        self.errs: list[float] = []

    def add(self, known_at: float, err: float) -> None:
        i = bisect.bisect_right(self.known, known_at)
        self.known.insert(i, known_at)
        self.errs.insert(i, err)

    def until(self, t: float) -> list[float]:
        return self.errs[: bisect.bisect_right(self.known, t)]


def _sample(errs: Sequence[float]) -> list[float]:
    if len(errs) <= MAX_ERR_SAMPLE:
        return list(errs)
    probs = (np.arange(MAX_ERR_SAMPLE) + 0.5) / MAX_ERR_SAMPLE
    return [float(x) for x in np.quantile(np.asarray(errs), probs)]


def add_derived(cases: list[Case], specs: Sequence[DerivedSpec] | None = None) -> None:
    """Adds the derived predictives to ``cases`` in place (all tasks present in ``cases``)."""
    groups: dict[tuple[str, str, str], list[Case]] = defaultdict(list)
    for c in cases:
        groups[(c.task, c.variable, c.horizon)].append(c)
    for (task, _v, _h), grp in groups.items():
        grp.sort(key=lambda c: c.origin)
        sp = specs if specs is not None else TASK_DERIVED.get(task, ())
        books: dict[str, _ErrorBook] = defaultdict(_ErrorBook)
        # cases sharing an origin are processed together: their outcomes are added after all of them are made
        by_origin: dict[float, list[Case]] = defaultdict(list)
        for c in grp:
            by_origin[c.origin.timestamp()].append(c)
        for ts in sorted(by_origin):
            batch = by_origin[ts]
            for c in batch:
                for s in sp:
                    pts = [_point(c.preds.get(k)) for k in s.inputs]
                    if any(x is None for x in pts):
                        miss = [k for k, x in zip(s.inputs, pts, strict=True) if x is None]
                        c.preds[s.key] = Abstain("input forecast missing: " + ", ".join(miss))
                        continue
                    point = math.fsum(x for x in pts if x is not None) / len(pts)
                    if s.kind == "pool":
                        comps = [c.preds[k] for k in s.inputs]
                        c.preds[s.key] = CB.equal_weight([p for p in comps if isinstance(p, Predictive)])
                        continue
                    if c.scale is None:
                        c.preds[s.key] = Abstain("no series scale at the origin")
                        continue
                    errs = _sample(books[s.key].until(ts))
                    if s.kind == "bias_conformal":
                        adj = CB.bias_corrected(0.0, errs, robust=True)
                        if isinstance(adj, Abstain):
                            c.preds[s.key] = adj
                            continue
                        point = point + c.scale * adj
                        c.preds[s.key] = CB.conformal(point, errs, scale=c.scale, center=True)
                    else:
                        c.preds[s.key] = CB.conformal(point, errs, scale=c.scale)
            # register this origin's errors with their own publication instants (used by later origins only)
            for c in batch:
                if c.outcome is None or c.outcome_known_at is None or c.scale is None:
                    continue
                for s in sp:
                    if s.kind == "pool":
                        continue
                    pts = [_point(c.preds.get(k)) for k in s.inputs]
                    if any(x is None for x in pts):
                        continue
                    base = math.fsum(x for x in pts if x is not None) / len(pts)
                    books[s.key].add(c.outcome_known_at.timestamp(), (c.outcome - base) / c.scale)


__all__ = ["TASK_DERIVED", "DerivedSpec", "add_derived", "conf_key"]
