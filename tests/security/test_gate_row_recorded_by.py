"""GATE2: ``promote_connector``'s gate-row check also requires ``recorded_by = 'reva_gate'``.

Defence in depth: the approval functions already refuse such a pass at the database; the
application refuses it before loading anything. A forged row (trigger bypass, or a database
from before the gate separation) with every other field right is rejected; the same row
recorded by the gate login is accepted.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from test_db_hardening import new_artifact, tx

pytestmark = pytest.mark.db


def test_verify_gate_row_requires_the_gate_login(db: Any) -> None:
    from reva.autonomy.jobs import _verify_gate_row
    from reva.kernel.errors import PolicyDenied
    from reva.ports.autonomy import GateDecision

    with db.owner.connect() as c:
        if not c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one():
            pytest.skip("forging a gate row needs a superuser (trigger bypass)")
    with tx(db.owner) as c:
        sha = new_artifact(c)
        pid = c.execute(
            text(
                "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author) "
                "VALUES ('connector_promotion', :s, 1, 'agent:test') RETURNING id"
            ),
            {"s": sha},
        ).scalar_one()
        c.execute(text("SET LOCAL session_replication_role = replica"))
        rows = {}
        for who in ("reva_worker", "reva_owner", "reva_gate"):
            rows[who] = c.execute(
                text(
                    "INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision, evidence_sha256, "
                    "evaluator, recorded_by) VALUES (:p, 'promotion', 'promotion-1', 'pass', :s, "
                    "'gate-evaluator:promotion', :r) RETURNING id"
                ),
                {"p": pid, "s": sha, "r": who},
            ).scalar_one()
        c.execute(text("SET LOCAL session_replication_role = origin"))

        def decision(gid: Any) -> GateDecision:
            return GateDecision(
                uuid.UUID(str(gid)),
                "promote",
                uuid.UUID(str(pid)),
                "promotion-1",
                (),
                (),
                "gate-evaluator:promotion",
                None,
            )

        for forger in ("reva_worker", "reva_owner"):
            with pytest.raises(PolicyDenied, match="recorded passing gate result"):
                _verify_gate_row(c, decision(rows[forger]))
        ok = _verify_gate_row(c, decision(rows["reva_gate"]))  # positive control
        assert ok["recorded_by"] == "reva_gate" and ok["decision"] == "pass"
