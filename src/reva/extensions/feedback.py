"""P9 feedback-coupling solver contract (architecture §9, §16.2).

V1 executes acyclic one-way coupling only (``reva.simulation.coupling``
detects strongly connected components and rejects them). A future feedback
block needs an explicit :class:`ConvergenceContract`: step size, convergence
criterion and tolerance, damping, iteration cap, conservative exchange and
error bounds. :func:`solve` refuses:

* without a contract → ``CapabilityGap`` with ``refusal="no_convergence_contract"``;
* with a valid contract → still ``CapabilityGap`` with
  ``refusal="not_implemented_in_v1"`` (capability ``coupling.feedback`` is
  reserved until a solver converges on reference problems).

:class:`FeedbackOutcome` fixes the result semantics later solvers must obey:
a non-converged run is ``failed`` or ``qualified_partial`` and may never carry
an equilibrium claim or present its last iterate as an equilibrium.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from reva.capabilities import not_available
from reva.kernel.errors import CapabilityGap, ValidationProblem

Criterion = Literal["residual_inf_norm", "relative_residual_l2", "relative_step_inf_norm"]


@dataclass(frozen=True)
class ConvergenceContract:
    step_size: float
    """Time step (in the coupled block's time unit) or pseudo-time step for the fixed point."""
    criterion: Criterion
    tolerance: float
    damping: float
    """Under-relaxation factor ω in (0, 1]; x_{k+1} = (1−ω)x_k + ω·G(x_k)."""
    max_iterations: int
    conservative_exchange: bool
    """Exchanged quantities are conserved across component boundaries (must be True)."""
    error_bounds: Mapping[str, float]
    """Declared admissible numerical error per exchanged variable, in that variable's unit."""
    on_failure: Literal["fail", "qualified_partial"] = "fail"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.step_size) and self.step_size > 0):
            raise ValidationProblem("convergence contract: step_size must be positive and finite")
        if not (math.isfinite(self.tolerance) and self.tolerance > 0):
            raise ValidationProblem("convergence contract: tolerance must be positive and finite")
        if not (0 < self.damping <= 1):
            raise ValidationProblem("convergence contract: damping must lie in (0, 1]")
        if not (isinstance(self.max_iterations, int) and 1 <= self.max_iterations <= 100_000):
            raise ValidationProblem("convergence contract: max_iterations must be an int in [1, 100000]")
        if self.conservative_exchange is not True:
            raise ValidationProblem("convergence contract: conservative exchange is mandatory")
        if not self.error_bounds:
            raise ValidationProblem("convergence contract: error bounds per exchanged variable are required")
        for name, bound in self.error_bounds.items():
            if not (math.isfinite(bound) and bound > 0):
                raise ValidationProblem(f"convergence contract: error bound for {name!r} must be positive")


@dataclass(frozen=True)
class FeedbackSolverConfig:
    block_id: str
    components: tuple[str, ...]
    """Model ids forming one strongly connected component."""
    exchanged_variables: tuple[str, ...]
    contract: ConvergenceContract | None
    method: Literal["damped_fixed_point", "newton_krylov", "anderson"] = "damped_fixed_point"
    reference_problems: tuple[str, ...] = ()
    """Ids of reference problems on which the solver must have converged (P9 gate)."""

    def __post_init__(self) -> None:
        if not self.components or not self.exchanged_variables:
            raise ValidationProblem("a feedback block needs components and exchanged variables")
        if self.contract is not None:
            missing = sorted(set(self.exchanged_variables) - set(self.contract.error_bounds))
            if missing:
                raise ValidationProblem(f"error bounds missing for exchanged variables {missing}")


@dataclass(frozen=True)
class FeedbackOutcome:
    """Result type for a future feedback solver; enforces honest non-convergence."""

    status: Literal["converged", "failed", "qualified_partial"]
    iterations: int
    final_residual: float
    tolerance: float
    max_iterations: int
    values: Mapping[str, Any] | None
    equilibrium_claim: bool
    qualification: str | None = None
    residual_history: tuple[float, ...] = field(default=())

    def __post_init__(self) -> None:
        converged = (
            math.isfinite(self.final_residual)
            and self.final_residual <= self.tolerance
            and self.iterations <= self.max_iterations
        )
        if self.status == "converged" and not converged:
            raise ValidationProblem("status 'converged' requires final residual <= tolerance within the cap")
        if self.equilibrium_claim and self.status != "converged":
            raise ValidationProblem("an equilibrium may only be claimed for a converged solve")
        if self.status == "failed" and self.values is not None:
            raise ValidationProblem("a failed solve must not publish values (no last-iterate results)")
        if self.status == "qualified_partial" and not (self.qualification and self.qualification.strip()):
            raise ValidationProblem("qualified partial results need an explicit qualification")


def solve(config: FeedbackSolverConfig, inputs: Mapping[str, Any] | None = None) -> FeedbackOutcome:
    """Refuse feedback execution with a structured ``CapabilityGap`` (V1)."""
    gap = not_available("coupling.feedback")
    if config.contract is None:
        raise CapabilityGap(
            "feedback coupling requires an explicit convergence contract (step size, criterion, damping, "
            "iteration cap, conservative exchange, error bounds)",
            capability="coupling.feedback",
            refusal="no_convergence_contract",
            block_id=config.block_id,
            components=list(config.components),
            status=gap.status if gap else "implemented",
            phase=gap.phase if gap else None,
        )
    if gap is not None:
        err = gap.to_error()
        err.details.update(
            refusal="not_implemented_in_v1",
            block_id=config.block_id,
            components=list(config.components),
            method=config.method,
        )
        raise err
    raise CapabilityGap(  # pragma: no cover - catalogue says reserved; never execute without a solver
        "no feedback solver is registered", capability="coupling.feedback", refusal="no_solver"
    )
