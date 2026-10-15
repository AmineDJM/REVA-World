"""ModelChallenger V1: bounded last-value / log-drift / local-level comparison proposals.

Proposals are T2 (model change → isolated evaluation + named review). The set
of challengers is fixed and bounded; a (challenger, incumbent, holdout) triple
already tested is never proposed again (repeated retesting of a holdout is
prohibited). Expected benefit is ``unsupported`` until evaluated: no decision
specification exists for forecast choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from reva.autonomy.common import ArtifactIO, stable_id
from reva.autonomy.policy import PinnedPolicy, promotion_policy
from reva.autonomy.proposals import base_commit
from reva.kernel.ids import logical_hash
from reva.ports.autonomy import (
    JSON,
    ModelDescriptor,
    PlatformChangeProposal,
    ResourceBudget,
    ScopedContext,
    ValueEstimate,
)

BOUNDED_CHALLENGERS = ("forecast.last_value", "forecast.log_drift", "forecast.local_level_log")
AUTHOR = "agent:model-challenger"


class BoundedModelChallenger:
    def __init__(self, io_: ArtifactIO, policy: PinnedPolicy | None = None) -> None:
        self.io = io_
        self.policy = policy or promotion_policy()

    def propose(
        self, task: Mapping[str, JSON], incumbent: ModelDescriptor, context: ScopedContext, budget: ResourceBudget
    ) -> list[PlatformChangeProposal]:
        if incumbent.model_key not in BOUNDED_CHALLENGERS:
            return []
        holdout = str(task.get("holdout_id") or logical_hash({"task": dict(task)}))
        tested = {(str(t[0]), str(t[1]), str(t[2])) for t in (task.get("tested") or [])}  # type: ignore[union-attr]
        out: list[PlatformChangeProposal] = []
        limit = max(0, min(len(BOUNDED_CHALLENGERS) - 1, budget.max_requests))
        for ch in BOUNDED_CHALLENGERS:
            if len(out) >= limit:
                break
            if ch == incumbent.model_key or (ch, incumbent.model_key, holdout) in tested:
                continue
            plan: dict[str, Any] = {
                "schema": "reva-model-challenge/1",
                "challenger": ch,
                "incumbent": incumbent.model_key,
                "incumbent_version": incumbent.version,
                "task": dict(task),
                "holdout_id": holdout,
                "replay_mode": "revised_data_retrospective",
                "promotion_rule": dict(self.policy.content.get("model_promotion") or {}),
                "context_id": str(context.context_id),
                "cutoff": context.knowledge_cutoff.isoformat(),
                "keep_naive_baseline": True,
            }
            plan_ref = self.io.put_json(plan, "models")
            note = self.io.put_json(
                {
                    "reason": "no decision specification for forecast selection; "
                    "benefit is unknown until the tournament is run"
                },
                "evaluation",
            )
            out.append(
                PlatformChangeProposal(
                    proposal_id=stable_id("challenge", ch, incumbent.model_key, holdout, plan_ref.sha256),
                    author=AUTHOR,
                    permission_tier=2,
                    changed_paths=(),
                    base_commit=base_commit(),
                    patch_or_manifest=plan_ref,
                    requested_capabilities=("run_backtest",),
                    scientific_change=True,
                    migration_required=False,
                    expected_benefit=ValueEstimate(
                        "unsupported",
                        None,
                        None,
                        {"status": "unquantified", "kind": "unknown", "description": "not evaluated"},
                        note,
                        "challenger@1",
                    ),
                    policy_version=self.policy.version,
                )
            )
        return out
