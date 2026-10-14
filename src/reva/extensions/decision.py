"""P12 decision search and multi-actor reaction interfaces (architecture §27).

A :class:`DecisionSpec` declares feasible actions, controllability, timing,
actor, objectives, distributional constraints, legal/ethical hard constraints,
risk measure, horizon, discounting and model set. Prediction does not choose
values: weights and constraints are explicit inputs, never inferred.

Result semantics are fixed now: a :class:`ParetoFrontier` reports trade-offs,
worst-case violations, weight sensitivity, affected groups and model
ambiguity, and every point is model-conditioned. A
:class:`MultiActorSpec` declares information sets, beliefs, objectives,
response delays, commitment constraints and an equilibrium concept; multiple
equilibria are retained, never averaged.

:func:`optimise` and :func:`solve_reactions` raise ``CapabilityGap`` in this
build (``decision.search`` / ``decision.multi_actor`` are reserved).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from reva.capabilities import require
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash

SPEC_VERSION = "reva.decision_spec/1"


@dataclass(frozen=True)
class Action:
    action_id: str
    description: str
    domain: Literal["continuous", "integer", "binary", "categorical"]
    lower: float | None = None
    upper: float | None = None
    levels: tuple[str, ...] = ()
    unit: str | None = None
    controllable_by: str = ""
    """Actor that controls this lever; must be the spec's actor or declared uncontrollable."""
    earliest_period: int = 0
    implementation_lag_periods: int = 0

    def __post_init__(self) -> None:
        if self.domain in ("continuous", "integer"):
            if (
                self.lower is None
                or self.upper is None
                or not (math.isfinite(self.lower) and math.isfinite(self.upper))
            ):
                raise ValidationProblem(f"action {self.action_id!r} needs finite bounds")
            if self.lower > self.upper:
                raise ValidationProblem(f"action {self.action_id!r} has lower > upper")
            if not self.unit:
                raise ValidationProblem(f"action {self.action_id!r} needs an explicit unit")
        if self.domain == "categorical" and len(self.levels) < 2:
            raise ValidationProblem(f"categorical action {self.action_id!r} needs at least two levels")
        if not self.controllable_by:
            raise ValidationProblem(f"action {self.action_id!r} must name the controlling actor")
        if self.earliest_period < 0 or self.implementation_lag_periods < 0:
            raise ValidationProblem(f"action {self.action_id!r} timing must be nonnegative")


@dataclass(frozen=True)
class Objective:
    objective_id: str
    concept: str
    """Registered concept/estimand id the objective is measured on."""
    direction: Literal["minimise", "maximise"]
    unit: str
    affected_groups: tuple[str, ...] = ()
    weight: float | None = None
    """Optional explicit normative weight; ``None`` means report the frontier, do not scalarise."""

    def __post_init__(self) -> None:
        if self.weight is not None and not (math.isfinite(self.weight) and self.weight >= 0):
            raise ValidationProblem(f"objective {self.objective_id!r} weight must be finite and >= 0")


@dataclass(frozen=True)
class DistributionalConstraint:
    constraint_id: str
    group: str
    metric: str
    operator: Literal["<=", ">="]
    bound: float
    unit: str


@dataclass(frozen=True)
class HardConstraint:
    constraint_id: str
    kind: Literal["legal", "ethical", "physical", "safety", "budget"]
    statement: str
    authority_ref: str
    """Citation of the law, policy or physical limit; hard constraints are never traded off."""


@dataclass(frozen=True)
class RiskMeasure:
    kind: Literal["expectation", "cvar", "worst_case", "minimax_regret", "distributionally_robust"]
    alpha: float | None = None
    ambiguity_set: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "cvar" and not (self.alpha is not None and 0 < self.alpha < 1):
            raise ValidationProblem("CVaR needs alpha in (0, 1)")
        if self.kind == "distributionally_robust" and not self.ambiguity_set:
            raise ValidationProblem("distributionally robust optimisation needs a named ambiguity set")


@dataclass(frozen=True)
class Discounting:
    kind: Literal["none", "constant_exponential", "declining_schedule"]
    annual_rate: float | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.kind == "constant_exponential" and not (
            self.annual_rate is not None and math.isfinite(self.annual_rate) and -0.5 < self.annual_rate < 1
        ):
            raise ValidationProblem("exponential discounting needs a finite annual rate")
        if not self.rationale.strip():
            raise ValidationProblem("discounting is a normative choice; a rationale is required")


@dataclass(frozen=True)
class DecisionSpec:
    actor: str
    actions: tuple[Action, ...]
    objectives: tuple[Objective, ...]
    distributional_constraints: tuple[DistributionalConstraint, ...]
    hard_constraints: tuple[HardConstraint, ...]
    risk_measure: RiskMeasure
    horizon_periods: int
    period_unit: Literal["year", "quarter", "month"]
    discounting: Discounting
    model_set: tuple[str, ...]
    """Approved model_version ids; a single model is accepted but reported as model ambiguity unassessed."""
    scenario_set: tuple[str, ...]
    no_hard_constraints_rationale: str | None = None
    spec_version: str = SPEC_VERSION

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValidationProblem("decision spec needs at least one feasible action")
        if not self.objectives:
            raise ValidationProblem("decision spec needs at least one objective")
        ids = [a.action_id for a in self.actions]
        if len(set(ids)) != len(ids):
            raise ValidationProblem("action ids must be unique")
        uncontrolled = [a.action_id for a in self.actions if a.controllable_by != self.actor]
        if uncontrolled:
            raise ValidationProblem(f"actions {uncontrolled} are not controllable by actor {self.actor!r}")
        if not self.hard_constraints and not (self.no_hard_constraints_rationale or "").strip():
            raise ValidationProblem("declare legal/ethical hard constraints or justify their absence explicitly")
        if self.horizon_periods < 1:
            raise ValidationProblem("horizon must be at least one period")
        if not self.model_set:
            raise ValidationProblem("decision spec needs a model set")
        if not self.scenario_set:
            raise ValidationProblem("decision spec needs at least one scenario")

    def digest(self) -> str:
        return logical_hash(asdict(self))

    @property
    def single_model_mean_only(self) -> bool:
        """Specifiable, but §27 forbids calling a one-model mean maximiser 'the best policy'."""
        return self.risk_measure.kind == "expectation" and len(self.model_set) == 1


@dataclass(frozen=True)
class ParetoPoint:
    actions: Mapping[str, float | str]
    objective_values: Mapping[str, Mapping[str, float]]
    """objective_id -> {scenario_or_model_id -> value}: per-scenario values, not one mean."""
    worst_case_constraint_violation: Mapping[str, float]
    affected_group_outcomes: Mapping[str, Mapping[str, float]]
    model_conditioned: Literal[True] = True


@dataclass(frozen=True)
class ParetoFrontier:
    spec_digest: str
    points: tuple[ParetoPoint, ...]
    robustness: Mapping[str, Any]
    """How the frontier changes across scenarios and models (e.g. regret per point)."""
    weight_sensitivity: Mapping[str, Any]
    model_ambiguity: Mapping[str, Any]
    method: str
    method_version: str
    caveats: tuple[str, ...] = field(
        default=(
            "all suggested actions are conditional on the declared model set and scenarios",
            "no point is 'the best policy'; trade-offs and normative weights remain choices",
        )
    )

    def __post_init__(self) -> None:
        if not self.caveats:
            raise ValidationProblem("a frontier must carry its model-conditioning caveats")


@dataclass(frozen=True)
class ActorSpec:
    actor_id: str
    objectives: tuple[Objective, ...]
    actions: tuple[Action, ...]
    information_set: tuple[str, ...]
    """Observable variables available to the actor when choosing."""
    beliefs: str
    """Reference to the actor's belief/expectation model."""
    response_delay_periods: int
    commitment_constraints: tuple[str, ...] = ()
    adaptation: Literal["none", "adaptive", "learning"] = "none"

    def __post_init__(self) -> None:
        if not self.information_set:
            raise ValidationProblem(f"actor {self.actor_id!r} needs an explicit information set")
        if self.response_delay_periods < 0:
            raise ValidationProblem(f"actor {self.actor_id!r} response delay must be nonnegative")


@dataclass(frozen=True)
class MultiActorSpec:
    actors: tuple[ActorSpec, ...]
    equilibrium_concept: Literal[
        "nash",
        "bayesian_nash",
        "stackelberg",
        "correlated",
        "level_k",
        "non_equilibrium_trajectory",
    ]
    reaction_models: tuple[str, ...]
    """Several plausible reaction models must be supported; one is accepted but flagged."""
    retain_multiple_equilibria: Literal[True] = True
    leader: str | None = None

    def __post_init__(self) -> None:
        if len(self.actors) < 2:
            raise ValidationProblem("a multi-actor spec needs at least two actors")
        ids = [a.actor_id for a in self.actors]
        if len(set(ids)) != len(ids):
            raise ValidationProblem("actor ids must be unique")
        if self.equilibrium_concept == "stackelberg" and self.leader not in ids:
            raise ValidationProblem("Stackelberg needs a declared leader among the actors")
        if self.retain_multiple_equilibria is not True:
            raise ValidationProblem("multiple equilibria are retained, never averaged")
        if not self.reaction_models:
            raise ValidationProblem("at least one reaction model must be declared")


def optimise(spec: DecisionSpec) -> ParetoFrontier:
    """Reserved: raises ``CapabilityGap`` (decision.search) with the spec digest."""
    require(
        "decision.search",
        spec_digest=spec.digest(),
        actor=spec.actor,
        objectives=len(spec.objectives),
        models=len(spec.model_set),
        risk_measure=spec.risk_measure.kind,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def solve_reactions(spec: MultiActorSpec) -> Any:
    """Reserved: raises ``CapabilityGap`` (decision.multi_actor)."""
    require(
        "decision.multi_actor", actors=[a.actor_id for a in spec.actors], equilibrium_concept=spec.equilibrium_concept
    )
    raise AssertionError("unreachable")  # pragma: no cover
