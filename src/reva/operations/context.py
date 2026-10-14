"""Contract between the durable worker and job handlers.

A handler signature is ``handle(payload: dict, ctx: JobContext) -> dict``.
Handlers that publish results must call ``ctx.complete_in(conn, ...)`` inside
the same transaction that writes result references, so a worker that lost its
lease/fencing token cannot commit (ops.complete_job verifies owner, token and
unexpired lease with the database clock and raises otherwise). A handler that
returns normally without calling complete_in lets the worker complete the job
afterwards in a separate fenced transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.ports.storage import ArtifactStore
from reva.settings import Settings


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

    def is_cancelled(self) -> bool:
        return self.heartbeat(None) == "cancel_requested"

    def assert_lease(self, conn: Connection) -> None:
        conn.execute(
            text("SELECT ops.assert_lease(:j, :w, :t)"),
            {"j": self.job_id, "w": self.worker_id, "t": self.fencing_token},
        )

    def complete_in(self, conn: Connection, status: str, result_ref: dict[str, Any] | None,
                    error: dict[str, Any] | None = None) -> None:
        import json

        conn.execute(
            text("SELECT ops.complete_job(:j, :w, :t, :s, CAST(:r AS jsonb), CAST(:e AS jsonb))"),
            {"j": self.job_id, "w": self.worker_id, "t": self.fencing_token, "s": status,
             "r": json.dumps(result_ref, default=str) if result_ref is not None else None,
             "e": json.dumps(error, default=str) if error is not None else None},
        )
        self.completed_in_handler = True
