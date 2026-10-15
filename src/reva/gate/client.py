"""Worker side of the independent promotion gate.

The worker never decides: it records *what* it wants evaluated (``ops.gate_request``:
the persisted proposal id plus the validator/sandbox report objects) and waits, bounded
by ``REVA_GATE_WAIT_SECONDS``, for the decision that the gate process records with its
own login (``reva_gate``). The decision is read back from ``ops.gate_result`` (only rows
with ``recorded_by = 'reva_gate'``) and its digest-verified evidence artifact; nothing
the worker supplied is echoed back as a decision.

If no decision arrives in time the client returns an *unrecorded* ``blocked`` decision
whose first reason starts with ``gate_pending``: nothing is promoted, and the request
stays queued for the gate. Once the gate records a pass, the scheduled ``gate_followup``
worker job (``reva.autonomy.followup``) promotes it from the stored proposal.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from reva.autonomy.common import ArtifactIO, ref_from, ref_json
from reva.gate import GATE_LOGIN
from reva.kernel.errors import ValidationProblem
from reva.ports.autonomy import ArtifactRef, Finding, GateDecision, PlatformChangeProposal, ValidationReport

log = logging.getLogger("reva.gate")

GATE_PREFIX = "gate-evaluator:"
#: Gate outcome -> ops.gate_result.decision (same mapping as reva.autonomy.gate.DB_DECISION).
DB_DECISION = {"promote": "pass", "reject": "fail", "needs_review": "needs_review", "blocked": "blocked"}
PENDING_REASON = "gate_pending"
MAX_REPORTS = 16


# --------------------------------------------------------------------------- report (de)serialisation
def report_doc(r: ValidationReport) -> dict[str, Any]:
    """A report object as stored in ``ops.gate_request.reports`` (refs kept whole)."""
    return {
        "report_id": str(r.report_id),
        "outcome": r.outcome,
        "findings": [{"code": f.code, "severity": f.severity, "message": f.message[:2000],
                      "evidence": [ref_json(e) for e in f.evidence]} for f in r.findings][:200],
        "evidence_manifest": ref_json(r.evidence_manifest),
        "evaluator_version": r.evaluator_version,
        "resource_usage": json.loads(json.dumps(dict(r.resource_usage), default=str)),
    }


def report_from_doc(d: Mapping[str, Any]) -> ValidationReport:
    """Parse a stored report object; anything malformed is a typed ``ValidationProblem``."""
    try:
        outcome = str(d["outcome"])
        if outcome not in ("pass", "fail", "blocked"):
            raise ValueError(outcome)
        findings = []
        for f in d.get("findings") or []:
            sev = str(f["severity"])
            if sev not in ("info", "warning", "blocker"):
                raise ValueError(sev)
            findings.append(Finding(code=str(f["code"]), severity=sev,  # type: ignore[arg-type]
                                    message=str(f.get("message", "")),
                                    evidence=tuple(ref_from(e) for e in f.get("evidence") or [])))
        return ValidationReport(
            report_id=uuid.UUID(str(d["report_id"])), outcome=outcome,  # type: ignore[arg-type]
            findings=tuple(findings), evidence_manifest=ref_from(d["evidence_manifest"]),
            evaluator_version=str(d["evaluator_version"]), resource_usage=dict(d.get("resource_usage") or {}),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValidationProblem(f"malformed report in gate request: {type(e).__name__}") from None


# --------------------------------------------------------------------------- requests and decisions
def request_gate(engine: Engine, proposal_id: uuid.UUID, reports: Sequence[ValidationReport], *,
                 gate_name: str = "promotion", policy_sha256: str | None = None) -> tuple[uuid.UUID, datetime]:
    """File a gate request (worker login). Returns (request id, server recorded_at)."""
    if len(reports) > MAX_REPORTS:
        raise ValidationProblem(f"at most {MAX_REPORTS} reports per gate request")
    with engine.begin() as conn:
        row = conn.execute(text(
            """INSERT INTO ops.gate_request(proposal_id, gate_name, reports, policy_sha256)
               VALUES (:p, :g, CAST(:r AS jsonb), :s) RETURNING id, recorded_at"""),
            {"p": proposal_id, "g": gate_name, "r": json.dumps([report_doc(r) for r in reports]),
             "s": policy_sha256}).one()
    return uuid.UUID(str(row.id)), row.recorded_at


def recorded_decision(engine: Engine, io_: ArtifactIO, proposal_id: uuid.UUID, *, gate_name: str = "promotion",
                      since: datetime | None = None) -> GateDecision | None:
    """The newest decision the independent gate (``reva_gate``) recorded for ``proposal_id``.

    The outcome comes from the gate's own digest-verified evidence and must agree with the
    database row; a disagreement is reported as an (unrecorded) ``blocked`` decision.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            """SELECT id, decision, evaluator, evidence_sha256, policy_version, recorded_at
               FROM ops.gate_result
               WHERE proposal_id = :p AND gate_name = :g AND recorded_by = :login
                 AND (CAST(:since AS timestamptz) IS NULL OR recorded_at >= CAST(:since AS timestamptz))
               ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
            {"p": proposal_id, "g": gate_name, "login": GATE_LOGIN, "since": since}).mappings().one_or_none()
    if row is None:
        return None
    receipt = ArtifactRef(str(row["evidence_sha256"]), "evaluation", "application/json")
    reasons: tuple[str, ...]
    try:
        ev = io_.get_json(receipt)  # digest-verified
        outcome = str(ev.get("outcome"))
        consistent = (DB_DECISION.get(outcome) == row["decision"] and ev.get("proposal_id") == str(proposal_id)
                      and ev.get("evaluator") == row["evaluator"])
        reasons = tuple(str(x) for x in ev.get("reasons") or [])
        reports = tuple(ArtifactRef(str(r.get("evidence_manifest")), "evaluation", "application/json")
                        for r in ev.get("reports") or [] if isinstance(r, dict))
    except Exception as e:  # unreadable evidence: fail closed
        consistent, outcome, reasons, reports = False, "blocked", (f"gate evidence unreadable: {type(e).__name__}",), ()
    if not consistent:
        return GateDecision(uuid.UUID(str(row["id"])), "blocked", proposal_id, str(row["policy_version"]), (),
                            ("gate result and its evidence disagree; treated as blocked", *reasons),
                            str(row["evaluator"]), None)
    return GateDecision(
        decision_id=uuid.UUID(str(row["id"])), outcome=outcome,  # type: ignore[arg-type]
        proposal_id=proposal_id, policy_version=str(row["policy_version"]), reports=reports, reasons=reasons,
        reviewer_identity=str(row["evaluator"]), signed_receipt=receipt,
    )


class GateClient:
    """Drop-in for ``PolicyPromotionGate.decide`` on the worker: request, then await the gate process."""

    def __init__(self, engine: Engine, io_: ArtifactIO, *, name: str = "promotion", guard: Any = None,
                 wait_seconds: float = 120.0, poll_seconds: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        if not name or ":" in name:
            raise ValueError("gate name must be a plain identifier")
        self.engine = engine
        self.io = io_
        self.name = name
        self.evaluator = f"{GATE_PREFIX}{name}"
        self.guard = guard
        self.wait_seconds = float(wait_seconds)
        self.poll_seconds = max(0.01, float(poll_seconds))
        self._sleep = sleep
        self._clock = clock
        self.last_request_id: uuid.UUID | None = None

    @classmethod
    def from_settings(cls, engine: Engine, io_: ArtifactIO, settings: Any, *, guard: Any = None,
                      name: str = "promotion") -> GateClient:
        wait = float(getattr(settings, "reva_gate_wait_seconds", 120.0) or 120.0)
        return cls(engine, io_, name=name, guard=guard, wait_seconds=wait,
                   poll_seconds=min(1.0, max(0.05, wait / 100)))

    def decide(self, proposal: PlatformChangeProposal | uuid.UUID, independent_reports: Sequence[ValidationReport],
               signed_policy: ArtifactRef | None = None) -> GateDecision:
        pid = proposal.proposal_id if isinstance(proposal, PlatformChangeProposal) else uuid.UUID(str(proposal))
        refs = tuple(r.evidence_manifest for r in independent_reports)
        if self.guard is not None and self.guard.blocked_reason():
            return GateDecision(uuid.uuid4(), "blocked", pid, "n/a", refs, (self.guard.blocked_reason() or "",),
                                self.evaluator, None)
        req, since = request_gate(self.engine, pid, independent_reports, gate_name=self.name,
                                  policy_sha256=signed_policy.sha256 if signed_policy is not None else None)
        self.last_request_id = req
        deadline = self._clock() + self.wait_seconds
        while True:
            d = recorded_decision(self.engine, self.io, pid, gate_name=self.name, since=since)
            if d is not None:
                return d
            if self._clock() >= deadline:
                break
            self._sleep(self.poll_seconds)
        log.warning("gate request %s for proposal %s: no decision within %.0fs", req, pid, self.wait_seconds)
        return GateDecision(
            decision_id=req, outcome="blocked", proposal_id=pid, policy_version="n/a", reports=refs,
            reasons=(f"{PENDING_REASON}: the independent gate process has not recorded a decision within "
                     f"{self.wait_seconds:.0f}s (request {req}); nothing was promoted. The request stays queued; "
                     "the gate_followup job promotes a later gate pass from the stored proposal.",),
            reviewer_identity=self.evaluator, signed_receipt=None,
        )


__all__ = ["DB_DECISION", "GATE_PREFIX", "PENDING_REASON", "GateClient", "recorded_decision", "report_doc",
           "report_from_doc", "request_gate"]
