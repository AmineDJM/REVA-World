"""The gate process: ``reva gate`` / ``python -m reva.gate.service`` / ``scripts/start_gate.py``.

Runs with the ``reva_gate`` login (``Settings.runtime_url("gate")``), never with the
worker's. A small loop:

1. find gate requests (``ops.gate_request``) that have no later decision recorded by
   ``reva_gate`` for the same proposal and gate (oldest first);
2. take a per-request session advisory lock (several gate processes never evaluate the
   same request twice) and re-check that it is still pending;
3. evaluate with the reviewed gate logic, ``reva.autonomy.gate.PolicyPromotionGate``, on
   the *persisted* proposal (``ops.proposal`` row + its digest-verified document), the
   stored, digest-verified report evidence and, for T1, its own recomputation of the
   validator and sandbox; the policy is the pinned policy file, never the request's;
4. the gate records the decision (``ops.record_gate_result`` as ``reva_gate``).

Failure handling (fail closed):

* kill switch / autonomy disabled: nothing is recorded, the request stays pending;
* a typed refusal (``RevaError``: unknown proposal, document mismatch, unreadable or
  tampered artifacts, malformed request): a ``blocked`` decision is recorded with the
  reason, so the request does not loop;
* an unexpected error (database or object store unavailable): retried on the next poll,
  and after ``max_attempts`` failures a ``blocked`` decision is recorded.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import signal
import threading
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from reva.autonomy.common import ArtifactIO
from reva.gate import GATE_LOGIN
from reva.gate.client import GATE_PREFIX, report_from_doc
from reva.kernel.errors import RevaError

log = logging.getLogger("reva.gate")

PENDING_SQL = """
SELECT r.id, r.proposal_id, r.gate_name, r.reports, r.recorded_at
FROM ops.gate_request r
WHERE r.gate_name = :g
  AND NOT EXISTS (SELECT 1 FROM ops.gate_result g
                  WHERE g.proposal_id = r.proposal_id AND g.gate_name = r.gate_name
                    AND g.recorded_by = :login AND g.recorded_at >= r.recorded_at)
  {extra}
ORDER BY r.recorded_at, r.id
LIMIT :n
"""
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended('reva-gate-request|' || CAST(:i AS text), 0))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('reva-gate-request|' || CAST(:i AS text), 0))"


class GateService:
    """Evaluates pending gate requests with the reva_gate login."""

    def __init__(
        self,
        engine: Engine,
        store: Any,
        settings: Any = None,
        *,
        name: str = "promotion",
        poll_seconds: float | None = None,
        max_attempts: int = 5,
        batch: int = 10,
        policy_path: Path | None = None,
        pinned_sha256: str | None = None,
        guard: Any = None,
    ) -> None:
        from reva.autonomy.policy import AutonomyGuard

        if not name or ":" in name:
            raise ValueError("gate name must be a plain identifier")
        self.engine = engine
        self.io = ArtifactIO(store)
        self.settings = settings
        self.name = name
        self.evaluator = f"{GATE_PREFIX}{name}"
        self.poll_seconds = float(
            poll_seconds if poll_seconds is not None else getattr(settings, "reva_gate_poll_seconds", 2.0) or 2.0
        )
        self.max_attempts = max(1, int(max_attempts))
        self.batch = max(1, int(batch))
        self.policy_path = policy_path
        self.pinned_sha256 = pinned_sha256
        self.guard = (
            guard if guard is not None else (AutonomyGuard.from_settings(settings) if settings is not None else None)
        )
        self._attempts: dict[uuid.UUID, int] = {}
        self._poisoned: set[uuid.UUID] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ queries
    def pending(self, limit: int | None = None, request_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        sql = PENDING_SQL.format(extra="AND r.id = :rid" if request_id is not None else "")
        params: dict[str, Any] = {"g": self.name, "login": GATE_LOGIN, "n": limit or self.batch}
        if request_id is not None:
            params["rid"] = request_id
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    # ------------------------------------------------------------------ loop
    def run_once(self) -> list[dict[str, Any]]:
        """Evaluate every currently pending request once. Returns one summary per request handled."""
        out: list[dict[str, Any]] = []
        for req in self.pending():
            if req["id"] in self._poisoned:
                continue
            res = self._process(req)
            if res is not None:
                out.append(res)
        return out

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or self._stop
        log.info(
            json.dumps(
                {"event": "reva.gate", "status": "started", "gate": self.name, "poll_seconds": self.poll_seconds}
            )
        )
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # database briefly unavailable: keep polling
                log.warning(json.dumps({"event": "reva.gate", "status": "poll_failed", "error": type(e).__name__}))
            stop.wait(self.poll_seconds)
        log.info(json.dumps({"event": "reva.gate", "status": "stopped", "gate": self.name}))

    def stop(self) -> None:
        self._stop.set()

    @contextlib.contextmanager
    def background(self) -> Iterator[GateService]:
        """Run the loop in a daemon thread (local development and tests)."""
        self._stop.clear()
        t = threading.Thread(target=self.run, name=f"reva-gate-{self.name}", daemon=True)
        t.start()
        try:
            yield self
        finally:
            self._stop.set()
            t.join(timeout=max(30.0, self.poll_seconds * 3))

    # ------------------------------------------------------------------ one request
    def _process(self, req: Mapping[str, Any]) -> dict[str, Any] | None:
        rid = uuid.UUID(str(req["id"]))
        with self.engine.connect() as lock_conn:
            if not lock_conn.execute(text(LOCK_SQL), {"i": str(rid)}).scalar_one():
                lock_conn.rollback()
                return None  # another gate process is evaluating it
            lock_conn.commit()
            try:
                if not self.pending(limit=1, request_id=rid):
                    return None  # decided meanwhile
                return self._evaluate(req)
            finally:
                with contextlib.suppress(Exception):
                    lock_conn.execute(text(UNLOCK_SQL), {"i": str(rid)})
                    lock_conn.commit()

    def _gate(self) -> Any:
        from reva.autonomy.gate import PolicyPromotionGate

        kw: dict[str, Any] = {}
        if self.policy_path is not None:
            kw["policy_path"] = self.policy_path
        if self.pinned_sha256 is not None:
            kw["pinned_sha256"] = self.pinned_sha256
        return PolicyPromotionGate(self.engine, self.io, name=self.name, guard=self.guard, **kw)

    def _signed_policy(self) -> Any:
        from reva.autonomy.policy import load_pinned, promotion_policy

        if self.policy_path is not None or self.pinned_sha256 is not None:
            from reva.autonomy.policy import PINNED_POLICY_DIGESTS, POLICY_DIR

            return load_pinned(
                self.policy_path or POLICY_DIR / "promotion.yaml",
                self.pinned_sha256 or PINNED_POLICY_DIGESTS["promotion.yaml"],
            ).ref()
        return promotion_policy().ref()

    def _evaluate(self, req: Mapping[str, Any]) -> dict[str, Any] | None:
        rid = uuid.UUID(str(req["id"]))
        pid = uuid.UUID(str(req["proposal_id"]))
        summary: dict[str, Any] = {"request_id": str(rid), "proposal_id": str(pid), "gate": self.name}
        if self.guard is not None and self.guard.blocked_reason():
            # Kill switch: no decision is recorded; the request waits until autonomy is re-enabled.
            return {**summary, "outcome": "blocked", "recorded": False, "reason": self.guard.blocked_reason()}
        try:
            reports = [report_from_doc(d) for d in (req.get("reports") or [])]
            try:
                signed = self._signed_policy()
            except RevaError:
                from reva.ports.autonomy import ArtifactRef

                signed = ArtifactRef("0" * 64, "policy", "application/yaml")  # the gate records 'blocked'
            d = self._gate().decide(pid, reports, signed)
        except RevaError as e:
            gid = self._record_failure(pid, rid, [f"gate refused the request: {e.code}: {e.message[:500]}"])
            return {
                **summary,
                "outcome": "blocked",
                "recorded": gid is not None,
                "gate_result_id": str(gid) if gid else None,
                "error": e.code,
            }
        except Exception as e:
            n = self._attempts.get(rid, 0) + 1
            self._attempts[rid] = n
            log.warning(
                json.dumps(
                    {
                        "event": "reva.gate",
                        "status": "evaluation_failed",
                        "request_id": str(rid),
                        "attempt": n,
                        "error": type(e).__name__,
                    }
                )
            )
            if n < self.max_attempts:
                return {**summary, "outcome": "retry", "recorded": False, "error": type(e).__name__}
            gid = self._record_failure(pid, rid, [f"gate evaluation failed {n} times: {type(e).__name__}"])
            return {
                **summary,
                "outcome": "blocked",
                "recorded": gid is not None,
                "gate_result_id": str(gid) if gid else None,
                "error": type(e).__name__,
            }
        self._attempts.pop(rid, None)
        recorded = d.signed_receipt is not None
        log.info(
            json.dumps(
                {
                    "event": "reva.gate",
                    "status": "decided",
                    "request_id": str(rid),
                    "proposal_id": str(pid),
                    "outcome": d.outcome,
                    "recorded": recorded,
                    "gate_result_id": str(d.decision_id),
                }
            )
        )
        return {
            **summary,
            "outcome": d.outcome,
            "recorded": recorded,
            "gate_result_id": str(d.decision_id),
            "reasons": list(d.reasons),
        }

    def _record_failure(self, pid: uuid.UUID, rid: uuid.UUID, reasons: list[str]) -> uuid.UUID | None:
        """Record a fail-closed ``blocked`` decision; if even that is refused, stop retrying this request."""
        from reva.autonomy.gate import record_gate_result

        version = "policy-integrity-failure"
        with contextlib.suppress(Exception):
            version = self._gate_policy_version()
        evidence = {
            "schema": "reva-gate-decision/1",
            "gate": self.name,
            "evaluator": self.evaluator,
            "proposal_id": str(pid),
            "gate_request_id": str(rid),
            "outcome": "blocked",
            "db_decision": "blocked",
            "reasons": reasons,
            "policy": {"version": version},
            "tier": {},
            "reports": [],
        }
        try:
            ref = self.io.put_json(evidence, "evaluation")
            with self.engine.begin() as conn:
                self.io.register(conn, ref, "gate-decision/1")
                gid = record_gate_result(conn, pid, self.name, version, "blocked", ref.sha256, self.evaluator)
        except Exception as e:
            self._poisoned.add(rid)
            log.error(
                json.dumps(
                    {"event": "reva.gate", "status": "unrecordable", "request_id": str(rid), "error": type(e).__name__}
                )
            )
            return None
        self._attempts.pop(rid, None)
        log.warning(
            json.dumps(
                {
                    "event": "reva.gate",
                    "status": "decided",
                    "request_id": str(rid),
                    "proposal_id": str(pid),
                    "outcome": "blocked",
                    "recorded": True,
                    "gate_result_id": str(gid),
                    "reason": reasons[0][:300],
                }
            )
        )
        return gid

    def _gate_policy_version(self) -> str:
        from reva.autonomy.policy import PINNED_POLICY_DIGESTS, POLICY_DIR, load_pinned

        return load_pinned(
            self.policy_path or POLICY_DIR / "promotion.yaml",
            self.pinned_sha256 or PINNED_POLICY_DIGESTS["promotion.yaml"],
        ).version


# --------------------------------------------------------------------------- entrypoint
def check_gate_login(engine: Engine) -> dict[str, Any]:
    """Fail closed unless this engine is the reva_gate login and can record gate results."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """SELECT session_user::text AS login,
                      has_function_privilege('ops.record_gate_result(uuid,text,text,text,text,text,jsonb)', 'EXECUTE')
                        AS can_record,
                      has_table_privilege('ops.gate_request', 'SELECT') AS can_read_requests,
                      (SELECT version_num FROM public.alembic_version LIMIT 1) AS schema_version"""
                )
            )
            .mappings()
            .one()
        )
    state = dict(row)
    if state["login"] != GATE_LOGIN:
        raise RevaError(f"the gate process must log in as {GATE_LOGIN}, not {state['login']}")
    if not (state["can_record"] and state["can_read_requests"]):
        raise RevaError("the gate login lacks its grants (bootstrap not finished?)")
    return state


def main(argv: list[str] | None = None) -> int:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.kernel.errors import ConfigurationError
    from reva.settings import load_settings

    ap = argparse.ArgumentParser(prog="reva gate", description="Independent promotion gate process (reva_gate).")
    ap.add_argument("--once", action="store_true", help="evaluate the pending requests once and exit")
    ap.add_argument("--poll-seconds", type=float, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = load_settings()
        settings.validate_for("gate")
    except ConfigurationError as e:
        print(json.dumps({"event": "reva.gate", "status": "error", "error": e.message}))
        return 78
    engine = make_engine("gate", settings)
    try:
        check_gate_login(engine)
    except Exception as e:
        print(json.dumps({"event": "reva.gate", "status": "error", "error": getattr(e, "message", type(e).__name__)}))
        engine.dispose()
        return 75
    service = GateService(engine, build_store(settings), settings, poll_seconds=args.poll_seconds)
    if args.once:
        res = service.run_once()
        print(json.dumps({"event": "reva.gate", "status": "ok", "handled": res}, default=str))
        engine.dispose()
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # not the main thread
            signal.signal(sig, lambda *_: service.stop())
    try:
        service.run()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
