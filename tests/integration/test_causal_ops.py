"""Causal store operations (agent CAUSOPS): the ``causal_refresh`` job kind, its daily schedule after the link store,
the API submission allow-list (migration 0016_causal_ops) and the ``reva causal refresh`` CLI."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text


def test_causal_job_kind_is_registered_scheduled_and_validated() -> None:
    from reva.cli.seed import ATLAS_FIRST_SLOT_HOURS, DEFAULT_SCHEDULES
    from reva.kernel.errors import ValidationProblem
    from reva.operations.jobs import (
        HANDLERS,
        SIM_ENGINE_JOB_TYPES,
        handle_causal_refresh,
        parse_causal_refresh_payload,
        resolve_handler,
    )
    from reva.operations.schedules import SCHEDULE_JOB_TYPES

    assert HANDLERS["causal_refresh"] == "reva.operations.jobs:handle_causal_refresh"
    assert resolve_handler("causal_refresh") is handle_causal_refresh
    assert "causal_refresh" in SCHEDULE_JOB_TYPES and "causal_refresh" not in SIM_ENGINE_JOB_TYPES
    sched = {k: (t, p, secs) for k, t, p, secs in DEFAULT_SCHEDULES}
    jt, params, secs = sched["causal_refresh:live"]
    assert (jt, params, secs) == ("causal_refresh", {"origin": "live"}, 86400)  # production serves live claims
    # daily, after the link store slot (itself after the atlas snapshot)
    assert (
        ATLAS_FIRST_SLOT_HOURS["causal_refresh:live"]
        > ATLAS_FIRST_SLOT_HOURS["links_materialise:live"]
        > ATLAS_FIRST_SLOT_HOURS["atlas_snapshot:live"]
    )
    assert parse_causal_refresh_payload({}) == {"origin": "live"}
    assert parse_causal_refresh_payload({"origin": None}) == {"origin": None}
    assert parse_causal_refresh_payload({"origin": "test_fixture"}) == {"origin": "test_fixture"}
    for bad in ({"origin": "prod"}, {"origin": 1}, {"shell": "x"}, {"origin": "live", "source_keys": ["x"]}):
        with pytest.raises(ValidationProblem):
            parse_causal_refresh_payload(bad)


def test_cli_causal_group_has_refresh() -> None:
    from typer.testing import CliRunner

    from reva.cli.main import app

    res = CliRunner().invoke(app, ["causal", "--help"])
    assert res.exit_code == 0 and "refresh" in res.output
    res = CliRunner().invoke(app, ["causal", "refresh", "--help"])
    assert res.exit_code == 0 and "--origin" in res.output
    res = CliRunner().invoke(app, ["causal", "refresh", "--origin", "prod"])  # refused before any connection
    assert res.exit_code == 2


def test_migration_0016_only_extends_the_allow_list() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0016_causal_ops.py"
    spec = importlib.util.spec_from_file_location("m0016", path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("0016_causal_ops", "0015_causal")
    prev = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0013_links_ops.py"
    spec13 = importlib.util.spec_from_file_location("m0013", prev)
    assert spec13 is not None and spec13.loader is not None
    m13 = importlib.util.module_from_spec(spec13)
    spec13.loader.exec_module(m13)
    assert m.SUBMIT_JOB_LINKS == m13.SUBMIT_JOB_LINKS  # downgrade restores 0013 exactly
    assert m.SUBMIT_JOB_CAUSAL == m13.SUBMIT_JOB_LINKS.replace(
        "'links_materialise') THEN", "'links_materialise','causal_refresh') THEN"
    )


@pytest.mark.db
def test_submit_job_accepts_causal_refresh(db: Any) -> None:
    with db.api.begin() as c:
        jid = c.execute(
            text("SELECT ops.submit_job('causal_refresh', CAST(:p AS jsonb), :k, 0, 'api:causops')"),
            {"p": json.dumps({"origin": "test_fixture"}), "k": f"causops-{uuid.uuid4().hex}"},
        ).scalar_one()
    with db.owner.connect() as c:
        row = c.execute(text("SELECT job_type, status FROM ops.job WHERE id = :i"), {"i": jid}).one()
        c.execute(text("UPDATE ops.job SET status = 'cancelled' WHERE id = :i"), {"i": jid})
        c.commit()
    assert tuple(row) == ("causal_refresh", "queued")
    with pytest.raises(Exception, match="cannot be submitted"), db.api.begin() as c:
        c.execute(text("SELECT ops.submit_job('causal_shell', '{}'::jsonb, :k, 0, 'api')"), {"k": f"x-{uuid.uuid4()}"})


@pytest.mark.db
def test_handler_loads_grades_then_writes_nothing_when_unchanged(db: Any) -> None:
    from reva.causal.fixture import FIXTURE_FILE, load_into_kg
    from reva.operations.jobs import handle_causal_refresh

    if not FIXTURE_FILE.exists():
        pytest.skip("causal fixture not captured")
    load_into_kg(db.worker, db.store)  # idempotent KG load of the real Wikidata causal fixture (test_fixture)
    ctx = SimpleNamespace(settings=db.settings, worker_engine=db.worker, store=db.store, job_id=uuid.uuid4())
    first = handle_causal_refresh({"origin": "test_fixture"}, ctx)
    assert first["status"] == "completed" and first["origin"] == "test_fixture"
    assert first["loaded"]["scanned"] >= 37  # the fixture's causal statements (other tests may have loaded more)
    with db.owner.connect() as c:
        claims = c.execute(
            text("SELECT count(*) FROM kg.causal_claim_current WHERE origin = 'test_fixture' AND claim_key LIKE 'kg:%'")
        ).scalar_one()
        graded = c.execute(
            text(
                """SELECT count(*) FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                    WHERE c.origin = 'test_fixture'"""
            )
        ).scalar_one()
        loads = c.execute(text("SELECT count(*) FROM kg.causal_load")).scalar_one()
    assert claims >= 37 and graded >= claims
    again = handle_causal_refresh({"origin": "test_fixture"}, ctx)  # unchanged KG: no claim, grade or load row
    assert again["loaded"]["inserted"] == 0 and again["graded"]["written"] == 0
    with db.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kg.causal_load")).scalar_one() == loads
