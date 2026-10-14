"""Contract between the durable worker and job handlers.

A handler signature is ``handle(payload: dict, ctx: JobContext) -> dict``.
Handlers that publish results must call ``ctx.complete_in(conn, ...)`` inside
the same transaction that writes result references, so a worker that lost its
lease/fencing token cannot commit (ops.complete_job verifies owner, token and
unexpired lease with the database clock and raises otherwise). A handler that
returns normally without calling complete_in lets the worker complete the job
afterwards in a separate fenced transaction.

Stop signals (R2-01)
--------------------
Two different things can ask a handler to stop, and they must not be confused:

* ``ctx.shutdown_requested()`` — the *worker* is shutting down (SIGTERM on a
  deploy). This is not a cancellation: the job must be handed back to the queue
  (raise :class:`reva.operations.jobs.JobCancelled`, return
  ``{"status": "cancelled"}`` or ``{"status": "interrupted", "checkpoint": ...}``)
  and the worker releases it without consuming an attempt.
* ``ctx.cancel_requested()`` — a *user* asked to cancel (DB status
  ``cancel_requested``). Only this may finish the job as ``cancelled``.

``ctx.is_cancelled()`` stays the generic "stop at the next safe point" check and
is true for either reason. Whatever a handler does, the worker decides the final
state: during shutdown it releases the job unless the database says the user
cancelled it. ``complete_in(conn, "cancelled", ...)`` during a shutdown without a
user cancel raises ``JobCancelled`` instead of terminally cancelling the job.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.ports.storage import ArtifactStore
from reva.settings import Settings


def _never() -> bool:
    return False


@dataclass
class JobContext:
    job_id: UUID
    job_type: str
    worker_id: str
    fencing_token: int
    attempt: int
    settings: Settings
    worker_engine: Engine
    sim_engine: Engine | None
    store: ArtifactStore
    heartbeat: Callable[[dict[str, Any] | None], str] = field(default=lambda p=None: "running")
    checkpoint: dict[str, Any] | None = None
    completed_in_handler: bool = False
    # Set by the worker: true once the worker process is shutting down (never a user cancel).
    shutdown_signal: Callable[[], bool] = field(default=_never)
    # Set by the worker: true only when the job row's DB status is ``cancel_requested``.
    # ``None`` falls back to the heartbeat status when no shutdown is in progress.
    cancel_probe: Callable[[], bool] | None = None

    def shutdown_requested(self) -> bool:
        """The worker is shutting down: release/checkpoint, never finish as cancelled."""
        return bool(self.shutdown_signal())

    def cancel_requested(self) -> bool:
        """A user cancellation is pending for this job (DB status ``cancel_requested``)."""
        if self.cancel_probe is not None:
            return bool(self.cancel_probe())
        if self.shutdown_requested():
            return False  # without a probe a shutdown is indistinguishable: never assume a user cancel
        return self.heartbeat(None) == "cancel_requested"

    def is_cancelled(self) -> bool:
        """Generic stop signal: a user cancel *or* a worker shutdown (see module docstring)."""
        return self.shutdown_requested() or self.heartbeat(None) == "cancel_requested"

    def assert_lease(self, conn: Connection) -> None:
        """Lock the job row and verify owner/token/lease (held until ``conn`` commits)."""
        conn.execute(
            text("SELECT ops.assert_lease(:j, :w, :t)"),
            {"j": self.job_id, "w": self.worker_id, "t": self.fencing_token},
        )

    def check_lease(self, conn: Connection) -> None:
        """Verify owner/token/lease WITHOUT locking the job row (R2-14).

        For long transactions (belief publication) that must not block the
        worker's heartbeat, which needs the same row lock to extend the lease.
        Requires SELECT on ops.job (worker role).
        """
        ok = conn.execute(
            text("SELECT 1 FROM ops.job WHERE id = :j AND worker_id = :w AND fencing_token = :t "
                 "AND lease_until > clock_timestamp() AND status IN ('running','cancel_requested')"),
            {"j": self.job_id, "w": self.worker_id, "t": self.fencing_token},
        ).scalar_one_or_none()
        if ok is None:
            from reva.kernel.errors import StaleLease

            raise StaleLease(f"lease lost for job {self.job_id} (fencing token {self.fencing_token})",
                             job_id=str(self.job_id), fencing_token=self.fencing_token)

    def complete_in(self, conn: Connection, status: str, result_ref: dict[str, Any] | None,
                    error: dict[str, Any] | None = None) -> None:
        import json

        if status == "cancelled" and self.shutdown_requested() and not self.cancel_requested():
            from reva.operations.jobs import JobCancelled

            # R2-01: a shutdown is not a cancellation; abort the handler's transaction so
            # the worker releases the job for a retry instead of ending it.
            raise JobCancelled(f"worker shutting down; job {self.job_id} released, not cancelled",
                               shutdown=True)
        conn.execute(
            text("SELECT ops.complete_job(:j, :w, :t, :s, CAST(:r AS jsonb), CAST(:e AS jsonb))"),
            {"j": self.job_id, "w": self.worker_id, "t": self.fencing_token, "s": status,
             "r": json.dumps(result_ref, default=str) if result_ref is not None else None,
             "e": json.dumps(error, default=str) if error is not None else None},
        )
        self.completed_in_handler = True
