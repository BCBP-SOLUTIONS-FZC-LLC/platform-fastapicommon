from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapicommon.context import get_request_context_or_none
from fastapicommon.exceptions.errors import PlatformException

_logger = logging.getLogger("fastapicommon.exceptions")


def _error_response(status_code: int, error: str) -> JSONResponse:
    """Build the platform error body — ``{error, status, trace_id, request_id}``, matching the
    Go ``gincommon`` library's ``ErrorResponse`` shape exactly (field names, types, and the set
    of fields). ``request_id``/``trace_id`` come from the ``RequestContext`` bound by
    ``ContextMiddleware``, which is still bound at this point: Starlette's ``ExceptionMiddleware``
    (which invokes this handler) runs *inside* ``ContextMiddleware``'s ``call_next()``, before its
    ``finally`` block resets the context.
    """
    context = get_request_context_or_none()
    body = {
        "error": error,
        "status": status_code,
        "trace_id": context.trace_id if context is not None else "",
        "request_id": context.request_id if context is not None else "",
    }
    return JSONResponse(status_code=status_code, content=body)


async def platform_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlatformException)
    return _error_response(exc.status_code, exc.message)


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return _error_response(exc.status_code, str(exc.detail))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.exception("Unhandled exception while processing request", exc_info=exc)
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred")


def register_exception_handlers(app: FastAPI) -> None:
    """Register platform exception handlers on a FastAPI app.

    Called automatically by fastapicommon.bootstrap.create_platform_app.
    """
    app.add_exception_handler(PlatformException, platform_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
