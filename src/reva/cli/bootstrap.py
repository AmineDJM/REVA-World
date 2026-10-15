"""Pre-deploy bootstrap: lock, verify capabilities, migrate, provision runtime roles.

Only this module uses the migration-owner credential. It never prints secrets.

Role passwords (review finding R-17): the plaintext password never reaches the
server or an exception message. ``provision_roles`` computes a SCRAM-SHA-256
verifier client-side (RFC 5802/7677, the format PostgreSQL stores in
``pg_authid``) and sends only that, and any failure is re-raised as a
``ConfigurationError`` that names the role and the SQLSTATE but carries neither
the statement nor its parameters. Server-side exposure: with
``log_statement = ddl|all`` (or ``log_min_error_statement`` on a failing
statement, or pg_stat_statements) PostgreSQL logs the ``ALTER ROLE`` text, i.e.
the verifier. A verifier is not the password, but it allows an offline guessing
attack, so treat server logs as sensitive; when the migration role is a
superuser, bootstrap additionally turns ``log_statement`` off for that
transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from reva.adapters.postgres.engine import make_engine
from reva.kernel.errors import ConfigurationError
from reva.settings import OPTIONAL_OUTSIDE_PRODUCTION, ROLE_LOGIN, Settings, load_settings

log = logging.getLogger("reva.bootstrap")
MIGRATION_LOCK_KEY = 7262001
REPO_ROOT = Path(__file__).resolve().parents[3]
MIN_SERVER_VERSION = 170000
RUNTIME_ROLES = tuple(ROLE_LOGIN.values())


def alembic_config() -> Config:
    ini = REPO_ROOT / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def verify_capabilities(conn: Connection, *, allow_older_server: bool = False) -> dict[str, Any]:
    ver = int(conn.execute(text("SHOW server_version_num")).scalar_one())
    if ver < MIN_SERVER_VERSION and not allow_older_server:
        raise ConfigurationError(f"PostgreSQL >= 17 required (server_version_num={ver})")
    postgis = conn.execute(
        text("SELECT default_version FROM pg_available_extensions WHERE name = 'postgis'")
    ).scalar_one_or_none()
    if postgis is None:
        raise ConfigurationError("PostGIS extension is not available on this server")
    can_create_role = conn.execute(
        text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if not can_create_role:
        raise ConfigurationError("migration owner lacks CREATEROLE; cannot provision runtime roles")
    return {"server_version_num": ver, "postgis_default_version": postgis}


SCRAM_ITERATIONS = 4096  # PostgreSQL's default scram_iterations


def scram_sha256_verifier(password: str, *, iterations: int = SCRAM_ITERATIONS, salt: bytes | None = None) -> str:
    """Return the SCRAM-SHA-256 verifier PostgreSQL stores for ``password``.

    ``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`` (all base64).
    PostgreSQL applies SASLprep to the password; for printable ASCII that is the
    identity, so only such passwords are accepted (generated passwords are).
    """
    if not password or not all(0x20 < ord(ch) < 0x7F for ch in password):
        raise ConfigurationError("runtime role passwords must be non-empty printable ASCII without spaces")
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return str(getattr(orig, "sqlstate", None) or getattr(exc, "sqlstate", None) or type(exc).__name__)


ROLE_PROVISION_LOCK_KEY = 7262002
#: SQLSTATEs worth retrying when several bootstraps (e.g. parallel test databases on one
#: cluster) alter the cluster-global runtime roles at once: "tuple concurrently updated"
#: surfaces as XX000; serialization/deadlock/lock-timeout as 40001/40P01/55P03.
_RETRYABLE = {"XX000", "40001", "40P01", "55P03"}
PROVISION_ATTEMPTS = 8


def role_connection_limit(settings: Settings) -> int:
    """CONNECTION LIMIT for each runtime role (cluster-wide, per role).

    ``REVA_ROLE_CONNECTION_LIMIT`` (or a ``reva_role_connection_limit`` setting):
    default 20 in production, where it must be a positive integer; default -1
    (unlimited) in local/test, where parallel test databases share the roles.
    """
    raw = getattr(settings, "reva_role_connection_limit", None)
    if raw is None:
        raw = os.environ.get("REVA_ROLE_CONNECTION_LIMIT")
    production = getattr(settings, "reva_env", "local") == "production"
    if raw is None or str(raw).strip() == "":
        return 20 if production else -1
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ConfigurationError("REVA_ROLE_CONNECTION_LIMIT must be an integer") from None
    if production and value < 1:
        raise ConfigurationError("REVA_ROLE_CONNECTION_LIMIT must be a positive integer in production")
    if value == 0 or value < -1 or value > 10000:
        raise ConfigurationError("REVA_ROLE_CONNECTION_LIMIT must be -1 (unlimited) or 1..10000")
    return value


def _verifier_matches(stored: str | None, password: str) -> bool:
    """True when a stored SCRAM verifier was derived from ``password``."""
    if not stored or not stored.startswith("SCRAM-SHA-256$"):
        return False
    try:
        head, _keys = stored[len("SCRAM-SHA-256$"):].split("$", 1)
        iterations, salt = head.split(":", 1)
        expected = scram_sha256_verifier(password, iterations=int(iterations), salt=base64.b64decode(salt))
    except (ValueError, ConfigurationError):
        return False
    return hmac.compare_digest(expected.encode(), stored.encode())


def _role_already_provisioned(conn: Connection, login: str, password: str, limit: int) -> bool:
    """Skip a needless catalog update (fewer concurrent ALTER ROLE races). Needs pg_authid
    (superuser); otherwise the role is always (re)provisioned."""
    row = conn.execute(
        text(
            "SELECT rolpassword, rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, "
            "rolreplication, rolconnlimit FROM pg_authid WHERE rolname = :r"
        ),
        {"r": login},
    ).mappings().one_or_none()
    if row is None:
        return False
    flags_ok = row["rolcanlogin"] and not any(
        row[k] for k in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls", "rolreplication")
    )
    return bool(flags_ok and row["rolconnlimit"] == limit and _verifier_matches(row["rolpassword"], password))


def provision_roles(conn: Connection, settings: Settings) -> list[str]:
    """Set LOGIN + password for each runtime role, sending only a SCRAM verifier.

    The statement is composed server-side (``format(%I, %L)``) from bind
    parameters; on any failure the original exception (whose text would contain
    the statement) is suppressed and only the role and SQLSTATE are reported.
    Roles are cluster-global: concurrent bootstraps of different databases can
    race on the same catalog rows, so retryable failures are retried with
    backoff, and a role that already has exactly this configuration is left
    untouched. (An advisory lock only serialises bootstraps of the same database.)
    """
    import random
    import time

    done = []
    superuser = bool(
        conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one_or_none()
    )
    limit = role_connection_limit(settings)
    skipped: list[str] = []
    for role, login in ROLE_LOGIN.items():
        pw = settings.role_password(role)  # type: ignore[arg-type]
        if (pw is None or not pw.get_secret_value()) and role in OPTIONAL_OUTSIDE_PRODUCTION \
                and getattr(settings, "reva_env", "local") != "production":
            # (GATE) Outside production the independent gate login may be left unprovisioned:
            # nothing can then record gate results, so nothing is auto-promoted (fail closed).
            # The role is left as it is (roles are cluster-global; other databases may use it).
            log.warning("runtime role %s not provisioned (no password configured)", login)
            skipped.append(login)
            continue
        if pw is None:
            raise ConfigurationError(f"generated password for {login} is missing")
        failure: str | None = None
        for attempt in range(PROVISION_ATTEMPTS):
            failure = None
            verifier = ""
            try:
                with conn.begin_nested():
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ROLE_PROVISION_LOCK_KEY})
                    if superuser and _role_already_provisioned(conn, login, pw.get_secret_value(), limit):
                        break
                    verifier = scram_sha256_verifier(pw.get_secret_value())
                    if superuser:  # keep even the verifier out of statement logs where we can
                        conn.execute(text("SET LOCAL log_statement = 'none'"))
                    stmt = conn.execute(
                        text(
                            # Only attributes a non-superuser CREATEROLE owner may set (PG16+);
                            # the privileged attributes are verified afterwards instead.
                            "SELECT format('ALTER ROLE %I LOGIN NOINHERIT CONNECTION LIMIT %s PASSWORD %L', "
                            "CAST(:r AS text), CAST(:l AS integer), CAST(:v AS text))"
                        ),
                        {"r": login, "l": limit, "v": verifier},
                    ).scalar_one()
                    # Not text(): the verifier contains ':' which text() would parse as bind markers.
                    conn.exec_driver_sql(stmt)
                    stmt = ""
                break
            except ConfigurationError:
                raise
            except Exception as e:
                failure = _sqlstate(e)
            finally:
                verifier = ""
            if failure not in _RETRYABLE:
                break
            time.sleep(min(2.0, 0.05 * (2**attempt)) * (0.5 + random.random()))
        if failure is not None:
            raise ConfigurationError(f"provisioning runtime role {login} failed (SQLSTATE {failure})") from None
        done.append(login)
    verify_runtime_role_attributes(conn, done)
    if skipped:
        verify_runtime_role_attributes(conn, skipped, require_login=False)
    return done


FORBIDDEN_ROLE_ATTRIBUTES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls", "rolreplication")


def verify_runtime_role_attributes(conn: Connection, logins: list[str], *, require_login: bool = True) -> None:
    """Fail loudly unless every runtime role is a NOINHERIT login without privileged attributes.

    ``require_login=False`` is used for an optional role that was not provisioned (no password):
    it must still exist and hold no privileged attribute, but may be NOLOGIN.
    """
    rows = conn.execute(
        text("SELECT rolname, rolcanlogin, rolinherit, " + ", ".join(FORBIDDEN_ROLE_ATTRIBUTES)
             + " FROM pg_roles WHERE rolname = ANY(:r)"),
        {"r": logins},
    ).mappings().all()
    found = {r["rolname"]: r for r in rows}
    problems = []
    for login in logins:
        r = found.get(login)
        if r is None:
            problems.append(f"{login}: missing")
            continue
        bad = [k.removeprefix("rol").upper() for k in FORBIDDEN_ROLE_ATTRIBUTES if r[k]]
        if r["rolinherit"]:
            bad.append("INHERIT")
        if require_login and not r["rolcanlogin"]:
            bad.append("NOLOGIN")
        if bad:
            problems.append(f"{login}: {', '.join(bad)}")
    if problems:
        raise ConfigurationError("runtime roles have unsafe attributes (a superuser must fix them): "
                                 + "; ".join(problems))


def verify_ownership(conn: Connection) -> None:
    owned = conn.execute(
        text(
            """
            SELECT n.nspname || '.' || c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE n.nspname IN ('meta','ontology','world','evidence','belief','model','sim','ops')
              AND r.rolname = ANY(:roles)
            """
        ),
        {"roles": list(RUNTIME_ROLES)},
    ).scalars().all()
    if owned:
        raise ConfigurationError(f"runtime roles own scientific objects: {owned}")
    members = conn.execute(
        text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.roleid WHERE g.rolname = 'reva_restricted_deletion' "
            "AND r.rolname = ANY(:roles)"
        ),
        {"roles": list(RUNTIME_ROLES)},
    ).scalars().all()
    if members:
        raise ConfigurationError(f"runtime roles must not hold deletion privilege: {members}")
    verify_runtime_memberships(conn)


#: NOLOGIN roles that own or receive privileged objects; no runtime role may be a member.
PRIVILEGED_GROUP_ROLES = ("reva_registry", "reva_restricted_deletion")


def verify_runtime_memberships(conn: Connection) -> None:
    """(R5-09) Runtime roles hold no role memberships at all.

    On PostgreSQL 16+ ``NOINHERIT`` is only the default for *new* grants: an explicit
    ``GRANT x TO reva_worker WITH INHERIT TRUE, SET TRUE`` gives the worker x's privileges
    (and ``SET ROLE x``) although ``rolinherit`` is false. So every membership held by a
    runtime role (whatever its INHERIT/SET/ADMIN options) fails bootstrap, as does any
    LOGIN role other than the migration owner in ``reva_registry`` (it owns the approval
    functions) and a ``reva_registry`` that can log in.
    """
    held = conn.execute(
        text(
            "SELECT r.rolname || ' -> ' || g.rolname || ' (admin=' || m.admin_option || ', inherit='"
            " || m.inherit_option || ', set=' || m.set_option || ')' "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.roleid WHERE r.rolname = ANY(:roles) ORDER BY 1"
        ),
        {"roles": list(RUNTIME_ROLES)},
    ).scalars().all()
    if held:
        raise ConfigurationError(f"runtime roles must not be members of other roles (a superuser must revoke): {held}")
    registry = conn.execute(
        text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'reva_registry'")
    ).scalar_one_or_none()
    if registry:
        raise ConfigurationError("reva_registry must be NOLOGIN")
    logins = conn.execute(
        text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.roleid WHERE g.rolname = ANY(:groups) "
            "AND r.rolcanlogin AND r.rolname <> current_user AND NOT r.rolsuper ORDER BY 1"
        ),
        {"groups": ["reva_registry"]},
    ).scalars().all()
    if logins:
        raise ConfigurationError(f"login roles must not be members of reva_registry: {logins}")


def bootstrap(settings: Settings | None = None, *, allow_older_server: bool = False,
              seed_registry: bool = True) -> dict[str, Any]:
    """Migrate, provision runtime roles, verify them and (by default) seed the reviewed registry.

    (R5-01) The registry seed writes approved mappings, eligibility decisions and models,
    which runtime roles may not write; it is a deploy-time step run with the migration
    owner here, not a worker job.
    """
    s = settings or load_settings()
    engine = make_engine("migration", s)
    report: dict[str, Any] = {}
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        conn.commit()
        try:
            report["capabilities"] = verify_capabilities(conn, allow_older_server=allow_older_server)
            conn.commit()
            cfg = alembic_config()
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
            report["roles"] = provision_roles(conn, s)
            # Readiness checks read the schema version; nothing else in public is granted.
            conn.execute(text("GRANT SELECT ON public.alembic_version TO reva_api, reva_worker, reva_sim, reva_gate"))
            verify_ownership(conn)
            conn.commit()
            from reva.adapters.postgres.schema_contract import assert_schema_contract

            drift = assert_schema_contract(conn, reva_env=s.reva_env)
            report["schema_fingerprint"] = drift.actual_digest if drift else None
            report["schema_version"] = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            conn.commit()
    if seed_registry:
        from reva.adapters.objects.factory import build_store
        from reva.cli.seed import seed

        report["seed"] = seed(engine, build_store(s), contact_email=s.reva_http_contact_email)
    engine.dispose()
    log.info("bootstrap complete %s", json.dumps(report, default=str))
    return report
