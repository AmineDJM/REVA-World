"""Link store operations (agent LINKWIRE): the ``links_materialise`` job kind, its schedule, the API submission
allow-list (migration 0013_links_ops) and the ``reva links materialise`` CLI alias."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text


def test_links_job_kind_is_registered_scheduled_and_validated() -> None:
    from reva.cli.seed import ATLAS_FIRST_SLOT_HOURS, DEFAULT_SCHEDULES
    from reva.kernel.errors import ValidationProblem
    from reva.links.jobs import handle_links_materialise, parse_payload
    from reva.operations.jobs import HANDLERS, SIM_ENGINE_JOB_TYPES, resolve_handler
    from reva.operations.schedules import SCHEDULE_JOB_TYPES

    assert HANDLERS["links_materialise"] == "reva.links.jobs:handle_links_materialise"
    assert resolve_handler("links_materialise") is handle_links_materialise
    assert "links_materialise" in SCHEDULE_JOB_TYPES and "links_materialise" not in SIM_ENGINE_JOB_TYPES
    sched = {k: (t, p, secs) for k, t, p, secs in DEFAULT_SCHEDULES}
    jt, params, secs = sched["links_materialise:live"]
    assert jt == "links_materialise" and secs == 86400
    assert params["atlas_origin"] == "live" and params["kg_origin"] == "live"  # production serves live only
    # daily, after the atlas snapshot slot
    assert ATLAS_FIRST_SLOT_HOURS["links_materialise:live"] > ATLAS_FIRST_SLOT_HOURS["atlas_snapshot:live"]
    assert parse_payload(params)["skip_unchanged"] is True
    for bad in ({"top_k": 0}, {"top_k": True}, {"atlas_origin": "prod"}, {"atlas_snapshot": "x"}, {"shell": 1}):
        with pytest.raises(ValidationProblem):
            parse_payload(bad)


def test_cli_links_group_has_only_materialise() -> None:
    from typer.testing import CliRunner

    from reva.cli.main import app

    res = CliRunner().invoke(app, ["links", "--help"])
    assert res.exit_code == 0 and "materialise" in res.output
    res = CliRunner().invoke(app, ["links", "materialise", "--help"])
    assert res.exit_code == 0 and "--atlas-origin" in res.output and "--top-k" in res.output


@pytest.mark.db
def test_submit_job_accepts_links_materialise(db: Any) -> None:
    with db.api.begin() as c:
        jid = c.execute(
            text("SELECT ops.submit_job('links_materialise', CAST(:p AS jsonb), :k, 0, 'api:linkwire')"),
            {"p": json.dumps({"atlas_origin": "test_fixture"}), "k": f"linkwire-{uuid.uuid4().hex}"},
        ).scalar_one()
    with db.owner.connect() as c:
        row = c.execute(text("SELECT job_type, status FROM ops.job WHERE id = :i"), {"i": jid}).one()
        c.execute(text("UPDATE ops.job SET status = 'cancelled' WHERE id = :i"), {"i": jid})
        c.commit()
    assert tuple(row) == ("links_materialise", "queued")
    with pytest.raises(Exception, match="cannot be submitted"), db.api.begin() as c:
        c.execute(text("SELECT ops.submit_job('links_shell', '{}'::jsonb, :k, 0, 'api')"), {"k": f"x-{uuid.uuid4()}"})


@pytest.mark.db
def test_handler_materialises_then_reuses_unchanged_inputs(db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from reva.adapters.connectors import atlas_whr
    from reva.atlas.ingest import handle_atlas_ingest
    from reva.atlas.snapshot import handle_atlas_snapshot
    from reva.atlas.sources import ensure_registered
    from reva.links.fixture import FIXTURE_FILE, load_into_kg
    from reva.links.jobs import handle_links_materialise

    monkeypatch.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path))
    ensure_registered(db.owner, db.store, atlas_whr.atlas_source())
    ctx = SimpleNamespace(
        settings=db.settings,
        worker_engine=db.worker,
        store=db.store,
        job_id=uuid.uuid4(),
        attempt=1,
        cancel_requested=lambda: False,
    )
    assert handle_atlas_ingest({"source": "atlas.whr", "mode": "fixture"}, ctx)["status"] == "completed"
    snap = handle_atlas_snapshot({"origin": "test_fixture", "skip_unchanged": False}, ctx)
    if FIXTURE_FILE.exists():
        load_into_kg(db.worker, db.store)
    payload = {"atlas_origin": "test_fixture", "kg_origin": "test_fixture", "top_k": 3}
    first = handle_links_materialise(payload, ctx)
    assert first["reused"] is False and first["materialisation_id"] > 0
    assert first["inputs"]["atlas_snapshot_sha256"] == snap["snapshot_sha256"]
    again = handle_links_materialise(payload, ctx)  # jobs default to skip_unchanged
    assert again == {"reused": True, "materialisation_id": first["materialisation_id"], "inputs": first["inputs"]}
    forced = handle_links_materialise({**payload, "skip_unchanged": False}, ctx)
    assert forced["reused"] is False and forced["materialisation_id"] > first["materialisation_id"]
    other_k = handle_links_materialise({**payload, "top_k": 4}, ctx)  # different inputs: a new materialisation
    assert other_k["reused"] is False
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.link_materialisation")).scalar_one()
    assert n == 3
