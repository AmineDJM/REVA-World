"""ValueOfInformationEstimator V1 — an adapter over ``reva.evaluation.voi`` (agent B).

* With a finite decision specification (binary state, two actions, utilities,
  test sensitivity/specificity, cost): ``decision_evsi`` in the stated utility unit.
* Without one: the transparent, unitless ``uncertainty_reduction_proxy`` — never
  money. Its inputs (prior sd, expected posterior sd, relevance) are stated in the
  acquisition plan or derived by the documented default below, and recorded in
  the assumptions artifact.
* Anything else: ``unsupported`` with the reason.
"""

from __future__ import annotations

from typing import Any

from reva.autonomy.common import ArtifactIO
from reva.evaluation import voi as voi_core
from reva.kernel.errors import MissingInput, ValidationProblem
from reva.ports.autonomy import ArtifactRef, KnowledgeGap, ResourceBudget, ValueEstimate

ADAPTER_VERSION = "reva.autonomy.voi@1.0.0"
DEFAULT_PROXY_RULE = (
    "default when the plan states no sds: prior_sd = 1 (normalised; no eligible estimate), "
    "expected_posterior_sd = 1 - expected_coverage_gain, relevance_weight = min(1, gap.priority / max_priority)"
)


class GapValueEstimator:
    def __init__(self, io_: ArtifactIO, max_priority: float = 2.0) -> None:
        self.io = io_
        self.max_priority = max_priority

    def estimate(
        self,
        gap: KnowledgeGap,
        acquisition_plan: ArtifactRef,
        decision_spec: ArtifactRef | None,
        budget: ResourceBudget,
    ) -> ValueEstimate:
        plan = self.io.get_json(acquisition_plan) or {}
        assumptions: dict[str, Any] = {
            "schema": "reva-voi-assumptions/1",
            "gap_id": str(gap.gap_id),
            "requirement_id": gap.requirement_id,
            "plan": acquisition_plan.sha256,
            "adapter": ADAPTER_VERSION,
        }
        if decision_spec is not None:
            spec = self.io.get_json(decision_spec)
            try:
                problem = voi_core.DecisionProblem(
                    prior=float(spec["prior"]),
                    utility=tuple(tuple(float(u) for u in row) for row in spec["utility"]),  # type: ignore[arg-type]
                    sensitivity=float(spec["sensitivity"]),
                    specificity=float(spec["specificity"]),
                    cost=float(spec.get("cost", 0.0)),
                    utility_unit=str(spec.get("utility_unit", "utility")),
                )
                est = voi_core.estimate_value({"gap_id": str(gap.gap_id)}, problem)
            except (KeyError, TypeError, ValueError, ValidationProblem) as e:
                assumptions["unsupported_reason"] = f"decision specification is not a finite binary problem: {e}"
                return self._unsupported(assumptions)
            assumptions.update({"decision_spec": decision_spec.sha256, "core_assumptions": list(est.assumptions)})
            return self._wrap(est, assumptions)
        prior_sd = plan.get("prior_sd")
        post_sd = plan.get("expected_posterior_sd")
        relevance = plan.get("relevance_weight")
        if prior_sd is None and post_sd is None and "expected_coverage_gain" in plan:
            gain = float(plan["expected_coverage_gain"])
            prior_sd, post_sd = 1.0, max(0.0, 1.0 - min(1.0, gain))
            assumptions["default_rule"] = DEFAULT_PROXY_RULE
        if relevance is None:
            relevance = max(0.0, min(1.0, float(gap.priority) / self.max_priority))
        try:
            est = voi_core.estimate_value(
                {
                    "gap_id": str(gap.gap_id),
                    "prior_sd": prior_sd,
                    "expected_posterior_sd": post_sd,
                    "relevance_weight": relevance,
                }
            )
        except (MissingInput, ValidationProblem) as e:
            assumptions["unsupported_reason"] = e.message
            return self._unsupported(assumptions)
        assumptions.update(
            {
                "inputs": {"prior_sd": prior_sd, "expected_posterior_sd": post_sd, "relevance_weight": relevance},
                "core_assumptions": list(est.assumptions),
                "not_money": True,
            }
        )
        return self._wrap(est, assumptions)

    def _wrap(self, est: Any, assumptions: dict[str, Any]) -> ValueEstimate:
        ref = self.io.put_json(assumptions, "evaluation")
        return ValueEstimate(
            kind=est.kind,
            value=est.value,
            unit=est.unit,
            uncertainty=dict(est.uncertainty),
            assumptions=ref,
            method_version=est.method_version,
        )

    def _unsupported(self, assumptions: dict[str, Any]) -> ValueEstimate:
        ref = self.io.put_json(assumptions, "evaluation")
        return ValueEstimate(
            kind="unsupported",
            value=None,
            unit=None,
            uncertainty={"status": "unquantified", "kind": "unknown", "description": "no value estimate"},
            assumptions=ref,
            method_version=ADAPTER_VERSION,
        )
