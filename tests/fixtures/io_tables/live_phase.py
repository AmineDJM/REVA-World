"""One phase of the live IO pipeline in its own process, so its peak memory is measured alone.

Usage (driven by tests/integration/test_io_tables_live.py): ``python live_phase.py <phase> '<json args>'``.
The database and object root come from the REVA_* environment of the parent (a throwaway
database). Prints one JSON line: ``{"phase", "seconds", "max_rss_mb", "result"}``.
Phases: ``ingest_figaro`` (live, 27 requests), ``ingest_wdi`` (live), ``dataset``, ``release``,
``simulate``.
"""

from __future__ import annotations

import asyncio
import json
import resource
import sys
import time
from typing import Any


def main(phase: str, args: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import text

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import Settings

    settings = Settings()  # type: ignore[call-arg]
    store = FilesystemArtifactStore(settings.reva_object_root, settings.reva_s3_prefix)
    if phase in ("ingest_figaro", "ingest_wdi"):
        from reva.adapters.connectors import io_tables as it
        from reva.adapters.connectors.registry import builtin_connectors
        from reva.evidence.runner import make_live_client, run_connector

        conn = (
            it.FigaroConnector(int(args["year"]))
            if phase == "ingest_figaro"
            else builtin_connectors()["worldbank.wdi.indicators"]
        )
        client = make_live_client(conn.describe(), settings)
        worker = make_engine("worker", settings)

        async def go() -> Any:
            try:
                return await run_connector(conn, client, store, worker, persist_cursor=False)
            finally:
                await client.aclose()

        rep = asyncio.run(go())
        return {"outcomes": [o.model_dump() for o in rep.outcomes], "errors": rep.errors, "inserted": rep.inserted}
    if phase == "dataset":
        from reva.adapters.connectors import io_tables as it

        out = it.publish_io_dataset(make_engine("worker", settings), store, year=int(args["year"]), origin="live")
        return out
    if phase == "release":
        from reva.belief.publication import publish_release
        from reva.kernel.context import Origin
        from reva.kernel.time import utc_now

        rel = publish_release(
            make_engine("worker", settings), store, cutoff=utc_now(), origin=Origin.live, environment="test"
        )
        return {"release_id": str(rel.release_id), "knowledge_cutoff": rel.knowledge_cutoff.isoformat()}
    if phase == "simulate":
        from reva.kernel.time import utc_now
        from reva.operations.context import JobContext
        from reva.simulation.jobs import handle_simulation

        owner = make_engine("migration", settings)
        payload = {"scenario_id": args["scenario_id"], "fidelity": args["fidelity"]}
        with owner.begin() as c:
            jid = c.execute(
                text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
                {"p": json.dumps(payload), "k": f"io-live-{args['scenario_id']}-{utc_now().timestamp()}"},
            ).scalar_one()
            token = c.execute(
                text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                                      lease_until=now()+interval '30 minutes',
                                      fencing_token=nextval('ops.fencing_seq') WHERE id=:j RETURNING fencing_token"""),
                {"j": jid},
            ).scalar_one()
        ctx = JobContext(
            job_id=jid,
            job_type="simulation",
            worker_id="t",
            fencing_token=int(token),
            attempt=1,
            settings=settings,
            worker_engine=make_engine("worker", settings),
            sim_engine=make_engine("sim", settings),
            store=store,
            heartbeat=lambda _p=None: "running",
        )
        return handle_simulation(payload, ctx)
    raise SystemExit(f"unknown phase {phase}")


if __name__ == "__main__":
    t0 = time.perf_counter()
    res = main(sys.argv[1], json.loads(sys.argv[2]))
    print(
        json.dumps(
            {
                "phase": sys.argv[1],
                "seconds": round(time.perf_counter() - t0, 2),
                "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                "result": res,
            },
            default=str,
        )
    )
