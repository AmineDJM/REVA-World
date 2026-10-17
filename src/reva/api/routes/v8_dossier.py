"""V8 Dossier (agent V8DOSSIER): one-click briefs — country, commodity, disease, drug, conflict, decision.

* ``GET /v1/v8/dossier/subjects?q=&kind=`` — featured subjects of every brief template + lookups.
* ``GET /v1/v8/dossier/{kind}/{id}?format=json|md|html`` — the brief: sections of templated sentences filled with
  sourced values (every value footnoted with source, licence, period, reference), tables, chart specs, explicit
  Missing blocks, "ce que ce dossier ne couvre pas". ``md`` and ``html`` (print stylesheet: print to PDF) are exports
  of the same document. Cached by (template version, data snapshot id); ``refresh=true`` recomputes.

Templates are declarative (``registry/dossier/<kind>.yaml``); the generator (``reva.v8.dossier``) never writes free
text. Read-only, ``read`` scope.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import get_engine, read_conn
from reva.api.errors import NotFound
from reva.v8.dossier import model as M

router = APIRouter(prefix="/v1/v8/dossier", tags=["v8-dossier"], dependencies=[Depends(require_scope("read"))])

KIND_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"
ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,120}$"


def _store(request: Request) -> object:
    st = getattr(request.app.state, "atlas_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.atlas_store = st
    return st


@router.get("/subjects", response_model=M.SubjectList)
def dossier_subjects(
    request: Request,
    q: str | None = Query(None, max_length=120, description="Text to look up (name, ISO3, id)"),
    kind: str | None = Query(None, pattern=KIND_PATTERN),
    limit: int = Query(20, ge=1, le=50),
) -> M.SubjectList:
    """Featured briefs and subject lookups (countries, KG diseases / drugs / UCDP conflicts, reference decisions)."""
    from reva.v8.dossier.subjects import search

    if not q:
        return search(None, None, kind, limit)
    with read_conn(request) as conn:
        return search(conn, q, kind, limit)


@router.get(
    "/{kind}/{sid}",
    response_model=M.Document,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 200: {"content": {"text/markdown": {}, "text/html": {}}}},
)
def dossier_document(
    request: Request,
    kind: str = Path(..., pattern=KIND_PATTERN),
    sid: str = Path(..., pattern=ID_PATTERN),
    format: Literal["json", "md", "html"] = Query("json", description="json (document), md (Markdown), html (print)"),
    refresh: bool = Query(False, description="Recompute even if a cached brief exists for this data snapshot"),
) -> Response | M.Document:
    """The brief of one subject (cached per template version and data snapshot)."""
    from reva.v8.dossier.export import to_html, to_markdown
    from reva.v8.dossier.generator import UnknownSubject, generate

    origin = "live"
    engine = get_engine(request)
    try:
        doc = generate(engine.connect, _store(request), kind, sid, origin=origin, use_cache=not refresh)
    except UnknownSubject as e:
        raise NotFound(str(e), kind=kind, id=sid) from e
    fname = f"reva-dossier-{kind}-{sid.replace(':', '_')}"
    if format == "md":
        return PlainTextResponse(
            to_markdown(doc),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="{fname}.md"'},
        )
    if format == "html":
        return HTMLResponse(to_html(doc), headers={"Content-Disposition": f'inline; filename="{fname}.html"'})
    return doc
