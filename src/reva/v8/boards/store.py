"""Board persistence over ``ops.v8_board`` (reads: SELECT; writes: the 0020 SECURITY DEFINER functions)."""

from __future__ import annotations

import functools
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from reva.v8.boards.models import BoardDoc

COLS = "id, title, doc, version, created_at, updated_at"


class StoreUnavailable(Exception):
    """``ops.v8_board`` does not exist (migration 0020_v8_boards not applied)."""


class Conflict(Exception):
    def __init__(self, current: dict[str, Any] | None) -> None:
        super().__init__("version conflict")
        self.current = current


class Exists(Exception):
    pass


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(v)


def to_board(row: Any) -> dict[str, Any]:
    r = dict(row)
    doc = r["doc"] if isinstance(r["doc"], dict) else json.loads(r["doc"])
    return {
        "id": r["id"],
        "title": r["title"],
        "items": doc.get("items", []),
        "rules": doc.get("rules", []),
        "created_at": _iso(r["created_at"]),
        "updated_at": _iso(r["updated_at"]),
        "version": int(r["version"]),
    }


def _guard[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapped(*a: P.args, **kw: P.kwargs) -> R:
        try:
            return fn(*a, **kw)
        except ProgrammingError as e:
            if "v8_board" in str(e.orig) and "does not exist" in str(e.orig):
                raise StoreUnavailable from None
            raise

    return wrapped


@_guard
def list_boards(conn: Connection, owner: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(f"SELECT {COLS} FROM ops.v8_board WHERE owner = :o ORDER BY updated_at DESC LIMIT 500"), {"o": owner}
    ).mappings()
    out = []
    for r in rows:
        b = to_board(r)
        out.append(
            {
                "id": b["id"],
                "title": b["title"],
                "n_items": len(b["items"]),
                "n_rules": len(b["rules"]),
                "updated_at": b["updated_at"],
                "version": b["version"],
            }
        )
    return out


@_guard
def get_board(conn: Connection, owner: str, board_id: str) -> dict[str, Any] | None:
    r = (
        conn.execute(text(f"SELECT {COLS} FROM ops.v8_board WHERE owner = :o AND id = :i"), {"o": owner, "i": board_id})
        .mappings()
        .first()
    )
    return to_board(r) if r else None


def _doc(d: BoardDoc) -> str:
    j = d.model_dump(mode="json", exclude_none=True)
    return json.dumps({"items": j["items"], "rules": j["rules"]}, ensure_ascii=False)


def new_id() -> str:
    return "b_" + secrets.token_hex(8)


@_guard
def create_board(conn: Connection, owner: str, d: BoardDoc, board_id: str | None = None) -> dict[str, Any]:
    bid = board_id or new_id()
    try:
        with conn.begin_nested():
            j = conn.execute(
                text("SELECT ops.v8_board_create(:o, :i, :t, CAST(:d AS jsonb))"),
                {"o": owner, "i": bid, "t": d.title.strip(), "d": _doc(d)},
            ).scalar_one()
    except IntegrityError:
        raise Exists from None
    return to_board(j)


@_guard
def update_board(conn: Connection, owner: str, board_id: str, d: BoardDoc, version: int) -> dict[str, Any] | None:
    """New board, None if absent; raises Conflict (with the current board) when ``version`` is stale."""
    j = conn.execute(
        text("SELECT ops.v8_board_update(:o, :i, :t, CAST(:d AS jsonb), :v)"),
        {"o": owner, "i": board_id, "t": d.title.strip(), "d": _doc(d), "v": version},
    ).scalar_one()
    if j is not None:
        return to_board(j)
    cur = get_board(conn, owner, board_id)
    if cur is None:
        return None
    raise Conflict(cur)


@_guard
def delete_board(conn: Connection, owner: str, board_id: str) -> bool:
    return bool(conn.execute(text("SELECT ops.v8_board_delete(:o, :i)"), {"o": owner, "i": board_id}).scalar_one())
