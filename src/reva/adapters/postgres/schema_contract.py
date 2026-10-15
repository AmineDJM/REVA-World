"""Schema contract: detect a database whose catalog differs from what the migrations build.

Migrations are edited in place until release (same revision ids), so
``alembic_version`` alone cannot tell whether a database actually has the
current tables, triggers, functions and grants (review finding R5-04). This
module fingerprints the catalog objects the application relies on and compares
them with ``migrations/schema_contract.json``, which is generated from a fresh
bootstrap (``alembic upgrade head`` + runtime role provisioning) of a throwaway
database by ``scripts/update_schema_contract.py``.

What is fingerprinted (one entry per object, order-independent, no OIDs):

* app schemas (``meta`` … ``ops`` and ``public``) with owner/ACL, the database
  ACL and installed extensions (name and schema, not version);
* relations (tables, views, sequences, composite types) with kind, RLS flags,
  options, ACL, view definition and sequence parameters; one entry per column
  (type, nullability, default, identity/generated, collation, column ACL);
* constraints, indexes, non-internal triggers (``pg_get_triggerdef``), event
  triggers, enum/domain/range types;
* functions/procedures keyed by ``schema.name(input arg types)`` with arguments (names,
  defaults), result,
  language, SECURITY DEFINER, volatility, strictness, ``proconfig``, ACL and a
  SHA-256 of the body (``prosrc`` plus any SQL-standard body);
* RLS policies; default privileges;
* attributes and memberships of the app roles (``reva_api``, ``reva_worker``,
  ``reva_sim``, ``reva_restricted_deletion``).

Instance-specific names are normalised: the migration owner (whatever its name)
renders as ``<owner>``, other non-app roles as ``<other>``; the database name,
role passwords, LOGIN/CONNECTION LIMIT of runtime roles (set per environment by
bootstrap) and extension versions are excluded. Objects that belong to an
extension (PostGIS functions, ``spatial_ref_sys``) are excluded.

The catalog is readable by every role, so the runtime roles can verify too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import ConfigurationError

log = logging.getLogger("reva.schema_contract")

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = REPO_ROOT / "migrations" / "schema_contract.json"
MIGRATION_SOURCES = (REPO_ROOT / "migrations" / "versions", Path(__file__).resolve().with_name("ddl.py"))
CONTRACT_FORMAT = 1
APP_SCHEMAS = ("meta", "ontology", "world", "evidence", "belief", "model", "sim", "ops", "public")
RUNTIME_ROLES = ("reva_api", "reva_worker", "reva_sim")
APP_ROLES = (*RUNTIME_ROLES, "reva_restricted_deletion", "reva_registry")
REGENERATE_HINT = "uv run python scripts/update_schema_contract.py"
#: ``REVA_SCHEMA_CONTRACT``: enforce (refuse to start), warn (log only) or off.
MODE_ENV = "REVA_SCHEMA_CONTRACT"
MODES = ("enforce", "warn", "off")


class SchemaDriftError(ConfigurationError):
    code = "schema_drift"


# --------------------------------------------------------------------------- normalisation
def _role(name: str | None, owner: str | None) -> str:
    """Instance-independent role name: app roles, PUBLIC and pg_* keep their names."""
    if name is None or name == "PUBLIC" or name in APP_ROLES or name.startswith("pg_"):
        return name or "PUBLIC"
    return "<owner>" if name == owner else "<other>"


def _acl(entries: Iterable[Any] | None, owner: str | None) -> list[list[Any]]:
    """[[grantee, privilege, grantable], ...] sorted; the grantor is not compared."""
    out = {(_role(g, owner), p, bool(w)) for g, p, w in (entries or [])}
    return [list(x) for x in sorted(out)]


def _digest(attrs: Mapping[str, Any]) -> str:
    raw = json.dumps(attrs, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _body_hash(*parts: str | None) -> str:
    return hashlib.sha256("\x00".join(p or "" for p in parts).encode()).hexdigest()


_NOT_EXT = """NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d WHERE d.classid = '{cat}'::regclass
                         AND d.objid = {oid} AND d.deptype = 'e')"""
_ACL_SQL = """(SELECT coalesce(json_agg(json_build_array(
                 CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END,
                 a.privilege_type, a.is_grantable)), '[]'::json)
               FROM pg_catalog.aclexplode({acl}) a)"""


def _acl_sql(acl: str, kind: str, owner: str) -> str:
    """ACL entries, falling back to the built-in default (``kind`` is an SQL "char" expression)."""
    return _ACL_SQL.format(acl=f"coalesce({acl}, pg_catalog.acldefault({kind}, {owner}))")


# --------------------------------------------------------------------------- collection
def collect_objects(conn: Connection) -> dict[str, dict[str, Any]]:
    """Return ``{object key: attributes}`` for every contract-relevant catalog object."""
    p = {"s": list(APP_SCHEMAS), "roles": list(APP_ROLES), "runtime": list(RUNTIME_ROLES)}
    objs: dict[str, dict[str, Any]] = {}

    def rows(sql: str) -> list[Any]:
        return list(conn.execute(text(sql), p).mappings())

    for r in rows(f"""
        SELECT n.nspname, pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
               {_acl_sql('n.nspacl', "'n'", 'n.nspowner')} AS acl
        FROM pg_catalog.pg_namespace n WHERE n.nspname = ANY(:s)"""):
        objs[f"schema:{r['nspname']}"] = {"owner": _role(r["owner"], None), "acl": _acl(r["acl"], r["owner"])}

    for r in rows(f"""
        SELECT pg_catalog.pg_get_userbyid(d.datdba) AS owner, {_acl_sql('d.datacl', "'d'", 'd.datdba')} AS acl
        FROM pg_catalog.pg_database d WHERE d.datname = current_database()"""):
        objs["database"] = {"acl": _acl(r["acl"], r["owner"])}

    for r in rows("""SELECT e.extname, n.nspname FROM pg_catalog.pg_extension e
                     JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace"""):
        objs[f"extension:{r['extname']}"] = {"schema": r["nspname"]}

    for r in rows(f"""
        SELECT n.nspname, c.relname, c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity,
               c.reloptions, c.relispartition, pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS partbound,
               CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END AS partkey,
               CASE WHEN c.relkind IN ('v', 'm') THEN pg_catalog.pg_get_viewdef(c.oid) END AS viewdef,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner,
               {_acl_sql('c.relacl', "CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::\"char\"", 'c.relowner')} AS acl,
               s.seqtypid::regtype::text AS seqtype, s.seqstart, s.seqincrement, s.seqmin, s.seqmax,
               s.seqcache, s.seqcycle
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid
        WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c')
          AND {_NOT_EXT.format(cat='pg_class', oid='c.oid')}"""):
        attrs = {
            "kind": r["relkind"], "persistence": r["relpersistence"], "rls": r["relrowsecurity"],
            "force_rls": r["relforcerowsecurity"], "options": sorted(r["reloptions"] or []),
            "partition_of": r["partbound"], "partkey": r["partkey"],
            "owner": _role(r["owner"], None), "acl": _acl(r["acl"], r["owner"]),
        }
        if r["viewdef"] is not None:
            attrs["viewdef_sha256"] = _body_hash(r["viewdef"])
        if r["relkind"] == "S":
            attrs["sequence"] = [r["seqtype"], r["seqstart"], r["seqincrement"], r["seqmin"], r["seqmax"],
                                 r["seqcache"], r["seqcycle"]]
        objs[f"relation:{r['nspname']}.{r['relname']}"] = attrs

    for r in rows(f"""
        SELECT n.nspname, c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull, pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS dflt, a.attidentity, a.attgenerated,
               CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
                    THEN a.attcollation::regcollation::text END AS coll,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner,
               CASE WHEN a.attacl IS NOT NULL THEN {_ACL_SQL.format(acl='a.attacl')} END AS acl
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
        LEFT JOIN pg_catalog.pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c')
          AND a.attnum > 0 AND NOT a.attisdropped
          AND {_NOT_EXT.format(cat='pg_class', oid='c.oid')}"""):
        objs[f"column:{r['nspname']}.{r['relname']}.{r['attname']}"] = {
            "type": r["type"], "not_null": r["attnotnull"], "default": r["dflt"],
            "identity": r["attidentity"], "generated": r["attgenerated"], "collation": r["coll"],
            "acl": _acl(r["acl"], r["owner"]),
        }

    for r in rows(f"""
        SELECT n.nspname, coalesce(c.relname, t.typname) AS parent, co.conname, co.contype, co.convalidated,
               pg_catalog.pg_get_constraintdef(co.oid) AS def
        FROM pg_catalog.pg_constraint co JOIN pg_catalog.pg_namespace n ON n.oid = co.connamespace
        LEFT JOIN pg_catalog.pg_class c ON c.oid = co.conrelid
        LEFT JOIN pg_catalog.pg_type t ON t.oid = co.contypid
        WHERE n.nspname = ANY(:s) AND (c.oid IS NULL OR {_NOT_EXT.format(cat='pg_class', oid='c.oid')})"""):
        objs[f"constraint:{r['nspname']}.{r['parent']}.{r['conname']}"] = {
            "type": r["contype"], "def": r["def"], "validated": r["convalidated"]}

    for r in rows(f"""
        SELECT n.nspname, ci.relname, pg_catalog.pg_get_indexdef(i.indexrelid) AS def, i.indisvalid
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class ci ON ci.oid = i.indexrelid
        JOIN pg_catalog.pg_class ct ON ct.oid = i.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = ct.relnamespace
        WHERE n.nspname = ANY(:s) AND {_NOT_EXT.format(cat='pg_class', oid='ct.oid')}"""):
        objs[f"index:{r['nspname']}.{r['relname']}"] = {"def": r["def"], "valid": r["indisvalid"]}

    for r in rows("""
        SELECT n.nspname, c.relname, t.tgname, pg_catalog.pg_get_triggerdef(t.oid) AS def, t.tgenabled
        FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(:s) AND NOT t.tgisinternal"""):
        objs[f"trigger:{r['nspname']}.{r['relname']}.{r['tgname']}"] = {"def": r["def"], "enabled": r["tgenabled"]}

    for r in rows(f"""
        SELECT e.evtname, e.evtevent, e.evtenabled, e.evttags, e.evtfoid::regproc::text AS fn
        FROM pg_catalog.pg_event_trigger e WHERE {_NOT_EXT.format(cat='pg_event_trigger', oid='e.oid')}"""):
        objs[f"event_trigger:{r['evtname']}"] = {"event": r["evtevent"], "enabled": r["evtenabled"],
                                                 "tags": sorted(r["evttags"] or []), "function": r["fn"]}

    for r in rows(f"""
        SELECT n.nspname, t.typname, t.typtype, t.typnotnull, t.typdefault,
               CASE WHEN t.typtype = 'd' THEN pg_catalog.format_type(t.typbasetype, t.typtypmod) END AS base,
               CASE WHEN t.typtype = 'r' THEN (SELECT rngsubtype::regtype::text FROM pg_catalog.pg_range
                                               WHERE rngtypid = t.oid) END AS subtype,
               ARRAY(SELECT e.enumlabel::text FROM pg_catalog.pg_enum e WHERE e.enumtypid = t.oid
                     ORDER BY e.enumsortorder) AS labels,
               pg_catalog.pg_get_userbyid(t.typowner) AS owner,
               {_acl_sql('t.typacl', "'T'", 't.typowner')} AS acl
        FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = ANY(:s) AND t.typtype IN ('e', 'd', 'r')
          AND {_NOT_EXT.format(cat='pg_type', oid='t.oid')}"""):
        objs[f"type:{r['nspname']}.{r['typname']}"] = {
            "kind": r["typtype"], "base": r["base"], "subtype": r["subtype"], "labels": list(r["labels"]),
            "not_null": r["typnotnull"], "default": r["typdefault"],
            "owner": _role(r["owner"], None), "acl": _acl(r["acl"], r["owner"])}

    for r in rows(f"""
        SELECT n.nspname, p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS ident,
               pg_catalog.pg_get_function_arguments(p.oid) AS args,
               pg_catalog.pg_get_function_result(p.oid) AS result, p.prokind, l.lanname, p.prosecdef,
               p.provolatile, p.proisstrict, p.proleakproof, p.proparallel, p.proretset, p.proconfig,
               p.prosrc, pg_catalog.pg_get_function_sqlbody(p.oid) AS sqlbody,
               pg_catalog.pg_get_userbyid(p.proowner) AS owner,
               {_acl_sql('p.proacl', "'f'", 'p.proowner')} AS acl
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_catalog.pg_language l ON l.oid = p.prolang
        WHERE n.nspname = ANY(:s) AND {_NOT_EXT.format(cat='pg_proc', oid='p.oid')}"""):
        objs[f"function:{r['nspname']}.{r['proname']}({r['ident']})"] = {
            "kind": r["prokind"], "args": r["args"], "result": r["result"], "language": r["lanname"],
            "security_definer": r["prosecdef"], "volatility": r["provolatile"], "strict": r["proisstrict"],
            "leakproof": r["proleakproof"], "parallel": r["proparallel"], "returns_set": r["proretset"],
            "config": sorted(r["proconfig"] or []), "body_sha256": _body_hash(r["prosrc"], r["sqlbody"]),
            "owner": _role(r["owner"], None), "acl": _acl(r["acl"], r["owner"])}

    for r in rows("""
        SELECT n.nspname, c.relname, pol.polname, pol.polcmd, pol.polpermissive,
               ARRAY(SELECT CASE WHEN x = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(x) END
                     FROM unnest(pol.polroles) x) AS roles,
               pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS qual,
               pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS with_check,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner
        FROM pg_catalog.pg_policy pol JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(:s)"""):
        objs[f"policy:{r['nspname']}.{r['relname']}.{r['polname']}"] = {
            "cmd": r["polcmd"], "permissive": r["polpermissive"],
            "roles": sorted(_role(x, r["owner"]) for x in r["roles"]), "using": r["qual"],
            "with_check": r["with_check"]}

    for r in rows(f"""
        SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS role, n.nspname, d.defaclobjtype,
               {_ACL_SQL.format(acl='d.defaclacl')} AS acl
        FROM pg_catalog.pg_default_acl d LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
        WHERE n.nspname IS NULL OR n.nspname = ANY(:s)"""):
        who = _role(r["role"], None) if r["role"] in APP_ROLES else "<role>"
        key = f"default_acl:{who}:{r['nspname'] or '*'}:{r['defaclobjtype']}"
        objs[key] = {"acl": sorted(objs.get(key, {}).get("acl", []) + _acl(r["acl"], r["role"]))}

    for r in rows("""
        SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication,
               rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = ANY(:roles)"""):
        attrs = {k: r[k] for k in ("rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolbypassrls",
                                   "rolreplication")}
        if r["rolname"] not in RUNTIME_ROLES:  # runtime LOGIN is set per environment by bootstrap
            attrs["rolcanlogin"] = r["rolcanlogin"]
        objs[f"role:{r['rolname']}"] = attrs

    member_of: dict[str, list[list[Any]]] = {r: [] for r in APP_ROLES if f"role:{r}" in objs}
    for r in rows("""
        SELECT m.rolname AS member, g.rolname AS grp, am.admin_option, am.inherit_option, am.set_option
        FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles m ON m.oid = am.member JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
        WHERE m.rolname = ANY(:roles)"""):
        member_of.setdefault(r["member"], []).append(
            [_role(r["grp"], None), r["admin_option"], r["inherit_option"], r["set_option"]])
    for member, groups in member_of.items():
        objs[f"membership:{member}"] = {"member_of": sorted(groups)}
    return objs


# --------------------------------------------------------------------------- fingerprint and diff
@dataclass(frozen=True)
class Fingerprint:
    objects: dict[str, str]  # key -> attribute digest

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for k in sorted(self.objects):
            h.update(f"{k}={self.objects[k]}\n".encode())
        return h.hexdigest()

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for k in self.objects:
            kind = k.split(":", 1)[0]
            out[kind] = out.get(kind, 0) + 1
        return dict(sorted(out.items()))


def compute_fingerprint(conn: Connection) -> Fingerprint:
    return Fingerprint({k: _digest(v) for k, v in collect_objects(conn).items()})


def migration_sources_digest() -> str:
    """SHA-256 over the migration sources (informational staleness hint only)."""
    h = hashlib.sha256()
    for src in MIGRATION_SOURCES:
        files = sorted(src.glob("*.py")) if src.is_dir() else [src]
        for f in files:
            if f.is_file():
                h.update(f.name.encode() + b"\x00" + f.read_bytes() + b"\x00")
    return h.hexdigest()


@dataclass
class SchemaDiff:
    missing: list[str] = field(default_factory=list)  # in contract, absent from the database
    unexpected: list[str] = field(default_factory=list)  # in the database, not in the contract
    changed: list[str] = field(default_factory=list)  # present in both, different definition
    contract_head: str | None = None
    live_head: str | None = None
    expected_digest: str = ""
    actual_digest: str = ""

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unexpected or self.changed)

    def summary(self, limit: int = 20) -> str:
        if self.ok:
            return f"schema matches contract (fingerprint {self.actual_digest[:16]}, head {self.live_head})"
        lines = [
            f"schema drift: database differs from migrations/schema_contract.json "
            f"(contract head {self.contract_head}, database alembic_version {self.live_head}; "
            f"fingerprint {self.actual_digest[:16]} != {self.expected_digest[:16]})"
        ]
        for title, keys in (("missing from database", self.missing), ("not in contract", self.unexpected),
                            ("definition differs", self.changed)):
            if keys:
                lines.append(f"  {title}: {len(keys)}")
                lines.extend(f"    - {k}" for k in keys[:limit])
                if len(keys) > limit:
                    lines.append(f"    ... and {len(keys) - limit} more")
        lines.append(
            "  The migrations were changed after this database was migrated (same revision ids), or the database "
            "was altered by hand. Recreate it from the migrations; if the migrations changed intentionally, "
            f"regenerate the contract with `{REGENERATE_HINT}`."
        )
        return "\n".join(lines)

    def as_dict(self, limit: int = 50) -> dict[str, Any]:
        return {
            "ok": self.ok, "contract_head": self.contract_head, "database_head": self.live_head,
            "expected_fingerprint": self.expected_digest, "actual_fingerprint": self.actual_digest,
            **{k: {"count": len(v), "objects": v[:limit]}
               for k, v in (("missing", self.missing), ("unexpected", self.unexpected), ("changed", self.changed))},
        }


def diff_fingerprints(expected: Mapping[str, str], actual: Mapping[str, str]) -> SchemaDiff:
    return SchemaDiff(
        missing=sorted(set(expected) - set(actual)),
        unexpected=sorted(set(actual) - set(expected)),
        changed=sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k]),
        expected_digest=Fingerprint(dict(expected)).digest,
        actual_digest=Fingerprint(dict(actual)).digest,
    )


def build_contract(fp: Fingerprint, alembic_head: str | None) -> dict[str, Any]:
    return {
        "format": CONTRACT_FORMAT,
        "comment": f"Generated by {REGENERATE_HINT} from a fresh bootstrap of a throwaway database. Do not edit.",
        "alembic_head": alembic_head,
        "migration_sources_sha256": migration_sources_digest(),
        "fingerprint": fp.digest,
        "counts": fp.counts(),
        "objects": dict(sorted(fp.objects.items())),
    }


def load_contract(path: Path | None = None) -> dict[str, Any]:
    path = path or CONTRACT_PATH
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"schema contract {path} is missing; generate it with `{REGENERATE_HINT}`") from None
    if data.get("format") != CONTRACT_FORMAT or not isinstance(data.get("objects"), dict):
        raise ConfigurationError(f"schema contract {path} has an unsupported format; regenerate it")
    return data


def write_contract(contract: Mapping[str, Any], path: Path | None = None) -> Path:
    path = path or CONTRACT_PATH
    path.write_text(json.dumps(contract, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _alembic_head(conn: Connection) -> str | None:
    try:
        with conn.begin_nested():
            return conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one_or_none()
    except Exception:
        return None


def verify_schema(conn: Connection, contract: Mapping[str, Any] | None = None) -> SchemaDiff:
    """Compare the connected database with the contract (read-only)."""
    contract = contract if contract is not None else load_contract()
    d = diff_fingerprints(contract["objects"], compute_fingerprint(conn).objects)
    d.contract_head = contract.get("alembic_head")
    d.live_head = _alembic_head(conn)
    return d


def contract_mode(environ: Mapping[str, str] | None = None, reva_env: str | None = None) -> str:
    """enforce | warn | off. Production always enforces; the test environment defaults to warn
    (other agents' in-flight migration edits must not break every DB test; the dedicated
    staleness test enforces the contract there)."""
    env = os.environ if environ is None else environ
    reva_env = reva_env or env.get("REVA_ENV", "local")
    if reva_env == "production":
        return "enforce"
    mode = (env.get(MODE_ENV) or ("warn" if reva_env == "test" else "enforce")).strip().lower()
    if mode not in MODES:
        raise ConfigurationError(f"{MODE_ENV} must be one of {', '.join(MODES)}")
    return mode


def assert_schema_contract(target: Engine | Connection, *, mode: str | None = None,
                           reva_env: str | None = None, contract_path: Path | None = None) -> SchemaDiff | None:
    """Startup guard: raise ``SchemaDriftError`` (mode enforce) when the live schema differs.

    Returns the diff (``None`` when the mode is ``off``, or in ``warn`` mode when the check
    itself could not run). In ``enforce`` mode an unverifiable schema also refuses. Read-only.
    """
    mode = mode or contract_mode(reva_env=reva_env)
    if mode == "off":
        return None
    try:
        contract = load_contract(contract_path)
        if isinstance(target, Connection):
            d = verify_schema(target, contract)
        else:
            with target.connect() as conn:
                d = verify_schema(conn, contract)
                conn.rollback()
    except Exception as e:
        if mode == "warn":  # e.g. no database in a unit test: never block, but say so
            log.warning("schema contract not verified: %s", type(e).__name__)
            return None
        if isinstance(e, ConfigurationError):
            raise
        raise SchemaDriftError(f"schema contract could not be verified ({type(e).__name__}); refusing to start") from e
    if d.ok:
        log.info("schema contract verified (%s)", d.actual_digest[:16])
        return d
    if mode == "warn":
        log.warning("%s", d.summary())
        return d
    raise SchemaDriftError(d.summary(), missing=len(d.missing), unexpected=len(d.unexpected),
                           changed=len(d.changed))


# --------------------------------------------------------------------------- reference generation
@contextmanager
def throwaway_database(base: Any, *, keep: bool = False, prefix: str = "reva_contract") -> Iterator[Any]:
    """Create ``<prefix>_<random>`` on the cluster of ``base``'s migration-owner URL, bootstrap it
    (fresh ``alembic upgrade head`` + runtime role provisioning, exactly what a deployment runs)
    and yield its ``Settings`` (``reva_env=test``). Dropped afterwards unless ``keep``. The
    database named in ``base`` (e.g. the dev database) is never touched."""
    import uuid

    from sqlalchemy import create_engine

    from reva.cli.bootstrap import bootstrap  # lazy: bootstrap imports alembic

    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        url = base.migration_url().set(database=name).render_as_string(hide_password=False)
        data = base.model_dump()
        data.update(reva_migration_database_url=url, reva_db_name=name, reva_env="test")
        settings = type(base).model_validate(data)
        bootstrap(settings)
        yield settings
    finally:
        if not keep:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def build_reference_contract(base: Any, *, keep: bool = False) -> dict[str, Any]:
    """Contract of a freshly bootstrapped throwaway database (see ``throwaway_database``)."""
    from sqlalchemy import create_engine

    with throwaway_database(base, keep=keep) as settings:
        eng = create_engine(settings.migration_url())
        try:
            with eng.connect() as conn:
                return build_contract(compute_fingerprint(conn), _alembic_head(conn))
        finally:
            eng.dispose()
