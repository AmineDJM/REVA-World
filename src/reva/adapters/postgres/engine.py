"""Lazy, role-scoped SQLAlchemy engines. Each runtime role has its own pool."""

from __future__ import annotations

import threading
from typing import Literal

from sqlalchemy import Engine, create_engine

from reva.settings import Role, Settings, get_settings

_lock = threading.Lock()
_engines: dict[str, Engine] = {}

EngineKind = Literal["api", "worker", "sim", "gate", "migration"]


def make_engine(kind: EngineKind, settings: Settings | None = None) -> Engine:
    s = settings or get_settings()
    url = s.migration_url() if kind == "migration" else s.runtime_url(kind)  # type: ignore[arg-type]
    timeout = s.reva_db_statement_timeout_ms if kind == "api" else max(s.reva_db_statement_timeout_ms, 300_000)
    return create_engine(
        url,
        pool_size=s.reva_db_pool_size,
        max_overflow=s.reva_db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        hide_parameters=True,  # bound values (token hashes, passwords) never reach error text
        connect_args={
            "options": f"-c statement_timeout={timeout} -c lock_timeout=5000",
            "application_name": f"reva-{kind}",
            "connect_timeout": 10,
        },
    )


def get_engine(kind: EngineKind) -> Engine:
    with _lock:
        eng = _engines.get(kind)
        if eng is None:
            eng = make_engine(kind)
            _engines[kind] = eng
        return eng


def dispose_all() -> None:
    with _lock:
        for e in _engines.values():
            e.dispose()
        _engines.clear()


def role_for(kind: EngineKind) -> Role:
    if kind == "migration":
        raise ValueError("migration engine has no runtime role")
    return kind
