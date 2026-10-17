"""Online forecast combination (V7 BEAT-ENSEMBLE): weights from past *resolved* losses only (pure, domain-agnostic).

Weights
    Exponentially weighted average forecaster (Hedge; Cesa-Bianchi & Lugosi 2006, Thm 2.2, learning rate
    ``eta = sqrt(8 ln K / n)`` for losses in [0, 1]) on **relative losses**: each resolved question's loss of a
    component is divided by the median loss of the components scored on that question (scale-free across countries,
    units and hyperinflation episodes), clipped at ``cap`` and rescaled to [0, 1]. Components abstain on different
    questions, so each component's mean relative loss is **shrunk towards 1** (the median performer) with prior
    weight ``prior_n`` before the exponential weighting; a component without history has shrunk loss 1. Below
    ``min_resolved`` resolved questions the weights are **equal** and labelled so: nothing else (no pseudo-real-time
    replay, no back-test) may set them.

Combination
    * location (point): weighted mean of the components' points;
    * shape: quantile averaging of the components' *deviations from their own median*, re-centred on the combined
      location, ``q(tau) = loc + sum_k v_k (Q_k(tau) - Q_k(0.5))`` (Vincentisation; monotone in tau because every
      component quantile function is), so the combined median is the combined point;
    * probabilities: linear pool ``p = sum_k w_k p_k``.

Information sources (method 2, ``reva.ensemble@2``)
    Forecasters built around the same official point (e.g. the IMF WEO projection and the densities wrapped around
    it) share **one source weight**, split equally among the members present: :func:`grouped_weights` runs the same
    Hedge rule on per-source losses (the mean of the members' losses on each question) and :func:`restrict_grouped`
    applies it to one question. Equal-weights mode then means equal weights per source.

Every function is deterministic: same inputs (sorted), same floats.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

from reva.models.forecasting.registry_baselines import Predictive

#: levels at which the combined quantile function is evaluated (its empirical sample: mid-point rule)
SAMPLE_LEVELS: tuple[float, ...] = tuple((i + 0.5) / 50 for i in range(50))
METHOD = "hedge_relative_loss_shrunk/1"
METHOD_BY_SOURCE = "hedge_relative_loss_shrunk_by_source/2"
EQUAL = "equal_weights_insufficient_history"
ONLINE = "online_weights"
SINGLE = "single_component"


@dataclass(frozen=True)
class WeightRule:
    """Pre-declared weighting rule (recorded in every sealed ensemble manifest)."""

    min_resolved: int = 20  # resolved questions of the task before the weights may move away from equal
    prior_n: float = 10.0  # shrinkage of each component's mean relative loss towards 1
    cap: float = 5.0  # relative losses clipped at cap, then rescaled to [0, 1]
    by_source: bool = False  # method 2: one weight per information source, split equally among its members

    def doc(self) -> dict[str, Any]:
        d = asdict(self)
        by_source = d.pop("by_source")
        return {"method": METHOD_BY_SOURCE if by_source else METHOD, **d}


@dataclass(frozen=True)
class LossRow:
    question_id: str
    model: str
    loss: float


def relative_losses(rows: Iterable[LossRow], cap: float) -> tuple[dict[str, list[float]], int, list[str]]:
    """Relative losses per model, the number of informative questions and the questions dropped (with < 2 scored
    components, or every loss 0). A (question, model) pair seen twice keeps its first row in sorted order."""
    by_q: dict[str, dict[str, float]] = defaultdict(dict)
    for r in sorted(rows, key=lambda r: (r.question_id, r.model, r.loss)):
        if math.isfinite(r.loss) and r.loss >= 0:
            by_q[r.question_id].setdefault(r.model, r.loss)
    out: dict[str, list[float]] = defaultdict(list)
    n = 0
    dropped: list[str] = []
    for qid in sorted(by_q):
        losses = by_q[qid]
        if len(losses) < 2:
            dropped.append(qid)
            continue
        scale = median(losses.values())
        if scale <= 0:
            scale = math.fsum(losses.values()) / len(losses)
        if scale <= 0:
            dropped.append(qid)
            continue
        n += 1
        for m in sorted(losses):
            out[m].append(min(losses[m] / scale, cap))
    return dict(out), n, dropped


def online_weights(components: Sequence[str], rows: Iterable[LossRow], rule: WeightRule) -> dict[str, Any]:
    """Weights of ``components`` from the resolved loss rows (only rows of these components are used)."""
    comps = sorted(set(components))
    if not comps:
        return {"status": "no_component", "weights": {}, "n_resolved_questions": 0, "per_component": {}}
    rel, n_q, dropped = relative_losses((r for r in rows if r.model in comps), rule.cap)
    per: dict[str, dict[str, Any]] = {}
    for c in comps:
        rs = rel.get(c, [])
        mean_rel = math.fsum(rs) / len(rs) if rs else None
        shrunk = (math.fsum(rs) + rule.prior_n) / (len(rs) + rule.prior_n)
        per[c] = {"n": len(rs), "mean_relative_loss": mean_rel, "shrunk_relative_loss": shrunk}
    base = {"n_resolved_questions": n_q, "n_dropped_questions": len(dropped), "per_component": per}
    if len(comps) == 1:
        return {"status": SINGLE, "weights": {comps[0]: 1.0}, "eta": None, **base}
    if n_q < rule.min_resolved:
        w = 1.0 / len(comps)
        return {"status": EQUAL, "weights": {c: w for c in comps}, "eta": None, **base}
    eta = math.sqrt(8.0 * math.log(len(comps)) / n_q)
    expo = {c: -eta * n_q * per[c]["shrunk_relative_loss"] / rule.cap for c in comps}
    top = max(expo.values())
    raw = {c: math.exp(expo[c] - top) for c in comps}
    tot = math.fsum(raw.values())
    return {"status": ONLINE, "weights": {c: raw[c] / tot for c in comps}, "eta": eta, **base}


def source_rows(rows: Iterable[LossRow], sources: Mapping[str, str]) -> list[LossRow]:
    """Per question and information source: the mean loss of the source's members scored on that question."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for r in sorted(rows, key=lambda r: (r.question_id, r.model, r.loss)):
        if (r.question_id, r.model) in seen or not (math.isfinite(r.loss) and r.loss >= 0):
            continue
        seen.add((r.question_id, r.model))
        acc[(r.question_id, sources.get(r.model, r.model))].append(r.loss)
    return [LossRow(q, s, math.fsum(v) / len(v)) for (q, s), v in sorted(acc.items())]


def grouped_weights(
    components: Sequence[str], sources: Mapping[str, str], rows: Iterable[LossRow], rule: WeightRule
) -> dict[str, Any]:
    """Method 2: Hedge over information sources; each source's weight split equally among its members."""
    comps = sorted(set(components))
    groups = {c: sources.get(c, c) for c in comps}
    members: dict[str, list[str]] = defaultdict(list)
    for c in comps:
        members[groups[c]].append(c)
    w = online_weights(sorted(members), source_rows((r for r in rows if r.model in groups), groups), rule)
    sw = w["weights"]
    return {
        **w,
        "sources": groups,
        "source_weights": sw,
        "weights": {c: sw[groups[c]] / len(members[groups[c]]) for c in comps} if sw else {},
    }


def restrict_grouped(
    source_weights: Mapping[str, float], sources: Mapping[str, str], present: Iterable[str]
) -> dict[str, float]:
    """Method 2 on one question: the present sources' weights renormalised, each split equally among its present
    members (equal per source if none has weight)."""
    ps = sorted(set(present))
    if not ps:
        return {}
    by: dict[str, list[str]] = defaultdict(list)
    for p in ps:
        by[sources.get(p, p)].append(p)
    tot = math.fsum(source_weights.get(s, 0.0) for s in by)
    sw = {s: (source_weights.get(s, 0.0) / tot if tot > 0 else 1.0 / len(by)) for s in by}
    return {p: sw[s] / len(m) for s, m in sorted(by.items()) for p in m}


def restrict(weights: Mapping[str, float], present: Iterable[str]) -> dict[str, float]:
    """Weights of the components present on one question, renormalised (equal if none has weight)."""
    ps = sorted(set(present))
    tot = math.fsum(weights.get(p, 0.0) for p in ps)
    if not ps:
        return {}
    if tot <= 0:
        return {p: 1.0 / len(ps) for p in ps}
    return {p: weights.get(p, 0.0) / tot for p in ps}


def combine_location(points: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return math.fsum(weights[k] * points[k] for k in sorted(points))


def _median(p: Predictive) -> float:
    m = p.quantile(0.5)
    assert m is not None
    return m


def combine_quantiles(
    loc: float, preds: Mapping[str, Predictive], weights: Mapping[str, float], levels: Sequence[float]
) -> list[float]:
    """Combined quantiles at ``levels``: the location plus the weighted average of the components' deviations from
    their own median (distributional components only)."""
    meds = {k: _median(preds[k]) for k in sorted(preds)}
    out = []
    for tau in levels:
        dev = math.fsum(weights[k] * (preds[k].quantile(tau) - meds[k]) for k in sorted(preds))  # type: ignore[operator]
        out.append(loc + dev)
    for i in range(1, len(out)):  # guard rounding: a quantile function never decreases
        out[i] = max(out[i], out[i - 1])
    return out


def combine_probability(probs: Mapping[str, float], weights: Mapping[str, float]) -> float:
    p = math.fsum(weights[k] * probs[k] for k in sorted(probs))
    return min(1.0, max(0.0, p))


__all__ = [
    "EQUAL",
    "METHOD",
    "METHOD_BY_SOURCE",
    "ONLINE",
    "SAMPLE_LEVELS",
    "SINGLE",
    "LossRow",
    "WeightRule",
    "combine_location",
    "combine_probability",
    "combine_quantiles",
    "grouped_weights",
    "online_weights",
    "relative_losses",
    "restrict",
    "restrict_grouped",
    "source_rows",
]
