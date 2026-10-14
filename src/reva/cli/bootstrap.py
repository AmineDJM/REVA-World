"""Pre-deploy bootstrap: lock, verify capabilities, migrate, provision runtime roles.

Only this module uses the migration-owner credential. It never prints secrets.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from reva.adapters.postgres.engine import make_engine
from reva.kernel.errors import ConfigurationError
from reva.settings import ROLE_LOGIN, Settings, load_settings

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


def provision_roles(conn: Connection, settings: Settings) -> list[str]:
    done = []
    for role, login in ROLE_LOGIN.items():
        pw = settings.role_password(role)  # type: ignore[arg-type]
        if pw is None:
            raise ConfigurationError(f"generated password for {login} is missing")
        stmt = conn.execute(
            text(
                "SELECT format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOBYPASSRLS CONNECTION LIMIT 10 PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"
            ),
            {"r": login, "p": pw.get_secret_value()},
        ).scalar_one()
        conn.execute(text(stmt))
        done.append(login)
    return done


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


def bootstrap(settings: Settings | None = None, *, allow_older_server: bool = False) -> dict[str, Any]:
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
            conn.execute(text("GRANT SELECT ON public.alembic_version TO reva_api, reva_worker, reva_sim"))
            verify_ownership(conn)
            conn.commit()
            report["schema_version"] = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            conn.commit()
    engine.dispose()
    log.info("bootstrap complete %s", json.dumps(report))
    return report
