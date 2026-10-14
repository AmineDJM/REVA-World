"""PlatformChangeProposal persistence: immutable manifest artifact + ops.proposal row.

The manifest records tier, scope, risk, dependencies, changed paths, requested
capabilities and expected benefit. Rows are immutable (DB trigger); the open
backlog is bounded by policies/budgets.yaml (``autonomy.max_open_proposals``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.autonomy.common import ArtifactIO, ref_from, ref_json
from reva.autonomy.policy import AutonomyGuard
from reva.kernel.errors import ImmutableRecordViolation
from reva.ports.autonomy import ArtifactRef, PlatformChangeProposal, ValueEstimate

PROPOSAL_SCHEMA = "reva-platform-change-proposal/1"


def base_commit() -> str:
    from reva.operations.environment import code_digest

    return os.environ.get("REVA_GIT_COMMIT") or code_digest()


def value_json(v: ValueEstimate) -> dict[str, Any]:
    return {"kind": v.kind, "value": v.value, "unit": v.unit, "uncertainty": dict(v.uncertainty),
            "assumptions": ref_json(v.assumptions), "method_version": v.method_version}


def value_from(d: Mapping[str, Any]) -> ValueEstimate:
    return ValueEstimate(kind=d["kind"], value=d["value"], unit=d["unit"], uncertainty=dict(d["uncertainty"]),
                         assumptions=ref_from(d["assumptions"]), method_version=d["method_version"])


def proposal_json(p: PlatformChangeProposal) -> dict[str, Any]:
    return {
        "proposal_id": str(p.proposal_id), "author": p.author, "permission_tier": p.permission_tier,
        "changed_paths": list(p.changed_paths), "base_commit": p.base_commit,
        "patch_or_manifest": ref_json(p.patch_or_manifest),
        "requested_capabilities": list(p.requested_capabilities), "scientific_change": p.scientific_change,
        "migration_required": p.migration_required, "expected_benefit": value_json(p.expected_benefit),
        "policy_version": p.policy_version,
    }


def proposal_from(d: Mapping[str, Any]) -> PlatformChangeProposal:
    return PlatformChangeProposal(
        proposal_id=UUID(d["proposal_id"]), author=d["author"], permission_tier=int(d["permission_tier"]),  # type: ignore[arg-type]
        changed_paths=tuple(d["changed_paths"]), base_commit=d["base_commit"],
        patch_or_manifest=ref_from(d["patch_or_manifest"]),
        requested_capabilities=tuple(d["requested_capabilities"]), scientific_change=bool(d["scientific_change"]),
        migration_required=bool(d["migration_required"]), expected_benefit=value_from(d["expected_benefit"]),
        policy_version=d["policy_version"],
    )


class ProposalStore:
    def __init__(self, engine: Engine, io_: ArtifactIO, guard: AutonomyGuard | None = None) -> None:
        self.engine = engine
        self.io = io_
        self.guard = guard

    def persist(self, proposal: PlatformChangeProposal, *, proposal_type: str, scope: Mapping[str, Any],
                risk: Mapping[str, Any], dependencies: Sequence[str] = (),
                summary: Mapping[str, Any] | None = None) -> ArtifactRef:
        if self.guard is not None:
            self.guard.check("propose")
        manifest = {"schema": PROPOSAL_SCHEMA, "proposal_type": proposal_type, "proposal": proposal_json(proposal),
                    "scope": dict(scope), "risk": dict(risk), "dependencies": list(dependencies)}
        ref = self.io.put_json(manifest, "models")
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT manifest_sha256 FROM ops.proposal WHERE id = :i"),
                               {"i": proposal.proposal_id}).scalar_one_or_none()
            if row is not None:
                if row != ref.sha256:
                    raise ImmutableRecordViolation("proposal id reused with a different manifest",
                                                   proposal_id=str(proposal.proposal_id))
                return ref
            if self.guard is not None:
                self.guard.check_backlog(conn)
            self.io.register(conn, ref, "platform-change-proposal/1")
            conn.execute(
                text("""INSERT INTO ops.proposal(id, proposal_type, manifest_sha256, permission_tier, summary, author)
                        VALUES (:i, :t, :m, :tier, CAST(:s AS jsonb), :a)"""),
                {"i": proposal.proposal_id, "t": proposal_type, "m": ref.sha256, "tier": proposal.permission_tier,
                 "s": json.dumps({**dict(summary or {}), "scope": dict(scope), "risk": dict(risk),
                                  "dependencies": list(dependencies)}, default=str),
                 "a": proposal.author},
            )
        return ref

    def load(self, proposal_id: UUID) -> PlatformChangeProposal:
        with self.engine.connect() as conn:
            sha = conn.execute(text("SELECT manifest_sha256 FROM ops.proposal WHERE id = :i"),
                               {"i": proposal_id}).scalar_one()
        doc = self.io.get_json(ArtifactRef(sha, "models", "application/json"))
        return proposal_from(doc["proposal"])
