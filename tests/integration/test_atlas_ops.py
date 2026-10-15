"""Atlas operations (agent ATLASOPS): job kinds, schedules, API submission allow-list, CLI.

* ``atlas_ingest`` / ``atlas_snapshot`` are worker job kinds (``HANDLERS``), schedulable, and accepted
  by ``ops.submit_job`` (migration 0010_atlas_ops); other types are still refused.
* The seeded atlas schedules name discovered atlas sources whose planned requests fit their
  per-source budget, with staggered first slots (no two large runs on the same day).
* The job handlers run the metered entry point (fixture mode here) and record / reuse snapshots.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_whr
from reva.atlas.sources import atlas_sources, ensure_registered


def _schedules() -> dict[str, tuple[str, dict[str, Any], int]]:
    from reva.cli.seed import DEFAULT_SCHEDULES

    return {k: (t, p, secs) for k, t, p, secs in DEFAULT_SCHEDULES if t.startswith("atlas_")}


def test_atlas_job_kinds_are_registered_and_schedulable() -> None:
    from reva.atlas.ingest import handle_atlas_ingest
    from reva.atlas.snapshot import handle_atlas_snapshot
    from reva.operations.jobs import HANDLERS, SIM_ENGINE_JOB_TYPES, resolve_handler
    from reva.operations.schedules import SCHEDULE_JOB_TYPES, schedule_payload

    assert HANDLERS["atlas_ingest"] == "reva.atlas.ingest:handle_atlas_ingest"
    assert HANDLERS["atlas_snapshot"] == "reva.atlas.snapshot:handle_atlas_snapshot"
    assert resolve_handler("atlas_ingest") is handle_atlas_ingest
    assert resolve_handler("atlas_snapshot") is handle_atlas_snapshot
    assert {"atlas_ingest", "atlas_snapshot"} <= SCHEDULE_JOB_TYPES
    assert not {"atlas_ingest", "atlas_snapshot"} & SIM_ENGINE_JOB_TYPES  # worker credentials only
    assert schedule_payload("atlas_ingest", {"source": "atlas.imf"}) == {"source": "atlas.imf", "mode": "live"}


def test_atlas_schedules_fit_the_budgets_and_are_staggered() -> None:
    from reva.cli.seed import ATLAS_FIRST_SLOT_HOURS
    from reva.operations.budgets import load_budget

    budget = load_budget()
    found = atlas_sources()
    sched = _schedules()
    assert sched["atlas_snapshot:live"] == (
        "atlas_snapshot",
        {"origin": "live", "warm": True, "skip_unchanged": True},
        86400,
    )
    per_day: dict[int, int] = {}
    for key, (jt, params, secs) in sched.items():
        assert key in ATLAS_FIRST_SLOT_HOURS, key
        if jt != "atlas_ingest":
            continue
        spec = found[params["source"]].spec  # every scheduled source is discovered
        assert key == f"atlas_ingest:{spec.name}" and secs >= 7 * 86400
        limit = budget.per_source_requests[spec.budget_key]
        assert spec.planned_requests <= limit, (spec.name, spec.planned_requests, limit)
        day = ATLAS_FIRST_SLOT_HOURS[key] // 24
        per_day[day] = per_day.get(day, 0) + spec.planned_requests
    assert max(per_day.values()) <= budget.total_requests
    # the two largest downloads (WID, IMF) never share a first day
    assert (
        ATLAS_FIRST_SLOT_HOURS["atlas_ingest:atlas.wid"] // 24 != ATLAS_FIRST_SLOT_HOURS["atlas_ingest:atlas.imf"] // 24
    )
    assert ATLAS_FIRST_SLOT_HOURS["atlas_snapshot:live"] > ATLAS_FIRST_SLOT_HOURS["atlas_ingest:atlas.imf"]


@pytest.mark.db
def test_submit_job_accepts_atlas_kinds_only(db: Any) -> None:
    for jt, payload in (
        ("atlas_ingest", {"source": "atlas.whr", "mode": "fixture"}),
        ("atlas_snapshot", {"origin": "test_fixture"}),
    ):
        with db.api.begin() as c:
            jid = c.execute(
                text("SELECT ops.submit_job(:t, CAST(:p AS jsonb), :k, 0, 'api:atlasops')"),
                {"t": jt, "p": json.dumps(payload), "k": f"atlasops-{uuid.uuid4().hex}"},
            ).scalar_one()
        with db.owner.connect() as c:
            row = c.execute(text("SELECT job_type, status FROM ops.job WHERE id = :i"), {"i": jid}).one()
            c.execute(text("UPDATE ops.job SET status = 'cancelled' WHERE id = :i"), {"i": jid})
            c.commit()
        assert tuple(row) == (jt, "queued")
    with pytest.raises(Exception, match="cannot be submitted"), db.api.begin() as c:
        c.execute(text("SELECT ops.submit_job('atlas_shell', '{}'::jsonb, :k, 0, 'api')"), {"k": f"x-{uuid.uuid4()}"})


@pytest.mark.db
def test_handlers_run_fixture_ingest_and_reuse_unchanged_snapshots(db: Any) -> None:
    from reva.atlas.ingest import handle_atlas_ingest
    from reva.atlas.snapshot import handle_atlas_snapshot

    ensure_registered(db.owner, db.store, atlas_whr.atlas_source())
    ctx = SimpleNamespace(
        settings=db.settings,
        worker_engine=db.worker,
        store=db.store,
        job_id=uuid.uuid4(),
        attempt=1,
        cancel_requested=lambda: False,
    )
    out = handle_atlas_ingest({"source": "atlas.whr", "mode": "fixture"}, ctx)
    assert out["status"] == "completed" and out["registered"][0]["dataset_key"] == "atlas.whr.whr26_fig21"
    first = handle_atlas_snapshot({"origin": "test_fixture", "skip_unchanged": False}, ctx)
    again = handle_atlas_snapshot({"origin": "test_fixture"}, ctx)  # jobs default to skip_unchanged
    assert first["reused"] is False and again["reused"] is True
    assert again["snapshot_sha256"] == first["snapshot_sha256"]
    with pytest.raises(Exception, match="origin"):
        handle_atlas_snapshot({"origin": "nope"}, ctx)


def test_cli_atlas_group_lists_sources() -> None:
    from typer.testing import CliRunner

    from reva.cli.main import app

    res = CliRunner().invoke(app, ["atlas", "list"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert {"atlas.imf", "atlas.undp_hdr", "atlas.whr", "atlas.owid", "atlas.wid"} <= set(doc["sources"])
    assert doc["sources"]["atlas.wid"]["budget_key"] == "wid.bulk"
    res = CliRunner().invoke(app, ["atlas", "--help"])
    assert all(c in res.output for c in ("ingest", "snapshot", "list"))
