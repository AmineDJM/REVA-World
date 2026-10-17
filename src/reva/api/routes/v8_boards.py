"""V8 boards & watch rules (agent V8API; contract ``docs/dev/v8_api_interface.md`` §3).

A board is an ordered list of pinned references (entity, series, forecast question, decision, note) plus watch
rules, owned by the authenticated principal (its audit identity). Boards hold references only, never values.
Persisted in ``ops.v8_board`` (migration ``0020_v8_boards``), written only through its SECURITY DEFINER functions.

* ``GET /v1/v8/boards`` · ``POST /v1/v8/boards`` · ``GET|POST|DELETE /v1/v8/boards/{id}`` (POST = full update with
  the expected ``version``; 409 with the current board when stale)
* ``POST /v1/v8/boards/{id}/rules`` · ``DELETE /v1/v8/boards/{id}/rules/{rule_id}``
* ``GET /v1/v8/boards/{id}/alerts?since=`` — rules evaluated against the change feed (thresholds against the latest
  observed atlas value; projections never count; no observed value → ``not_evaluable``, never 0).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import Field
from sqlalchemy import Connection

from reva.api import schemas as s
from reva.api.auth import Principal, require_scope
from reva.api.deps import Timer, read_conn, require_aware
from reva.api.errors import ApiProblem, NotFound
from reva.v8.boards import alerts as A
from reva.v8.boards import store as S
from reva.v8.boards.models import (
    ID_RE,
    Board,
    BoardCreate,
    BoardDoc,
    BoardSummary,
    BoardUpdate,
    RuleCreate,
    rule_problem,
)
from reva.v8.changes.feed import ChangeQuery, read_changes

router = APIRouter(prefix="/v1/v8/boards", tags=["v8-boards"])
READ = Depends(require_scope("read"))
BOARD_ID = Path(..., pattern=ID_RE)
RULE_ID = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
REFERENCE_NOTE = s.ApiWarning(
    code="references_only",
    message="A board stores references (ids, concept + country, question ids); values are always read live with "
    "their source, never stored in the board.",
)


class BoardResponse(s.Envelope):
    data: Board


class BoardList(s.ApiModel):
    boards: list[BoardSummary]


class BoardListResponse(s.Envelope):
    data: BoardList


class Alert(s.ApiModel):
    rule_id: str
    item_uid: str
    kind: Literal["new_data", "revision", "resolution", "threshold"]
    at: str | None
    message: str
    change: dict[str, Any] | None = None
    observed: dict[str, Any] | None = Field(None, description="threshold: latest observed value, period, source")
    status: Literal["fired", "not_fired", "not_evaluable"]
    reason: str | None = None
    item_label: str | None = None


class Alerts(s.ApiModel):
    since: str
    evaluated_at: str
    alerts: list[Alert]
    n_rules: int


class AlertsResponse(s.Envelope):
    data: Alerts


def _env(t: Timer, rows: int) -> dict[str, Any]:
    return {
        "context_mode": "operations",
        "origin": "none",
        "modality": "metadata",
        "warnings": [REFERENCE_NOTE],
        "cost": t.cost(rows),
    }


@contextmanager
def _write(request: Request) -> Iterator[Connection]:
    with request.app.state.engine().begin() as conn:
        yield conn


def _unavailable() -> ApiProblem:
    return ApiProblem(
        503, "boards_store_missing", "Board storage is not installed on this database (migration 0020_v8_boards)."
    )


def _owner(p: Principal) -> str:
    return p.actor[:200]


@router.get("", response_model=BoardListResponse, responses={503: {"model": s.Problem}})
def list_boards(request: Request, principal: Principal = READ) -> BoardListResponse:
    t = Timer()
    try:
        with read_conn(request) as conn:
            rows = S.list_boards(conn, _owner(principal))
    except S.StoreUnavailable:
        raise _unavailable() from None
    return BoardListResponse(data=BoardList(boards=[BoardSummary(**r) for r in rows]), **_env(t, len(rows)))


@router.post(
    "",
    response_model=BoardResponse,
    status_code=201,
    responses={409: {"model": s.Problem}, 422: {"model": s.Problem}, 503: {"model": s.Problem}},
)
def create_board(request: Request, body: BoardCreate, principal: Principal = READ) -> BoardResponse:
    t = Timer()
    try:
        with _write(request) as conn:
            b = S.create_board(conn, _owner(principal), body, body.id)
    except S.Exists:
        raise ApiProblem(409, "board_exists", "a board with this id already exists", board_id=body.id) from None
    except S.StoreUnavailable:
        raise _unavailable() from None
    return BoardResponse(data=Board(**b), **_env(t, 1))


def _get(request: Request, owner: str, board_id: str) -> dict[str, Any]:
    try:
        with read_conn(request) as conn:
            b = S.get_board(conn, owner, board_id)
    except S.StoreUnavailable:
        raise _unavailable() from None
    if b is None:
        raise NotFound("board not found", board_id=board_id)
    return b


@router.get("/{board_id}", response_model=BoardResponse, responses={404: {"model": s.Problem}})
def get_board(request: Request, board_id: str = BOARD_ID, principal: Principal = READ) -> BoardResponse:
    t = Timer()
    return BoardResponse(data=Board(**_get(request, _owner(principal), board_id)), **_env(t, 1))


def _save(request: Request, owner: str, board_id: str, doc: BoardDoc, version: int) -> dict[str, Any]:
    try:
        with _write(request) as conn:
            b = S.update_board(conn, owner, board_id, doc, version)
    except S.Conflict as c:
        raise ApiProblem(
            409,
            "version_conflict",
            "the board changed since this version was read; merge with the current board and retry",
            current=c.current,
        ) from None
    except S.StoreUnavailable:
        raise _unavailable() from None
    if b is None:
        raise NotFound("board not found", board_id=board_id)
    return b


@router.post(
    "/{board_id}",
    response_model=BoardResponse,
    responses={404: {"model": s.Problem}, 409: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def update_board(
    request: Request, body: BoardUpdate, board_id: str = BOARD_ID, principal: Principal = READ
) -> BoardResponse:
    """Full update: ``{title, items, rules, version}`` (version = the one read)."""
    t = Timer()
    b = _save(request, _owner(principal), board_id, body, body.version)
    return BoardResponse(data=Board(**b), **_env(t, 1))


@router.delete("/{board_id}", status_code=204, responses={404: {"model": s.Problem}})
def delete_board(request: Request, board_id: str = BOARD_ID, principal: Principal = READ) -> Response:
    try:
        with _write(request) as conn:
            ok = S.delete_board(conn, _owner(principal), board_id)
    except S.StoreUnavailable:
        raise _unavailable() from None
    if not ok:
        raise NotFound("board not found", board_id=board_id)
    return Response(status_code=204)


def _doc_of(b: dict[str, Any]) -> BoardDoc:
    return BoardDoc.model_validate({"title": b["title"], "items": b["items"], "rules": b["rules"]})


@router.post(
    "/{board_id}/rules",
    response_model=BoardResponse,
    status_code=201,
    responses={404: {"model": s.Problem}, 409: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def add_rule(
    request: Request, body: RuleCreate, board_id: str = BOARD_ID, principal: Principal = READ
) -> BoardResponse:
    t = Timer()
    owner = _owner(principal)
    b = _get(request, owner, board_id)
    pin = next((i for i in _doc_of(b).items if i.uid == body.item_uid), None)
    why = rule_problem(body.kind, body.op, body.value, pin)
    if why:
        raise ApiProblem(422, "invalid_rule", why)
    rule = {
        "id": "r_" + secrets.token_hex(8),
        "kind": body.kind,
        "item_uid": body.item_uid,
        **({"op": body.op} if body.op else {}),
        **({"value": body.value.strip()} if body.value else {}),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    new = BoardDoc.model_validate({"title": b["title"], "items": b["items"], "rules": [*b["rules"], rule]})
    out = _save(request, owner, board_id, new, b["version"])
    return BoardResponse(data=Board(**out), **_env(t, 1))


@router.delete("/{board_id}/rules/{rule_id}", response_model=BoardResponse, responses={404: {"model": s.Problem}})
def delete_rule(
    request: Request, board_id: str = BOARD_ID, rule_id: str = RULE_ID, principal: Principal = READ
) -> BoardResponse:
    t = Timer()
    owner = _owner(principal)
    b = _get(request, owner, board_id)
    rules = [r for r in b["rules"] if r.get("id") != rule_id]
    if len(rules) == len(b["rules"]):
        raise NotFound("rule not found", rule_id=rule_id)
    new = BoardDoc.model_validate({"title": b["title"], "items": b["items"], "rules": rules})
    out = _save(request, owner, board_id, new, b["version"])
    return BoardResponse(data=Board(**out), **_env(t, 1))


def _atlas_query(request: Request) -> Any:
    """The atlas query already open in this process for the latest snapshot (never opened or built here)."""
    try:
        from reva.atlas.query import request_snapshot
        from reva.v8.boards.alerts import _warm_query

        st = getattr(request.app.state, "atlas_store", None)
        if st is None:
            from reva.adapters.objects.factory import build_store

            st = build_store(request.app.state.settings)
            request.app.state.atlas_store = st
        with read_conn(request) as conn:
            snap, _ = request_snapshot(conn, st, origin=None)
        return _warm_query(snap.sha256)
    except Exception:  # no snapshot / store: thresholds are reported not evaluable
        return None


@router.get("/{board_id}/alerts", response_model=AlertsResponse, responses={404: {"model": s.Problem}})
def board_alerts(
    request: Request,
    board_id: str = BOARD_ID,
    since: datetime | None = Query(None, description="Changes recorded after this time (default: 30 days ago)"),
    principal: Principal = READ,
) -> AlertsResponse:
    t = Timer()
    require_aware(since, "since")
    b = _get(request, _owner(principal), board_id)
    now = datetime.now(UTC)
    start = since or (now - timedelta(days=30))
    from reva.api.routes.v8_changes import _store

    with read_conn(request) as conn:
        feed = read_changes(
            conn,
            ChangeQuery(since=start, until=now, kinds=["ingest", "kg_batch", "question_resolved"], limit=100000),
            store=_store(request),
        )
        alerts = A.evaluate(conn, b, feed["items"], atlas=lambda: _atlas_query(request))
    return AlertsResponse(
        data=Alerts(
            since=start.isoformat().replace("+00:00", "Z"),
            evaluated_at=now.isoformat().replace("+00:00", "Z"),
            alerts=[Alert(**a) for a in alerts],
            n_rules=len(b["rules"]),
        ),
        **_env(t, len(alerts)),
    )
