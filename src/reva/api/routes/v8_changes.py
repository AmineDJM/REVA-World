"""V8 change feed (agent V8API; contract ``docs/dev/v8_api_interface.md`` §2).

``GET /v1/v8/changes?since=&until=&kinds=&limit=&cursor=`` — what changed in REVA, newest first: ingests (atlas
tables and datasets, revisions flagged), atlas snapshots, KG batches, causal-claim loads, effect estimates added /
retired, sealed forecast batches, resolved questions (with the official forecaster's score when one was sealed),
evaluations and open owner decisions. Typed items with counts read from the database, refs (for watch rules) and
links. Read-only (``read`` scope). Results are cached for 20 s per query (``until`` defaults to now).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, read_conn, require_aware
from reva.api.errors import ApiProblem
from reva.v8.changes.feed import CHANGE_KINDS, ChangeQuery, decode_cursor, read_changes

router = APIRouter(prefix="/v1/v8/changes", tags=["v8-changes"], dependencies=[Depends(require_scope("read"))])

CACHE_TTL_S = 20.0
NOTE = s.ApiWarning(
    code="recorded_facts_only",
    message="Each item reports what the database recorded (server time, counts); nothing is inferred. Effect "
    "estimates and causal claims are attributed to their sources; forecasts are labelled by their judged status.",
)


class ChangeLink(s.ApiModel):
    label: str
    href: str


class ChangeRef(s.ApiModel):
    type: str
    id: str


class ChangeItem(s.ApiModel):
    id: str
    kind: Literal[
        "ingest",
        "atlas_snapshot",
        "kg_batch",
        "causal_claims",
        "effects",
        "forecast_batch",
        "question_resolved",
        "evaluation",
        "owner_decision",
    ]
    at: str = Field(description="Server recorded_at (UTC)")
    title: str = Field(description="French, one line")
    summary: str | None = None
    source: str | None = None
    severity: Literal["info", "notable", "action"]
    counts: dict[str, int] = Field(default_factory=dict)
    refs: list[ChangeRef] = Field(default_factory=list)
    links: list[ChangeLink] = Field(default_factory=list)
    origin: Literal["live", "test_fixture"] = "live"
    detail: dict[str, Any] = Field(default_factory=dict)


class ChangesData(s.ApiModel):
    since: str
    until: str
    items: list[ChangeItem]
    counts: dict[str, int] = Field(description="Items per kind in [since, until] (all pages)")
    kind_labels: dict[str, str]


class ChangesResponse(s.Envelope):
    data: ChangesData


_LOCK = threading.Lock()
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def _store(request: Request) -> Any:
    st = getattr(request.app.state, "forecast_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.forecast_store = st
    return st


def _origin(request: Request) -> str | None:
    prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
    return "live" if prod else None


@router.get("", response_model=ChangesResponse, responses={422: {"model": s.Problem}})
def changes(
    request: Request,
    since: datetime | None = Query(None, description="Items recorded after this time (default: 7 days ago)"),
    until: datetime | None = Query(None, description="Items recorded up to this time (default: now)"),
    kinds: str | None = Query(None, max_length=300, pattern=r"^[a-z_]+(,[a-z_]+)*$"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=400),
) -> ChangesResponse:
    t = Timer()
    require_aware(since, "since")
    require_aware(until, "until")
    kind_list = [k for k in (kinds or "").split(",") if k]
    bad = [k for k in kind_list if k not in CHANGE_KINDS]
    if bad:
        raise ApiProblem(422, "validation_error", f"unknown change kinds: {', '.join(bad)}", allowed=list(CHANGE_KINDS))
    if cursor:
        try:
            decode_cursor(cursor)
        except Exception:
            raise ApiProblem(422, "validation_error", "invalid cursor") from None
    now = datetime.now(UTC)
    start = since or (now - timedelta(days=7))
    if until is not None and until <= start:
        raise ApiProblem(422, "validation_error", "until must be after since")
    origin = _origin(request)
    key = (
        start.isoformat() if since else "7d",
        until.isoformat() if until else None,
        tuple(kind_list),
        limit,
        cursor,
        origin,
    )
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        res = hit[1]
    else:
        with read_conn(request) as conn:
            res = read_changes(
                conn,
                ChangeQuery(
                    since=start, until=until, kinds=kind_list or None, limit=limit, cursor=cursor, origin=origin
                ),
                store=_store(request),
            )
        with _LOCK:
            if len(_CACHE) > 256:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic(), res)
    origins = {i["origin"] for i in res["items"]}
    org = "none" if not origins else ("mixed" if len(origins) > 1 else origins.pop())
    env: dict[str, Any] = {
        "context_mode": "operations",
        "origin": org,
        "modality": "metadata",
        "warnings": [NOTE],
        "cost": t.cost(len(res["items"])),
        "next_cursor": res["next_cursor"],
    }
    data = ChangesData(
        since=res["since"],
        until=res["until"],
        items=[ChangeItem(**i) for i in res["items"]],
        counts=res["counts"],
        kind_labels=res["kind_labels"],
    )
    return ChangesResponse(data=data, **env)
