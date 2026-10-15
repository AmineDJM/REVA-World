"""Test-only launcher for a *real* worker process with deterministic fault-injection points.

Runs the production entrypoint ``reva.operations.worker.main()`` unchanged. Before
that it (1) registers a test-only ``chaos_slow`` job type in ``jobs.HANDLERS`` and
(2) optionally wraps two library functions with a delay, because the code base has
no settings/env delay hook of its own (see docs/dev/chaos_check.md):

* ``CHAOS_PLAN_DELAY=<s>``    sleep after ``reva.simulation.jobs.plan_run`` (compute phase,
                              before the cancellation check and the fenced publication);
* ``CHAOS_PUBLISH_DELAY=<s>`` sleep inside ``publish_release``'s open transaction, after the
                              release row has been inserted and before it is sealed/committed.

Every injection point appends a JSON line to ``$CHAOS_DIR/events-<pid>.jsonl`` so the
test knows exactly where the process is before it delivers a fault.
Nothing here is imported by ``src/``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def emit(event: str, **fields: Any) -> None:
    d = os.environ.get("CHAOS_DIR")
    if not d:
        return
    line = json.dumps({"event": event, "pid": os.getpid(), "t": time.time(), **fields}, default=str)
    with open(Path(d) / f"events-{os.getpid()}.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def chaos_slow(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """A deterministic slow job.

    payload: ``steps`` x ``step_seconds``; ``mode``:
      * ``cooperative`` - checks ``ctx.is_cancelled()`` every step (heartbeats through the
        worker) and, on a shutdown, returns ``interrupted`` with a checkpoint;
      * ``stubborn``    - ignores every stop signal for ``stubborn_attempts`` attempts.
    Resumes from ``ctx.checkpoint['step']`` when present.
    """
    from reva.operations.context import JobCancelled

    steps = int(payload.get("steps", 50))
    step_s = float(payload.get("step_seconds", 0.1))
    mode = payload.get("mode", "cooperative")
    start = int((ctx.checkpoint or {}).get("step", 0))
    emit("start", job=str(ctx.job_id), token=ctx.fencing_token, attempt=ctx.attempt, worker=ctx.worker_id,
         resume_from=start)
    if mode == "stubborn" and ctx.attempt <= int(payload.get("stubborn_attempts", 1)):
        time.sleep(float(payload.get("stubborn_seconds", 30)))
        emit("finish", job=str(ctx.job_id), token=ctx.fencing_token, attempt=ctx.attempt)
        return {"status": "finished", "token": ctx.fencing_token}
    for i in range(start, steps):
        if ctx.is_cancelled():
            emit("stop", job=str(ctx.job_id), token=ctx.fencing_token, step=i,
                 shutdown=ctx.shutdown_requested())
            if ctx.shutdown_requested() and not ctx.cancel_requested():
                return {"status": "interrupted", "checkpoint": {"step": i}}
            raise JobCancelled("user cancel honoured at a safe point")
        time.sleep(step_s)
        emit("step", job=str(ctx.job_id), token=ctx.fencing_token, step=i)
    emit("finish", job=str(ctx.job_id), token=ctx.fencing_token, attempt=ctx.attempt)
    return {"status": "done", "steps": steps, "token": ctx.fencing_token, "attempt": ctx.attempt}


def _install() -> None:
    from reva.operations import jobs

    jobs.HANDLERS["chaos_slow"] = "__main__:chaos_slow"

    plan_delay = float(os.environ.get("CHAOS_PLAN_DELAY") or 0)
    if plan_delay > 0:
        import reva.simulation.jobs as sj

        real_plan = sj.plan_run

        def slow_plan(*a: Any, **k: Any) -> Any:
            plan = real_plan(*a, **k)
            emit("plan_sleep_start", seconds=plan_delay)
            time.sleep(plan_delay)
            emit("plan_sleep_end")
            return plan

        sj.plan_run = slow_plan

    publish_delay = float(os.environ.get("CHAOS_PUBLISH_DELAY") or 0)
    if publish_delay > 0:
        import reva.belief.publication as pub

        real_pin = pub.pin_release_relations

        def slow_pin(conn: Any, release_id: Any, activity_id: Any) -> Any:
            n = real_pin(conn, release_id, activity_id)
            emit("publish_in_txn", release_id=str(release_id), seconds=publish_delay)
            time.sleep(publish_delay)
            emit("publish_resume", release_id=str(release_id))
            return n

        pub.pin_release_relations = slow_pin


if __name__ == "__main__":
    _install()
    from reva.operations.worker import main

    emit("boot")
    raise SystemExit(main())
