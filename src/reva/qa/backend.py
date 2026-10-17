"""Read-only data access for the QA checks: SQL rows + the object store.

Two backends share one interface:

* :func:`postgres_backend` — the dev/live database through a SQLAlchemy engine; the connection is set to
  ``default_transaction_read_only = on`` (every statement runs in a READ ONLY transaction) and the object store
  is the configured :class:`~reva.ports.storage.ArtifactStore` (reads decode zstd-wrapped objects and re-hash).
* :func:`fixture_backend` — an in-memory DuckDB database with the same schema-qualified table names (``kg.…``,
  ``meta.…``, ``evidence.…``; views such as ``kg.statement_current`` are plain tables there) and a
  :class:`MemoryStore`. Used by the tests ("fixture mode").

SQL is written to run on both engines: no parameters (literals are validated and inlined by :func:`lit`), no
Postgres-only operators.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.kernel.errors import IntegrityFailure, ValidationProblem

_LIT_RE = re.compile(r"^[A-Za-z0-9_:.\-/|=+% ]{1,200}$")


def lit(value: str) -> str:
    """A validated, quoted SQL string literal (identifiers, keys, pids)."""
    if not _LIT_RE.match(value):
        raise ValidationProblem("unsafe literal in a QA query", value=value[:80])
    return "'" + value + "'"


def lits(values: Iterable[str]) -> str:
    vs = sorted(set(values))
    return "(" + ", ".join(lit(v) for v in vs) + ")" if vs else "(NULL)"


class MemoryStore:
    """Minimal in-memory artifact store (fixture mode): get/head/open with sha256 verification."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(self, data: bytes, namespace: str, media_type: str = "application/octet-stream") -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.objects[(namespace, sha)] = data
        return sha

    def get(self, namespace: str, sha256: str) -> bytes:
        try:
            data = self.objects[(namespace, sha256)]
        except KeyError:
            raise FileNotFoundError(f"object {namespace}/{sha256} not found") from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)
        return data

    def head(self, namespace: str, sha256: str) -> Any:
        data = self.objects.get((namespace, sha256))
        return None if data is None else len(data)

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]:
        yield self.get(namespace, sha256)


@dataclass
class QaBackend:
    mode: str
    rows: Callable[[str], list[dict[str, Any]]]
    store: Any
    today: date
    closers: list[Callable[[], None]] = field(default_factory=list)

    def read_parquet(self, sha256: str, columns: list[str] | None = None) -> pl.DataFrame:
        return pl.read_parquet(io.BytesIO(self.store.get("normalised", sha256)), columns=columns)

    def object_state(self, namespace: str, sha256: str, *, verify: bool) -> str:
        """``ok`` | ``missing`` | ``corrupt`` (decoded through the store API, re-hashed when ``verify``)."""
        try:
            if self.store.head(namespace, sha256) is None:
                return "missing"
            if verify:
                h = hashlib.sha256()
                for chunk in self.store.open(namespace, sha256):
                    h.update(chunk)
                if h.hexdigest() != sha256:
                    return "corrupt"
        except FileNotFoundError:
            return "missing"
        except IntegrityFailure:
            return "corrupt"
        except Exception:
            return "corrupt"
        return "ok"

    def close(self) -> None:
        for c in self.closers:
            c()


def postgres_backend(
    engine: Any, store: Any, *, today: date | None = None, statement_timeout_s: int = 1800
) -> QaBackend:
    from sqlalchemy import text

    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SET default_transaction_read_only = on"))
    conn.execute(text(f"SET statement_timeout = {int(statement_timeout_s) * 1000}"))

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r._mapping) for r in conn.execute(text(sql))]

    return QaBackend("db", rows, store, today or date.today(), [conn.close])


def fixture_backend(tables: dict[str, pl.DataFrame], store: MemoryStore, *, today: date) -> QaBackend:
    """DuckDB in memory: ``tables`` maps ``schema.table`` → frame (JSON columns as strings)."""
    import duckdb

    con = duckdb.connect(":memory:")
    for name, frame in tables.items():
        schema, table = name.split(".", 1)
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        con.register("_qa_tmp", frame.to_arrow())
        con.execute(f"CREATE TABLE {schema}.{table} AS SELECT * FROM _qa_tmp")
        con.unregister("_qa_tmp")

    def rows(sql: str) -> list[dict[str, Any]]:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    return QaBackend("fixture", rows, store, today, [con.close])
