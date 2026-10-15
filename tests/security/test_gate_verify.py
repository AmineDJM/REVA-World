"""GATE2: ``reva verify`` checks that the gate login connects and holds exactly its privileges.

* the provisioned ``reva_gate`` login matches the expected least-privilege set exactly;
* another login, an extra table grant or an extra SECURITY DEFINER grant is reported;
* without ``REVA_GATE_DB_PASSWORD`` the check is skipped with a note (a failure in production);
* a login that cannot connect is reported without echoing the URL or password;
* the ``reva verify`` command carries the result and fails on a mismatch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from reva.adapters.postgres.engine import make_engine

pytestmark = pytest.mark.db


@pytest.fixture()
def gate_db(db: Any) -> Any:
    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    return db


def _owner_exec(db: Any, sql: str) -> None:
    with db.owner.begin() as c:
        c.execute(text(sql))


def test_gate_login_has_exactly_its_expected_privileges(gate_db: Any) -> None:
    from reva.gate.verify import verify_gate

    out = verify_gate(gate_db.settings, engine_factory=lambda: make_engine("gate", gate_db.settings))
    assert out["ok"] is True and out["status"] == "ok" and out["problems"] == [], out
    assert out["login"] == "reva_gate"


def test_other_logins_and_extra_grants_are_reported(gate_db: Any) -> None:
    from reva.gate.verify import gate_privilege_report

    worker = gate_privilege_report(gate_db.worker)
    assert worker["ok"] is False and any("not reva_gate" in p for p in worker["problems"])
    assert any("unexpected" in p for p in worker["problems"])

    grants = ("GRANT SELECT ON ops.job TO reva_gate",
              "GRANT EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint) TO reva_gate",
              "GRANT INSERT ON ops.gate_result TO reva_gate")
    try:
        for g in grants:
            _owner_exec(gate_db, g)
        rep = gate_privilege_report(gate_db.gate)
    finally:
        _owner_exec(gate_db, "REVOKE SELECT ON ops.job FROM reva_gate")
        _owner_exec(gate_db, "REVOKE EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint) FROM reva_gate")
        _owner_exec(gate_db, "REVOKE INSERT ON ops.gate_result FROM reva_gate")
    assert rep["ok"] is False
    assert "ops.job: unexpected SELECT" in rep["problems"]
    assert "ops.gate_result: unexpected INSERT" in rep["problems"]
    assert any(p.startswith("function ops.assert_lease(") and "unexpected EXECUTE" in p for p in rep["problems"])

    try:  # a missing grant is reported too
        _owner_exec(gate_db, "REVOKE SELECT ON ops.gate_request FROM reva_gate")
        rep = gate_privilege_report(gate_db.gate)
    finally:
        _owner_exec(gate_db, "GRANT SELECT ON ops.gate_request TO reva_gate")
    assert rep["ok"] is False and "ops.gate_request: missing SELECT" in rep["problems"]
    assert gate_privilege_report(gate_db.gate)["ok"] is True


def test_gate_check_is_skipped_without_a_password_and_required_in_production(db: Any) -> None:
    from reva.gate.verify import verify_gate

    local = verify_gate(db.settings.model_copy(update={"reva_gate_db_password": None}))
    assert local["ok"] is True and local["status"] == "skipped" and "REVA_GATE_DB_PASSWORD" in local["note"]
    prod = verify_gate(db.settings.model_copy(update={"reva_gate_db_password": None, "reva_env": "production"}))
    assert prod["ok"] is False and prod["status"] == "missing"


def test_unreachable_gate_login_is_reported_without_secrets(gate_db: Any) -> None:
    from reva.gate.verify import verify_gate

    # a closed port (local pg_hba may trust loopback, so a wrong password alone could still connect)
    url = gate_db.settings.runtime_url("gate").set(password="definitely-wrong-password", port=1)
    out = verify_gate(gate_db.settings, engine_factory=lambda: create_engine(url, connect_args={"connect_timeout": 3}))
    assert out["ok"] is False and out["status"] == "unreachable"
    blob = json.dumps(out)
    assert "definitely-wrong-password" not in blob
    assert gate_db.settings.reva_gate_db_password.get_secret_value() not in blob


def test_reva_verify_reports_the_gate_login(gate_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI command, pointed at the test database (never the dev database)."""
    import reva.adapters.objects.factory as factory
    import reva.adapters.postgres.engine as engine_mod
    import reva.settings as settings_mod
    from reva.cli.main import app

    engines = {"api": gate_db.api, "worker": gate_db.worker, "sim": gate_db.sim}
    monkeypatch.setattr(settings_mod, "load_settings", lambda *a, **k: gate_db.settings)
    monkeypatch.setattr(engine_mod, "get_engine", lambda kind: engines[kind])
    monkeypatch.setattr(factory, "get_store", lambda *a, **k: gate_db.store)

    res = CliRunner().invoke(app, ["verify"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["ok"] is True and out["db_gate"]["status"] == "ok" and out["db_gate"]["login"] == "reva_gate"

    try:  # an extra grant fails `reva verify`
        _owner_exec(gate_db, "GRANT SELECT ON ops.job TO reva_gate")
        res = CliRunner().invoke(app, ["verify"])
    finally:
        _owner_exec(gate_db, "REVOKE SELECT ON ops.job FROM reva_gate")
    assert res.exit_code == 1
    out = json.loads(res.output)
    assert out["ok"] is False and out["db_gate"]["status"] == "privilege_mismatch"
    assert "ops.job: unexpected SELECT" in out["db_gate"]["problems"]

    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda *a, **k: gate_db.settings.model_copy(update={"reva_gate_db_password": None}))
    res = CliRunner().invoke(app, ["verify"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["db_gate"]["status"] == "skipped"
