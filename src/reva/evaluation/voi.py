"""Value of information.

Two clearly separated things live here:

(a) **Decision EVSI** for a *finite, fully specified* decision problem: a binary
    state ``theta in {0, 1}`` with prior ``P(theta=1) = prior``, two actions with
    utilities ``utility[a][theta]``, and an imperfect binary test with
    ``sensitivity = P(+|theta=1)``, ``specificity = P(-|theta=0)`` and an
    acquisition ``cost`` in the same utility unit. :func:`evsi_exact`
    enumerates the two test outcomes; :func:`evsi_nested_mc` is a nested Monte
    Carlo estimator of the same quantity (used to validate the estimator).

    Both return the value *net of cost*::

        EVSI_net = E_x[ max_a E[U(a, theta) | x] ] - max_a E[U(a, theta)] - cost

(b) An **uncertainty-reduction proxy** (:func:`uncertainty_reduction_proxy`): a
    transparent, unitless heuristic used when no decision problem is specified.
    It is *not* money and must never be presented as an EVSI.

:func:`estimate_value` dispatches: no decision spec -> proxy; a finite
:class:`DecisionProblem` -> ``decision_evsi``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import numpy as np

from reva.kernel.errors import MissingInput, ValidationProblem
from reva.kernel.uncertainty import UncertaintySpec

EVSI_METHOD_VERSION = "reva.evaluation.voi.evsi_exact_binary@1.0.0"
PROXY_METHOD_VERSION = "reva.evaluation.voi.uncertainty_reduction_proxy@1.0.0"
PROXY_UNIT = "dimensionless_proxy"

ValueKind = Literal["uncertainty_reduction_proxy", "decision_evsi"]


@dataclass(frozen=True)
class ValueEstimate:
    kind: ValueKind
    value: str | None  # decimal string
    unit: str | None
    uncertainty: dict[str, Any]
    assumptions: tuple[str, ...]
    method_version: str


@dataclass(frozen=True)
class DecisionProblem:
    """Binary-state, two-action decision with an imperfect binary test.

    ``utility[a][theta]`` is the utility of action ``a`` (0 or 1) in state
    ``theta`` (0 or 1). ``utility_unit`` names the unit of utilities and cost
    (it may be a currency only if the caller's utilities really are money).
    """

    prior: float
    utility: tuple[tuple[float, float], tuple[float, float]]
    sensitivity: float
    specificity: float
    cost: float = 0.0
    utility_unit: str = "utility"

    def __post_init__(self) -> None:
        for name in ("prior", "sensitivity", "specificity"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0, 1]")
        if len(self.utility) != 2 or any(len(row) != 2 for row in self.utility):
            raise ValueError("utility must be a 2x2 matrix utility[action][state]")
        if not all(math.isfinite(u) for row in self.utility for u in row):
            raise ValueError("utilities must be finite")
        if not math.isfinite(self.cost) or self.cost < 0:
            raise ValueError("cost must be finite and >= 0")


def _u(problem: DecisionProblem) -> np.ndarray:
    return np.asarray(problem.utility, dtype=float)  # shape (action, state)


def prior_decision_value(problem: DecisionProblem) -> float:
    """max_a E[U(a, theta)] under the prior."""
    p = np.array([1.0 - problem.prior, problem.prior])
    return float((_u(problem) @ p).max())


def _outcome_probs(problem: DecisionProblem) -> np.ndarray:
    """P(x | theta) with rows x in {-, +}, columns theta in {0, 1}."""
    se, sp = problem.sensitivity, problem.specificity
    return np.array([[sp, 1.0 - se], [1.0 - sp, se]])


def evsi_exact(problem: DecisionProblem) -> float:
    """Exact net EVSI by enumerating the two test outcomes."""
    u = _u(problem)
    prior = np.array([1.0 - problem.prior, problem.prior])
    like = _outcome_probs(problem)
    preposterior = 0.0
    for x in (0, 1):
        joint = like[x] * prior  # P(x, theta)
        px = float(joint.sum())
        if px == 0.0:
            continue
        posterior = joint / px
        preposterior += px * float((u @ posterior).max())
    return preposterior - prior_decision_value(problem) - problem.cost


def evpi(problem: DecisionProblem) -> float:
    """Expected value of perfect information (gross, no cost)."""
    prior = np.array([1.0 - problem.prior, problem.prior])
    return float((_u(problem).max(axis=0) * prior).sum()) - prior_decision_value(problem)


@dataclass(frozen=True)
class MonteCarloEstimate:
    value: float
    std_error: float  # outer-loop sampling error only; inner-loop bias is O(1/n_inner)
    n_outer: int
    n_inner: int
    seed: int


def evsi_nested_mc(
    problem: DecisionProblem, n_outer: int, n_inner: int, seed: int, chunk: int = 2048
) -> MonteCarloEstimate:
    """Nested Monte Carlo estimate of net EVSI.

    Outer loop: draw ``theta_i ~ prior`` and ``x_i ~ P(x | theta_i)``.
    Inner loop: draw ``n_inner`` fresh ``theta_j ~ prior`` and estimate the
    posterior expected utility by self-normalised likelihood weighting,
    ``E[U(a)|x_i] ~= sum_j L(x_i|theta_j) U(a,theta_j) / sum_j L(x_i|theta_j)``,
    then take ``max_a``. The prior decision value is computed exactly (it has a
    closed form), so the only Monte Carlo error is in the preposterior term.
    The max over noisy inner estimates biases the estimate upward by O(1/n_inner).
    """
    if n_outer < 2 or n_inner < 1:
        raise ValueError("n_outer must be >= 2 and n_inner >= 1")
    rng = np.random.default_rng(seed)
    u = _u(problem)
    like = _outcome_probs(problem)
    vals = np.empty(n_outer, dtype=float)
    for start in range(0, n_outer, chunk):
        m = min(chunk, n_outer - start)
        theta = (rng.random(m) < problem.prior).astype(int)
        x = (rng.random(m) < like[1, theta]).astype(int)
        theta_in = (rng.random((m, n_inner)) < problem.prior).astype(int)
        w = like[x[:, None], theta_in]  # L(x_i | theta_j)
        wsum = w.sum(axis=1)
        eu = np.stack([(w * u[a, theta_in]).sum(axis=1) for a in (0, 1)], axis=1)
        # If every inner weight is zero the inner sample carries no information
        # about x_i; fall back to the prior expected utility for that draw.
        prior_eu = u @ np.array([1.0 - problem.prior, problem.prior])
        safe = wsum > 0
        eu[safe] = eu[safe] / wsum[safe, None]
        eu[~safe] = prior_eu
        vals[start : start + m] = eu.max(axis=1)
    est = float(vals.mean()) - prior_decision_value(problem) - problem.cost
    se = float(vals.std(ddof=1) / math.sqrt(n_outer))
    return MonteCarloEstimate(est, se, n_outer, n_inner, seed)


def _dec(x: float) -> str:
    if not math.isfinite(x):
        raise ValueError("value must be finite")
    return str(Decimal(repr(float(x))))


PROXY_ASSUMPTIONS = (
    "transparent heuristic, not a decision-theoretic value of information",
    "unitless; NOT money and not comparable to a monetary EVSI",
    "value = relevance_weight * (1 - expected_posterior_sd / prior_sd)",
    "expected_posterior_sd is an assumed outcome of acquisition, not an observed one",
    "relevance_weight in [0, 1] is a stated judgement, not estimated",
)


def uncertainty_reduction_proxy(
    prior_sd: float, expected_posterior_sd: float, relevance_weight: float = 1.0
) -> ValueEstimate:
    """Fractional reduction in predictive sd, weighted by stated relevance.

    ``value = relevance_weight * (1 - expected_posterior_sd / prior_sd)`` in [0, 1].
    """
    for name, v in (("prior_sd", prior_sd), ("expected_posterior_sd", expected_posterior_sd)):
        if not math.isfinite(v) or v < 0:
            raise ValidationProblem(f"{name} must be finite and >= 0", field=name)
    if prior_sd == 0:
        raise ValidationProblem("prior_sd must be > 0 (nothing to reduce)", field="prior_sd")
    if expected_posterior_sd > prior_sd:
        raise ValidationProblem("expected_posterior_sd cannot exceed prior_sd")
    if not (0.0 <= relevance_weight <= 1.0):
        raise ValidationProblem("relevance_weight must be in [0, 1]", field="relevance_weight")
    value = relevance_weight * (1.0 - expected_posterior_sd / prior_sd)
    return ValueEstimate(
        kind="uncertainty_reduction_proxy",
        value=_dec(value),
        unit=PROXY_UNIT,
        uncertainty=UncertaintySpec.unknown(
            "heuristic proxy; inputs are assumptions, no uncertainty quantified"
        ).model_dump(mode="json"),
        assumptions=PROXY_ASSUMPTIONS,
        method_version=PROXY_METHOD_VERSION,
    )


@dataclass(frozen=True)
class KnowledgeGap:
    gap_id: str
    prior_sd: float | None = None
    expected_posterior_sd: float | None = None
    relevance_weight: float = 1.0
    notes: tuple[str, ...] = field(default=())


def _gap_field(gap: KnowledgeGap | Mapping[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(gap, Mapping):
        return gap.get(name, default)
    return getattr(gap, name, default)


def estimate_value(
    gap: KnowledgeGap | Mapping[str, Any], decision_spec: DecisionProblem | None = None
) -> ValueEstimate:
    """Value of closing ``gap``.

    Without a decision specification only the unitless uncertainty-reduction
    proxy is returned (never a monetary EVSI). With a finite
    :class:`DecisionProblem` the exact net EVSI is returned in the problem's
    utility unit.
    """
    if decision_spec is None:
        prior_sd = _gap_field(gap, "prior_sd")
        post_sd = _gap_field(gap, "expected_posterior_sd")
        missing = [
            n for n, v in (("prior_sd", prior_sd), ("expected_posterior_sd", post_sd)) if v is None
        ]
        if missing:
            raise MissingInput(
                "gap lacks inputs for the uncertainty-reduction proxy", missing=missing
            )
        return uncertainty_reduction_proxy(
            float(prior_sd), float(post_sd), float(_gap_field(gap, "relevance_weight", 1.0))
        )
    if not isinstance(decision_spec, DecisionProblem):
        raise ValidationProblem("decision_spec must be a finite DecisionProblem")
    value = evsi_exact(decision_spec)
    return ValueEstimate(
        kind="decision_evsi",
        value=_dec(value),
        unit=decision_spec.utility_unit,
        uncertainty=UncertaintySpec.by_definition(
            "exact enumeration conditional on the stated prior, utilities and test accuracy; "
            "uncertainty in those inputs is not propagated"
        ).model_dump(mode="json"),
        assumptions=(
            "binary state with stated prior",
            "two actions with stated utility matrix",
            "binary test with stated sensitivity and specificity, conditionally independent",
            "value is net of acquisition cost, in the utility unit",
            "risk-neutral expected-utility decision maker",
        ),
        method_version=EVSI_METHOD_VERSION,
    )
