"""``gate_followup``: promote connector proposals whose gate decision arrived late.

When the independent gate process (``reva gate``) is down or slow, discovery's
``GateClient`` stops waiting after ``REVA_GATE_WAIT_SECONDS`` and reports
``status: gate_pending``. The request stays queued and the gate decides it later,
but no one is waiting for that decision any more. This scheduled worker job closes
the loop: for every ``connector_promotion`` proposal whose *newest* decision by the
gate login (``recorded_by = 'reva_gate'``) is a pass that has not been spent on an
approval yet, it performs the promotion through the existing path
(``reva.autonomy.jobs.promote_connector`` → ``evidence.approve_eligibility``).

Properties:

* **No provider data is re-fetched.** ``promote_connector`` loads the manifest and
  mapping from the stored, digest-verified proposal document the gate pass names
  (R6-02); this job makes no network requests.
* **Idempotent.** Candidates exclude passes already consumed by an approval
  (``ops.approval_use``) and passes with a recorded follow-up outcome
  (``ops.outbox`` event ``autonomy.gate_followup``). ``promote_connector`` itself
  re-checks inside its transaction under the evidence write lock (``already_promoted``,
  ``rolled_back_hold``) and the database spends one gate pass on one approval.
* **Fenced.** ``promote_connector`` asserts the job lease inside the promotion
  transaction; the outcome record asserts it too. A worker that lost its lease
  promotes nothing.
* **Kill-switch aware.** The autonomy guard is read before the run, before every
  candidate and again inside the promotion transaction. A blocked run records
  nothing, so the candidates stay eligible for the next run.
* **Fail closed.** A typed refusal (policy, binding, immutable record, database
  refusal) is recorded as a terminal outcome so it is not retried every few minutes;
  a transient database error is left for the next run.
* **Bounded.** Only passes younger than ``max_age_hours`` (default 168, at most 720)
  and older than the worker's own gate wait (``REVA_GATE_WAIT_SECONDS``: a waiting
  discovery step handles its own decision) are considered, at most ``limit`` per run.

Payload (all optional): ``{"limit": 1..100, "max_age_hours": 1..720}``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from reva.adapters.postgres import repositories as repo
from reva.adapters.postgres.queue import STALE_LEASE_SQLSTATE
from reva.autonomy.common import ArtifactIO, AutonomyBlocked
from reva.autonomy.policy import AutonomyGuard, promotion_policy
from reva.gate import GATE_LOGIN
from reva.kernel.errors import RevaError, StaleLease

log = logging.getLogger("reva.autonomy")

EVENT = "autonomy.gate_followup"
PAYLOAD_KEYS = frozenset({"limit", "max_age_hours"})
DEFAULT_LIMIT = 20
DEFAULT_MAX_AGE_HOURS = 168
#: Seconds past the worker's own gate wait before a decision counts as "late".
GRACE_SECONDS = 30.0

CANDIDATES_SQL = """
SELECT g.id AS gate_result_id, g.proposal_id, g.recorded_at
FROM ops.gate_result g JOIN ops.proposal p ON p.id = g.proposal_id
WHERE p.proposal_type = 'connector_promotion'
  AND g.gate_name = 'promotion' AND g.recorded_by = :login AND g.decision = 'pass'
  AND g.recorded_at >= clock_timestamp() - make_interval(hours => :max_age_hours)
  AND g.recorded_at <= clock_timestamp() - make_interval(secs => :min_age_seconds)
  -- the newest decision for the proposal (a later non-pass supersedes it in ops.consume_approval too)
  AND NOT EXISTS (SELECT 1 FROM ops.gate_result x
                  WHERE x.proposal_id = g.proposal_id AND (x.recorded_at, x.id) > (g.recorded_at, g.id))
  -- not spent on an approval yet (the promotion has not completed)
  AND NOT EXISTS (SELECT 1 FROM ops.approval_use u WHERE u.approval_id = g.id AND u.kind = 'eligibility')
  -- no terminal follow-up outcome recorded yet
  AND NOT EXISTS (SELECT 1 FROM ops.outbox o
                  WHERE o.event_type = :event AND o.created_at >= g.recorded_at
                    AND o.payload->>'gate_result_id' = g.id::text)
ORDER BY g.recorded_at, g.id
LIMIT :n
"""


def _bounded_int(payload: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int | None:
    v = payload.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return None
    return v


def pending_followups(engine: Engine, *, limit: int = DEFAULT_LIMIT, max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
                      min_age_seconds: float = 0.0) -> list[dict[str, Any]]:
    """Passing ``reva_gate`` decisions of connector promotions that no promotion has consumed yet."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(CANDIDATES_SQL), {
            "login": GATE_LOGIN, "max_age_hours": int(max_age_hours), "min_age_seconds": float(min_age_seconds),
            "event": EVENT, "n": int(limit)}).mappings()]


def _record(engine: Engine, ctx: Any, item: dict[str, Any]) -> None:
    try:
        with engine.begin() as conn:
            if getattr(ctx, "job_id", None) is not None and hasattr(ctx, "assert_lease"):
                ctx.assert_lease(conn)  # fenced like the promotion itself
            repo.emit_outbox(conn, EVENT, {**item, "job_id": str(getattr(ctx, "job_id", "") or "") or None})
    except DBAPIError as e:
        if getattr(getattr(e, "orig", None), "sqlstate", None) == STALE_LEASE_SQLSTATE:
            raise StaleLease(f"lease lost for job {getattr(ctx, 'job_id', None)}",
                             job_id=str(getattr(ctx, "job_id", None))) from None
        raise


def handle_gate_followup(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Promote connector proposals whose passing gate decision arrived after discovery stopped waiting."""
    from reva.autonomy.jobs import promote_connector
    from reva.gate.client import recorded_decision

    guard = AutonomyGuard.from_settings(ctx.settings)
    reason = guard.blocked_reason()
    if reason:
        return {"status": "blocked", "reason": reason}
    payload = dict(payload or {})
    unknown = sorted(str(k)[:40] for k in payload if k not in PAYLOAD_KEYS)
    if unknown:
        return {"status": "rejected", "reason": "unsupported payload keys", "unsupported": unknown}
    limit = _bounded_int(payload, "limit", DEFAULT_LIMIT, 1, 100)
    max_age = _bounded_int(payload, "max_age_hours", DEFAULT_MAX_AGE_HOURS, 1, 720)
    if limit is None or max_age is None:
        return {"status": "rejected", "reason": "limit must be 1..100 and max_age_hours 1..720 (integers)"}
    engine: Engine = ctx.worker_engine
    io_ = ArtifactIO(ctx.store)
    wait = float(getattr(ctx.settings, "reva_gate_wait_seconds", 120.0) or 120.0)
    candidates = pending_followups(engine, limit=limit, max_age_hours=max_age, min_age_seconds=wait + GRACE_SECONDS)
    policy = promotion_policy() if candidates else None
    results: list[dict[str, Any]] = []
    status = "ok"
    for cand in candidates:
        reason = guard.blocked_reason()
        if reason:  # nothing recorded: the remaining candidates stay for the next run
            status = "blocked"
            break
        if hasattr(ctx, "is_cancelled") and ctx.is_cancelled():
            status = "cancelled"
            break
        gid, pid = UUID(str(cand["gate_result_id"])), UUID(str(cand["proposal_id"]))
        item: dict[str, Any] = {"gate_result_id": str(gid), "proposal_id": str(pid)}
        try:
            decision = recorded_decision(engine, io_, pid)
            if decision is None or decision.decision_id != gid or decision.outcome != "promote":
                # newest decision changed meanwhile, or its evidence disagrees with the row: fail closed
                item.update(status="not_promotable",
                            reason=(decision.reasons[0][:300] if decision is not None and decision.reasons
                                    else "the newest gate decision is not this promote decision"))
            else:
                out = promote_connector(engine, io_, ctx, decision, policy, guard=guard)  # type: ignore[arg-type]
                item.update({k: out[k] for k in ("status", "version", "connector_version_id", "superseded")
                             if k in out})
        except StaleLease:
            raise  # fenced: this worker no longer owns the job; record nothing
        except AutonomyBlocked as e:  # kill switch flipped mid-run: record nothing, stop
            results.append({**item, "status": "blocked", "reason": e.message[:300]})
            status = "blocked"
            break
        except RevaError as e:  # typed refusal: terminal for this gate pass
            if e.details.get("retryable"):
                results.append({**item, "status": "retry", "code": e.code})
                continue
            item.update(status="refused", code=e.code, reason=e.message[:300])
        except OperationalError as e:  # database unavailable / timeout: try again next run
            results.append({**item, "status": "retry", "kind": type(getattr(e, "orig", None) or e).__name__[:60]})
            continue
        except (DBAPIError, SQLAlchemyError) as e:  # trigger / governance refusal: terminal, typed, no params
            orig = getattr(e, "orig", None)
            if getattr(orig, "sqlstate", None) == STALE_LEASE_SQLSTATE:  # fenced: lost the lease, record nothing
                raise StaleLease(f"lease lost for job {getattr(ctx, 'job_id', None)}",
                                 job_id=str(getattr(ctx, "job_id", None))) from None
            item.update(status="refused", code="database_refused", kind=type(orig or e).__name__[:60],
                        sqlstate=str(getattr(orig, "sqlstate", "") or "")[:10])
        _record(engine, ctx, item)
        results.append(item)
        log.info("gate_followup: proposal %s gate result %s -> %s", pid, gid, item["status"])
    promoted = sum(1 for r in results if r.get("status") == "promoted")
    return {"status": status, "candidates": len(candidates), "promoted": promoted, "results": results,
            **({"reason": guard.blocked_reason()} if status == "blocked" and guard.blocked_reason() else {})}


__all__ = ["EVENT", "handle_gate_followup", "pending_followups"]
