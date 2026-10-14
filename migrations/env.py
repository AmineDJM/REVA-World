"""Alembic environment. Uses only the migration-owner credential."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from reva.settings import load_settings

config = context.config


def _url():  # type: ignore[no-untyped-def]
    override = config.attributes.get("connection_url")
    return override if override is not None else load_settings().migration_url()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline SQL generation is not supported; run against a database")
run_migrations_online()
