"""``reva verify`` check for the independent gate login (``reva_gate``).

Connects *as the gate login* and compares what it can actually do with the exact
least-privilege set the migrations grant it (``0007_security``, governance section).
Anything extra (a table, column, sequence, schema or SECURITY DEFINER function grant,
a role attribute or a membership) or anything missing fails the check.

Only SECURITY DEFINER functions are compared: an ordinary function runs with the
caller's own privileges and grants nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, text

from reva.gate import GATE_LOGIN

#: Schemas the gate may use (and never create objects in).
EXPECTED_SCHEMA_USAGE = frozenset({"meta", "evidence", "ops"})
#: Relation privileges of the gate login (table level or any column), nothing else.
EXPECTED_RELATION_PRIVILEGES: dict[str, frozenset[str]] = {
    "ops.proposal": frozenset({"SELECT"}),
    "ops.gate_request": frozenset({"SELECT"}),
    "ops.gate_result": frozenset({"SELECT"}),
    "evidence.connector_version": frozenset({"SELECT"}),
    "evidence.source": frozenset({"SELECT"}),
    "evidence.eligibility": frozenset({"SELECT"}),
    "meta.artifact": frozenset({"SELECT", "INSERT"}),
    "meta.artifact_location": frozenset({"SELECT", "INSERT"}),
}
#: SECURITY DEFINER functions the gate may execute.
EXPECTED_DEFINER_FUNCTIONS = frozenset({"ops.record_gate_result(uuid,text,text,text,text,text,jsonb)"})
#: ``public`` keeps PostgreSQL's default USAGE for every role (extension functions, alembic_version).
DEFAULT_USAGE_SCHEMAS = frozenset({"public"})

RELATIONS_SQL = """
SELECT n.nspname || '.' || c.relname AS name,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                 AND d.deptype = 'e') AS extension_member,
       coalesce(array_agg(p.priv ORDER BY p.priv) FILTER (WHERE CASE
         WHEN c.relkind = 'S' THEN p.priv IN ('USAGE','SELECT','UPDATE') AND has_sequence_privilege(c.oid, p.priv)
         WHEN p.priv IN ('SELECT','INSERT','UPDATE','REFERENCES') THEN has_any_column_privilege(c.oid, p.priv)
         WHEN p.priv IN ('DELETE','TRUNCATE','TRIGGER') THEN has_table_privilege(c.oid, p.priv)
         ELSE false END), '{}') AS privs
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','USAGE']) AS p(priv)
WHERE c.relkind IN ('r','p','v','m','f','S')
  AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname NOT LIKE 'pg_temp%'
  AND n.nspname NOT LIKE 'pg_toast_temp%'
GROUP BY 1, 2
ORDER BY 1
"""
SCHEMAS_SQL = """
SELECT n.nspname AS name, has_schema_privilege(n.oid, 'USAGE') AS usage, has_schema_privilege(n.oid, 'CREATE') AS create
FROM pg_namespace n
WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname NOT LIKE 'pg_temp%'
  AND n.nspname NOT LIKE 'pg_toast_temp%'
"""
DEFINER_SQL = """
SELECT n.nspname || '.' || p.proname || '(' || oidvectortypes(p.proargtypes) || ')' AS name,
       has_function_privilege(p.oid, 'EXECUTE') AS can_execute
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')
"""
ROLE_SQL = """
SELECT session_user::text AS login, current_user::text AS current_role, r.rolsuper, r.rolinherit, r.rolcreaterole,
       r.rolcreatedb, r.rolcanlogin, r.rolreplication, r.rolbypassrls,
       (SELECT coalesce(array_agg(g.rolname::text ORDER BY g.rolname), '{}') FROM pg_auth_members m
          JOIN pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid) AS member_of,
       has_database_privilege(current_database(), 'CREATE') AS db_create
FROM pg_roles r WHERE r.rolname = session_user
"""


def _canon(fn: str) -> str:
    return fn.replace(" ", "")


def gate_privilege_report(engine: Engine) -> dict[str, Any]:
    """What the login behind ``engine`` can do, compared with the gate's expected privileges."""
    problems: list[str] = []
    with engine.connect() as conn:
        role = dict(conn.execute(text(ROLE_SQL)).mappings().one())
        if role["login"] != GATE_LOGIN or role["current_role"] != GATE_LOGIN:
            problems.append(f"connected as {role['login']}/{role['current_role']}, not {GATE_LOGIN}")
        for attr in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls", "rolinherit"):
            if role[attr]:
                problems.append(f"role attribute {attr} is set")
        if not role["rolcanlogin"]:
            problems.append("role cannot log in")
        if role["member_of"]:
            problems.append(f"role is a member of {', '.join(role['member_of'])}")
        if role["db_create"]:
            problems.append("role may CREATE schemas in the database")

        for s in conn.execute(text(SCHEMAS_SQL)).mappings():
            if s["create"]:
                problems.append(f"schema {s['name']}: CREATE")
            expected = s["name"] in EXPECTED_SCHEMA_USAGE
            if s["usage"] and not expected and s["name"] not in DEFAULT_USAGE_SCHEMAS:
                problems.append(f"schema {s['name']}: unexpected USAGE")
            if expected and not s["usage"]:
                problems.append(f"schema {s['name']}: missing USAGE")

        seen: set[str] = set()
        for r in conn.execute(text(RELATIONS_SQL)).mappings():
            name, have = str(r["name"]), set(r["privs"] or ())
            want = set(EXPECTED_RELATION_PRIVILEGES.get(name, frozenset()))
            if name == "public.alembic_version" or r["extension_member"]:
                # the schema version probe (check_gate_login) may read alembic_version; extension
                # catalogues (PostGIS spatial_ref_sys, geometry_columns, ...) are readable by every role
                want |= {"SELECT"} & have
            if have - want:
                problems.append(f"{name}: unexpected {', '.join(sorted(have - want))}")
            if want - have:
                problems.append(f"{name}: missing {', '.join(sorted(want - have))}")
            seen.add(name)
        for name in sorted(set(EXPECTED_RELATION_PRIVILEGES) - seen):
            problems.append(f"{name}: relation not found")

        executable = {_canon(str(f["name"])) for f in conn.execute(text(DEFINER_SQL)).mappings() if f["can_execute"]}
        expected_fns = {_canon(f) for f in EXPECTED_DEFINER_FUNCTIONS}
        for fn in sorted(executable - expected_fns):
            problems.append(f"function {fn}: unexpected EXECUTE (SECURITY DEFINER)")
        for fn in sorted(expected_fns - executable):
            problems.append(f"function {fn}: missing EXECUTE")
    return {"ok": not problems, "login": role["login"], "problems": problems}


def verify_gate(settings: Any, engine_factory: Any = None) -> dict[str, Any]:
    """The ``db_gate`` entry of ``reva verify``.

    * ``REVA_GATE_DB_PASSWORD`` unset: skipped with a note outside production (nothing can then be
      auto-promoted, which is fail closed); a failure in production, where the gate is required.
    * otherwise: the gate login must connect and hold exactly its expected privileges.
    """
    if getattr(settings, "reva_gate_db_password", None) is None:
        if str(getattr(settings, "reva_env", "")) == "production":
            return {"ok": False, "status": "missing",
                    "note": "REVA_GATE_DB_PASSWORD is required in production (independent promotion gate)"}
        return {"ok": True, "status": "skipped",
                "note": "REVA_GATE_DB_PASSWORD is not configured: the reva_gate login was not checked. Without "
                        "the gate process no proposal is auto-promoted (discovery reports gate_pending)."}
    if engine_factory is None:
        from reva.adapters.postgres.engine import make_engine

        def engine_factory() -> Engine:
            return make_engine("gate", settings)

    engine = engine_factory()
    try:
        report = gate_privilege_report(engine)
    except Exception as e:  # cannot connect / authenticate: never echo the URL or password
        return {"ok": False, "status": "unreachable", "error": type(getattr(e, "orig", None) or e).__name__}
    finally:
        engine.dispose()
    return {"status": "ok" if report["ok"] else "privilege_mismatch", **report}


__all__ = ["EXPECTED_DEFINER_FUNCTIONS", "EXPECTED_RELATION_PRIVILEGES", "EXPECTED_SCHEMA_USAGE",
           "gate_privilege_report", "verify_gate"]
