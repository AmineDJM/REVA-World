"""Schema contract (R5-04): a database whose catalog differs from what the migrations
build is detected, even when ``alembic_version`` claims the current head.

Uses its own throwaway database (not the shared ``db`` fixture, which other tests
may alter). Mutations run inside a transaction that is rolled back.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from reva.adapters.postgres.engine import make_engine
from reva.adapters.postgres.schema_contract import (
    REGENERATE_HINT,
    Fingerprint,
    SchemaDiff,
    SchemaDriftError,
    assert_schema_contract,
    build_contract,
    compute_fingerprint,
    contract_mode,
    diff_fingerprints,
    load_contract,
    throwaway_database,
)
from reva.kernel.errors import ConfigurationError
from reva.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def fresh() -> Iterator[tuple[Settings, Engine, Fingerprint]]:
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    with throwaway_database(base, prefix="reva_test_contract") as settings:
        owner = create_engine(settings.migration_url())
        with owner.connect() as conn:
            fp = compute_fingerprint(conn)
        yield settings, owner, fp
        owner.dispose()


def _drifted(conn: Connection, baseline: Fingerprint) -> SchemaDiff:
    return diff_fingerprints(baseline.objects, compute_fingerprint(conn).objects)


def test_committed_contract_matches_fresh_migrations(fresh: tuple[Settings, Engine, Fingerprint]) -> None:
    """Fails when migrations/schema_contract.json is stale relative to the migration files."""
    _, _, fp = fresh
    contract = load_contract()
    d = diff_fingerprints(contract["objects"], fp.objects)
    d.contract_head = contract.get("alembic_head")
    assert d.ok, (
        "migrations/schema_contract.json is stale: regenerate it with "
        f"`{REGENERATE_HINT}` (\"missing from database\" = only in the committed contract)\n" + d.summary(40)
    )
    assert contract["fingerprint"] == fp.digest


def test_fingerprint_is_deterministic_and_role_independent(fresh: tuple[Settings, Engine, Fingerprint]) -> None:
    settings, owner, fp = fresh
    with owner.connect() as conn:
        assert compute_fingerprint(conn) == fp
    # API/worker startup verifies as a runtime role: the catalog must look the same to it.
    for kind in ("api", "worker"):
        eng = make_engine(kind, settings)
        try:
            with eng.connect() as conn:
                assert compute_fingerprint(conn).digest == fp.digest
        finally:
            eng.dispose()
    kinds = fp.counts()
    for k in ("schema", "relation", "column", "constraint", "trigger", "function", "policy", "role"):
        assert kinds.get(k, 0) > 0, k
    assert not any(k.startswith("function:public.st_") for k in fp.objects), "extension objects leaked in"


def test_fresh_database_passes_startup_guard(fresh: tuple[Settings, Engine, Fingerprint], tmp_path: Path) -> None:
    settings, _owner, fp = fresh
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(build_contract(fp, "head")))
    api = make_engine("api", settings)
    try:
        d = assert_schema_contract(api, mode="enforce", contract_path=path)
        assert d is not None and d.ok
    finally:
        api.dispose()


def test_function_body_change_is_detected(fresh: tuple[Settings, Engine, Fingerprint], tmp_path: Path) -> None:
    _, owner, fp = fresh
    key = "function:meta.reject_truncate()"
    assert key in fp.objects
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(build_contract(fp, "head")))
    with owner.connect() as conn:
        ddl = conn.execute(text("SELECT pg_get_functiondef('meta.reject_truncate()'::regprocedure)")).scalar_one()
        assert "AS $function$" in ddl
        edited = ddl.replace("AS $function$", "AS $function$\n-- edited in place\n", 1)
        conn.exec_driver_sql(edited.replace("%", "%%"))  # psycopg placeholder escaping
        d = _drifted(conn, fp)
        assert d.changed == [key] and not d.missing and not d.unexpected
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_contract(conn, mode="enforce", contract_path=path)
        assert key in ei.value.message and "definition differs: 1" in ei.value.message
        # warn mode reports but does not refuse
        warned = assert_schema_contract(conn, mode="warn", contract_path=path)
        assert warned is not None and warned.changed == [key]
        conn.rollback()
        assert _drifted(conn, fp).ok


def test_dropped_trigger_is_detected(fresh: tuple[Settings, Engine, Fingerprint]) -> None:
    _, owner, fp = fresh
    key = next(k for k in sorted(fp.objects) if k.startswith("trigger:") and k.endswith(".immutable_record"))
    schema, table, trigger = key.removeprefix("trigger:").split(".")
    with owner.connect() as conn:
        conn.execute(text(f'DROP TRIGGER "{trigger}" ON "{schema}"."{table}"'))
        d = _drifted(conn, fp)
        assert d.missing == [key] and not d.changed and not d.unexpected
        conn.rollback()


def test_grant_policy_and_role_drift_are_detected(fresh: tuple[Settings, Engine, Fingerprint]) -> None:
    _, owner, fp = fresh
    pol = next(k for k in sorted(fp.objects) if k.startswith("policy:"))
    schema, table, name = pol.removeprefix("policy:").split(".")
    with owner.connect() as conn:
        conn.execute(text("GRANT DELETE ON ops.audit TO reva_api"))
        conn.execute(text(f'DROP POLICY "{name}" ON "{schema}"."{table}"'))
        conn.execute(text("CREATE FUNCTION ops.zz_backdoor() RETURNS int LANGUAGE sql AS 'SELECT 1'"))
        d = _drifted(conn, fp)
        assert "relation:ops.audit" in d.changed
        assert pol in d.missing
        assert "function:ops.zz_backdoor()" in d.unexpected
        conn.rollback()


def test_summary_is_bounded() -> None:
    expected = {f"column:t.c{i:03d}": "x" for i in range(200)}
    d = diff_fingerprints(expected, {})
    out = d.summary(limit=5)
    assert "missing from database: 200" in out and "... and 195 more" in out
    assert len(out.splitlines()) < 12
    assert len(d.as_dict(limit=5)["missing"]["objects"]) == 5


def test_contract_mode() -> None:
    assert contract_mode({}, "production") == "enforce"
    assert contract_mode({"REVA_SCHEMA_CONTRACT": "off"}, "production") == "enforce"
    assert contract_mode({}, "local") == "enforce"
    assert contract_mode({}, "test") == "warn"
    assert contract_mode({"REVA_SCHEMA_CONTRACT": "enforce"}, "test") == "enforce"
    with pytest.raises(ConfigurationError):
        contract_mode({"REVA_SCHEMA_CONTRACT": "maybe"}, "local")
