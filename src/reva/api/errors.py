"""RFC 9457-style problem details for every error path.

Request validation errors never echo the submitted input (it may contain a
password). Database errors are mapped to typed problems without internals.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from reva.kernel.errors import RevaError

log = logging.getLogger("reva.api")
PROBLEM_MEDIA_TYPE = "application/problem+json"


class ApiProblem(RevaError):
    """An HTTP-level problem with an explicit status and stable code."""

    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.http_status = status
        self.code = code


class Unauthenticated(ApiProblem):
    def __init__(self, message: str = "authentication required") -> None:
        super().__init__(401, "unauthenticated", message)


class Forbidden(ApiProblem):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(403, "forbidden", message, **details)


class NotFound(ApiProblem):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(404, "not_found", message, **details)


def problem_response(request: Request, problem: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    body = dict(problem)
    body.setdefault("instance", request.url.path)
    rid = getattr(request.state, "request_id", None)
    if rid:
        body.setdefault("request_id", rid)
    return JSONResponse(body, status_code=int(body["status"]), media_type=PROBLEM_MEDIA_TYPE, headers=headers)


def _problem(status: int, code: str, detail: str, **details: Any) -> dict[str, Any]:
    return ApiProblem(status, code, detail, **details).problem()


def _db_message(exc: DBAPIError) -> str:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    msg = getattr(diag, "message_primary", None) or str(orig or exc).splitlines()[0]
    return str(msg)[:300]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RevaError)
    async def _reva(request: Request, exc: RevaError) -> JSONResponse:
        headers = {"Retry-After": str(exc.details["retry_after"])} if "retry_after" in exc.details else None
        return problem_response(request, exc.problem(), headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors()[:50]
        ]
        return problem_response(
            request, _problem(422, "validation_error", "request does not satisfy the contract", errors=errors)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthenticated", 403: "forbidden",
                413: "payload_too_large"}.get(exc.status_code, "http_error")
        return problem_response(request, _problem(exc.status_code, code, str(exc.detail)),
                                dict(exc.headers) if exc.headers else None)

    @app.exception_handler(OperationalError)
    async def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        log.warning("database unavailable: %s", type(exc).__name__)
        return problem_response(request, _problem(503, "database_unavailable", "database is not reachable"))

    @app.exception_handler(DBAPIError)
    async def _db(request: Request, exc: DBAPIError) -> JSONResponse:
        # SECURITY DEFINER functions raise plain exceptions for rejected arguments.
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "P0001":
            return problem_response(request, _problem(422, "rejected_by_database", _db_message(exc)))
        if isinstance(sqlstate, str) and sqlstate.startswith("22"):
            # data exception (bad cast, out of range...): a client parameter problem, not a server fault
            return problem_response(request, _problem(400, "invalid_parameter",
                                                      "a request parameter has an invalid value"))
        if sqlstate == "57014":
            return problem_response(request, _problem(503, "statement_timeout", "query exceeded the time budget"))
        log.exception("database error", extra={"request_id": getattr(request.state, "request_id", None)})
        return problem_response(request, _problem(500, "database_error", "database error"))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled error", extra={"request_id": rid})
        response = problem_response(request, _problem(500, "internal_error", "internal error"))
        if rid:
            # Outside the header middleware: correlate the 500 with the logs here.
            response.headers["X-Request-ID"] = rid
        # This handler runs in the outermost (server error) middleware, outside the header middleware:
        # apply the same security headers here so 500 pages are not the one response without them.
        apply = getattr(request.app.state, "apply_security_headers", None)
        if apply is not None:
            apply(response.headers, request.url.path)
        return response
