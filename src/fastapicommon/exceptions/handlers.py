from __future__ import annotations

import contextlib
import json
import traceback
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapicommon.context import get_request_context_or_none
from fastapicommon.exceptions.errors import PlatformException
from fastapicommon.logging import get_logger
from fastapicommon.problem import (
    CODE_ROUTE_NOT_FOUND,
    PROBLEM_MEDIA_TYPE,
    TYPE_INTERNAL_ERROR,
    TYPE_NOT_IMPLEMENTED,
    TYPE_VALIDATION_FAILED,
    FieldError,
    ProblemType,
    build_problem,
    resolve_trace_id,
    type_for_status,
)
from fastapicommon.problem.validation import validation_problem_parts

_problem_log = get_logger("fastapicommon.problem")
_unclassified_log = get_logger("fastapicommon.exceptions")

DETAIL_ROUTE_NOT_FOUND = "No route matches the request path."
DETAIL_METHOD_NOT_ALLOWED = "The route does not support the request method."


@dataclass(frozen=True, slots=True)
class ProblemSettings:
    """Per-app problem rendering settings (stored on ``app.state.fastapicommon_problem``)."""

    type_base: str = ""
    service: str = ""


def _settings(request: Request) -> ProblemSettings:
    s = getattr(request.app.state, "fastapicommon_problem", None)
    return s if isinstance(s, ProblemSettings) else ProblemSettings()


def problem_response(
    request: Request,
    t: ProblemType,
    *,
    detail: str | None = None,
    error_code: str | None = None,
    validation_errors: Iterable[FieldError] = (),
) -> JSONResponse:
    """Render ``t`` as an ``application/problem+json`` response for ``request`` and log it once
    at WARN as ``problem response`` (the same fields as gincommon and pgcommon-py).

    ``traceId`` follows gincommon's order (traceparent → sanitized x-request-id → span →
    generated); ``instance`` is the path without the query string; ``tenantId`` is set only
    for an authenticated identity and never on a 401.
    """
    s = _settings(request)
    context = get_request_context_or_none()
    trace_id = resolve_trace_id(
        request.headers.get("traceparent"),
        request.headers.get("x-request-id"),
        context.trace_id if context is not None else None,
    )
    tenant_id = None
    if context is not None and context.user.is_authenticated and t.status != 401:
        tenant_id = context.tenant_id
    body = build_problem(
        t,
        base=s.type_base,
        detail=detail,
        error_code=error_code,
        instance=request.url.path,
        trace_id=trace_id,
        tenant_id=tenant_id,
        validation_errors=validation_errors,
    )
    fields: dict[str, object] = {
        "traceId": body["traceId"],
        "type": body["type"],
        "status": body["status"],
        "errorCode": body["errorCode"],
    }
    if s.service:
        fields["service"] = s.service
    if tenant_id:
        fields["tenantId"] = tenant_id
    with contextlib.suppress(Exception):  # a failing logger must not break the response
        _problem_log.warning("problem response", **fields)
    return JSONResponse(status_code=body["status"], content=body, media_type=PROBLEM_MEDIA_TYPE)


async def platform_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlatformException)
    return problem_response(
        request,
        exc.problem_type,
        detail=exc.message,
        error_code=exc.code,
        validation_errors=exc.validation_errors,
    )


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    detail, fields = validation_problem_parts(exc.errors())
    return problem_response(
        request, TYPE_VALIDATION_FAILED, detail=detail, validation_errors=fields
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    t = type_for_status(exc.status_code)
    code = None
    detail: str | None = None
    if "endpoint" not in request.scope and exc.status_code == 404:
        code, detail = CODE_ROUTE_NOT_FOUND, DETAIL_ROUTE_NOT_FOUND
    elif exc.status_code == 405:
        detail = DETAIL_METHOD_NOT_ALLOWED
    elif exc.status_code < 500 and isinstance(exc.detail, str) and exc.detail != t.title:
        # A 4xx detail the service wrote; 5xx details are never sent.
        detail = exc.detail
    response = problem_response(request, t, detail=detail, error_code=code)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def not_implemented_handler(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(request, TYPE_NOT_IMPLEMENTED)


#: Bounds of the "unclassified error" entry (gincommon caps error_chain at 8 too).
MAX_ERROR_CHAIN = 8
MAX_STACK_FRAMES = 30


def _type_name(exc: BaseException) -> str:
    t = type(exc)
    return t.__qualname__ if t.__module__ == "builtins" else f"{t.__module__}.{t.__qualname__}"


def error_chain_types(exc: BaseException, limit: int = MAX_ERROR_CHAIN) -> list[str]:
    """The exception types along ``__cause__`` / ``__context__`` and exception-group
    members, outermost first, at most ``limit`` (gincommon's ``error_chain``). Type names
    are code identifiers, never exception text."""
    out: list[str] = []
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue and len(out) < limit:
        e = queue.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        out.append(_type_name(e))
        if isinstance(e, BaseExceptionGroup):
            queue.extend(e.exceptions)
        nxt = e.__cause__ or (None if e.__suppress_context__ else e.__context__)
        if nxt is not None:
            queue.append(nxt)
    return out


def stack_frames(exc: BaseException, limit: int = MAX_STACK_FRAMES) -> list[str]:
    """The traceback as ``file:line in function`` entries, innermost last, at most
    ``limit`` (the innermost kept). No source lines and no exception message: frames are
    code locations only."""
    frames = traceback.extract_tb(exc.__traceback__)[-limit:]
    return [f"{f.filename}:{f.lineno} in {f.name}" for f in frames]


def _operation(request: Request) -> str:
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return ""
    return f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}".lstrip(
        "."
    )


def log_unclassified(
    request: Request, exc: BaseException, *, status: int, error_code: str, trace_id: str
) -> None:
    """Emit the error-level ``unclassified error`` entry (gincommon's contract): where it
    happened, the exception types and the stack frames — never the exception message."""
    from fastapicommon.metrics.platform import http_route_label  # avoid an import cycle

    context = get_request_context_or_none()
    s = _settings(request)
    chain = error_chain_types(exc)
    fields: dict[str, object] = {
        "classified": False,
        "traceId": trace_id,
        "trace_id": context.trace_id if context is not None else "",
        "request_id": context.request_id if context is not None else "",
        "method": request.method,
        "route": http_route_label(request.scope),
        "operation": _operation(request),
        "status": status,
        "errorCode": error_code,
        "error_type": chain[0] if chain else "",
        "error_chain": chain,
        "stack": stack_frames(exc),
    }
    if s.service:
        fields["service"] = s.service
    with contextlib.suppress(Exception):  # a failing logger must not break the response
        _unclassified_log.error("unclassified error", **fields)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Any exception nothing classified: a generic 500 internal-error, and one error-level
    ``unclassified error`` entry — the same contract as gincommon's (LLD OQ-24). It records
    where the failure happened and the exception types and stack frames, **never the
    exception message**, which may carry SQL, PII, tokens or secrets. An unclassified 500
    is a programming defect: classify the failure (raise a ``PlatformException``) where it
    happens. Install ``UnhandledErrorMiddleware`` (``create_platform_app`` does) so the
    exception is not re-raised to the server, which would log its message."""
    response = problem_response(request, TYPE_INTERNAL_ERROR)
    body = json.loads(bytes(response.body))
    log_unclassified(
        request,
        exc,
        status=body["status"],
        error_code=body["errorCode"],
        trace_id=body.get("traceId", ""),
    )
    return response


def register_exception_handlers(app: FastAPI, *, type_base: str = "", service: str = "") -> None:
    """Register the platform's single global exception handler set on a FastAPI app: every
    error becomes an RFC 9457 problem. Called by ``create_platform_app``.

    Mapping: PlatformException → its type; RequestValidationError → 400 validation-failed
    with validationErrors; HTTPException → the platform type for its status;
    NotImplementedError → 501; anything else → 500 internal-error without detail.
    """
    app.state.fastapicommon_problem = ProblemSettings(type_base=type_base, service=service)
    app.add_exception_handler(PlatformException, platform_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(NotImplementedError, not_implemented_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
