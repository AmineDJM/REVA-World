"""Migration 0020_v8_boards is in the committed schema contract, and the dev database passes the API's strict
startup check (``_check_schema_contract`` with ``REVA_SCHEMA_CONTRACT=enforce``) as the ``reva_api`` role.

Read-only: the catalog is fingerprinted, nothing is written. Skipped when the dev database is unreachable.
"""

from __future__ import annotations

import json
import re

import pytest

from reva.adapters.postgres.schema_contract import CONTRACT_PATH, MIGRATION_SOURCES

pytestmark = pytest.mark.db


def _latest_revision() -> str:
    heads = {}
    for p in sorted(MIGRATION_SOURCES[0].glob("*.py")):
        m = re.search(r'^revision = "([^"]+)"', p.read_text(encoding="utf-8"), re.M)
        d = re.search(r'^down_revision = "([^"]+)"', p.read_text(encoding="utf-8"), re.M)
        if m:
            heads[m.group(1)] = d.group(1) if d else None
    downs = set(heads.values())
    (head,) = [r for r in heads if r not in downs]
    return head


def test_committed_contract_covers_the_boards_migration() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["alembic_head"] == _latest_revision()
    keys = " ".join(contract["objects"].keys()) if isinstance(contract["objects"], dict) else json.dumps(contract)
    for obj in ("ops.v8_board", "ops.v8_board_create", "ops.v8_board_update", "ops.v8_board_delete"):
        assert obj in keys, obj


def test_dev_db_passes_the_strict_startup_check() -> None:
    from reva.adapters.postgres.engine import make_engine
    from reva.adapters.postgres.schema_contract import assert_schema_contract
    from reva.settings import get_settings

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        pytest.skip("dev database not reachable")
    try:
        diff = assert_schema_contract(eng, mode="enforce", reva_env=s.reva_env)  # raises SchemaDriftError on drift
    finally:
        eng.dispose()
    assert diff is None or diff.ok
