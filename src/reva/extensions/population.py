"""P10 household-aware synthetic population and privacy-accounting interfaces.

V1 synthesises only from public age/recorded-sex aggregate cells. Household
synthesis requires licensed/approved joint data and uncertainty over
unidentified joints; privacy accounting requires an explicit ε, δ and
neighbouring relation. Both capabilities are reserved
(``population.households``, ``population.privacy_accounting``): the entry
points raise ``CapabilityGap``. The spec types validate what a future
implementation must be given.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from reva.capabilities import require
from reva.kernel.errors import ValidationProblem

NeighbouringRelation = Literal[
    "add_remove_individual",
    "substitute_individual",
    "add_remove_household",
    "substitute_household",
]
Composition = Literal["basic_sequential", "advanced", "renyi_dp", "zero_concentrated_dp"]


@dataclass(frozen=True)
class DPBudget:
    """An explicit (ε, δ)-DP budget. Nothing is defaulted: the caller must state every term."""

    epsilon: float
    delta: float
    neighbouring: NeighbouringRelation
    unit_of_privacy: str
    """E.g. 'person', 'household' — must be consistent with ``neighbouring``."""
    composition: Composition
    justification: str

    def __post_init__(self) -> None:
        if not (isinstance(self.epsilon, (int, float)) and math.isfinite(self.epsilon) and self.epsilon > 0):
            raise ValidationProblem("DP budget: epsilon must be explicit, positive and finite")
        if not (isinstance(self.delta, (int, float)) and math.isfinite(self.delta) and 0 <= self.delta < 1):
            raise ValidationProblem("DP budget: delta must be explicit and in [0, 1)")
        household = self.neighbouring.endswith("household")
        if household != (self.unit_of_privacy == "household"):
            raise ValidationProblem("DP budget: unit of privacy must match the neighbouring relation")
        if not self.justification.strip():
            raise ValidationProblem("DP budget: a justification is required")


@dataclass(frozen=True)
class PrivacySpend:
    query_id: str
    mechanism: str
    epsilon: float
    delta: float


@runtime_checkable
class PrivacyAccountant(Protocol):
    """Tracks cumulative privacy loss under a declared composition theorem."""

    budget: DPBudget

    def charge(self, spend: PrivacySpend) -> None:
        """Record a spend; must raise ``PolicyDenied`` if it would exceed the budget."""

    def remaining(self) -> tuple[float, float]: ...
    def ledger(self) -> Sequence[PrivacySpend]: ...


@dataclass(frozen=True)
class JointDataSource:
    source_id: str
    licence: str
    approval: Literal["approved", "pending", "restricted"]
    variables: tuple[str, ...]
    unit: Literal["person", "household"]


@dataclass(frozen=True)
class HouseholdSynthesisSpec:
    universe: str
    reference_period: str
    marginal_sources: tuple[str, ...]
    joint_sources: tuple[JointDataSource, ...]
    structural_constraints: tuple[str, ...]
    """E.g. 'one reference person per household', 'child age < parent age' — must hold exactly."""
    unidentified_joint_method: Literal["bounds", "ensemble_over_copulas", "maximum_entropy_with_ensemble"]
    sensitive_attributes_allowed: tuple[str, ...] = ()
    privacy_budget: DPBudget | None = None

    def __post_init__(self) -> None:
        if not self.marginal_sources:
            raise ValidationProblem("household synthesis needs marginal sources")
        unapproved = [s.source_id for s in self.joint_sources if s.approval != "approved"]
        if unapproved:
            raise ValidationProblem(f"joint data sources not approved: {unapproved}")
        if not self.structural_constraints:
            raise ValidationProblem("structural household constraints must be declared")
        if self.sensitive_attributes_allowed and self.privacy_budget is None:
            raise ValidationProblem("sensitive attributes require an explicit privacy budget")


@dataclass(frozen=True)
class HouseholdPopulationRef:
    population_id: str
    manifest_sha256: str
    households: int
    persons: int
    joint_uncertainty: Mapping[str, object]


@runtime_checkable
class HouseholdSynthesizer(Protocol):
    def synthesize(self, spec: HouseholdSynthesisSpec, *, seed: int) -> HouseholdPopulationRef: ...


def synthesize_households(spec: HouseholdSynthesisSpec, *, seed: int) -> HouseholdPopulationRef:
    """Reserved: raises ``CapabilityGap`` (population.households)."""
    require("population.households", universe=spec.universe, joint_sources=len(spec.joint_sources))
    raise AssertionError("unreachable")  # pragma: no cover


def open_privacy_accountant(budget: DPBudget) -> PrivacyAccountant:
    """Reserved: raises ``CapabilityGap`` (population.privacy_accounting)."""
    require("population.privacy_accounting", neighbouring=budget.neighbouring, composition=budget.composition)
    raise AssertionError("unreachable")  # pragma: no cover
