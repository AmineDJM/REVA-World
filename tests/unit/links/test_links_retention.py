"""Link store retention (agent LINKRET): static checks of migration 0017_links_retention, the plan arithmetic,
the ``reva links prune`` CLI surface, and a guard that any new table referencing ``kg.link_materialisation``
is seen by the retention policy. Database behaviour: ``tests/integration/test_links_retention_db.py``."""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "versions" / "0017_links_retention.py"


def _migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("m0017", MIGRATION)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migration_chain_and_scope() -> None:
    m = _migration()
    assert (m.revision, m.down_revision) == ("0017_links_retention", "0016_causal_ops")
    sql = m.SQL_TABLE + m.SQL_FUNCTIONS + m.SQL_LINK_ROW_TRIGGERS + m.SQL_GRANTS
    # link_row: UPDATE refused unconditionally; DELETE gated on a prune record of the same transaction
    assert "BEFORE UPDATE ON kg.link_row\n  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation()" in sql
    assert "WHEN (NOT kg.link_prune_in_xact(OLD.materialisation_id)) EXECUTE FUNCTION meta.reject_mutation()" in sql
    assert "meta.inserted_in_current_xact(p.xmin)" in sql
    # the prune function and the guards are owner-only: revoked from PUBLIC, granted to no runtime role
    assert "kg.prune_link_materialisation(bigint, uuid, text, jsonb) FROM PUBLIC" in sql
    assert not re.search(r"GRANT EXECUTE ON FUNCTION kg\.prune_link_materialisation", sql)
    assert sql.count("meta.session_principal() <> 'owner'") == 2
    assert not re.search(r"GRANT (INSERT|UPDATE|DELETE)[^;]*kg\.link_prune TO reva_(api|worker|sim)", sql)
    # the served materialisations are refused by the database itself
    assert "latest of its lineage" in sql and "latest live materialisation" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql and "link rows remain" in sql
    # no change to the link store's other protections (TRUNCATE, materialisation rows)
    assert "link_materialisation" not in m.SQL_LINK_ROW_TRIGGERS
    with pytest.raises(NotImplementedError):
        m.downgrade()


def test_only_known_tables_reference_link_materialisations() -> None:
    """A new foreign key to kg.link_materialisation is honoured automatically by the policy (``fk:`` reasons);
    this guard makes the addition visible: update ``reva.links.retention``'s docstring when it fires."""
    refs = {
        p.name
        for p in (ROOT / "migrations" / "versions").glob("*.py")
        if re.search(r"REFERENCES\s+kg\.link_materialisation", p.read_text())
    }
    assert refs == {"0012_links_store.py", "0017_links_retention.py"}


def test_plan_estimates_and_policy() -> None:
    from reva.links.retention import Materialisation, PrunePlan

    now = datetime(2026, 10, 17, tzinfo=UTC)
    a = Materialisation(1, "a" * 64, "live", "live", 3, 600, now, False)
    b = Materialisation(2, "b" * 64, "live", "live", 4, 400, now, False)
    c = Materialisation(3, None, None, "live", 5, 1000, now, False)
    plan = PrunePlan(2, [a, b, c], {3: ["latest", "recent"], 2: ["recent"]}, [a], [], 2_000_000, 2000)
    assert plan.estimated_bytes(a) == 600_000
    assert c.live_served and a.live_served
    assert not Materialisation(4, "c" * 64, "test_fixture", "live", 1, 1, now, False).live_served
    j = plan.to_json()
    assert j["prune_rows"] == 600 and j["prune_estimated_bytes"] == 600_000
    assert [k["id"] for k in j["kept"]] == [2, 3] and j["kept"][1]["reasons"] == ["latest", "recent"]
    assert plan.policy() == {"keep": 2, "kept": {"2": ["recent"], "3": ["latest", "recent"]}}
    assert PrunePlan(2, [], {}, [], [], 0, 0).estimated_bytes(a) == 0


def test_keep_is_validated_before_reading() -> None:
    from reva.links.retention import RetentionError, plan_prune

    for bad in (0, -3, True, 1.5):
        with pytest.raises(RetentionError):
            plan_prune(None, keep=bad)  # type: ignore[arg-type]


def test_cli_prune_surface() -> None:
    from typer.testing import CliRunner

    from reva.cli.main import app

    res = CliRunner().invoke(app, ["links", "--help"])
    assert res.exit_code == 0 and "prune" in res.output and "materialise" in res.output
    res = CliRunner().invoke(app, ["links", "prune", "--help"])
    out = re.sub(r"\x1b\[[0-9;]*m", "", res.output)
    assert res.exit_code == 0
    for opt in ("--keep", "--dry-run", "--vacuum", "--reason"):
        assert opt in out
