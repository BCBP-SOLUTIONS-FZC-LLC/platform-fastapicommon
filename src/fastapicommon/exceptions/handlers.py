from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapicommon.exceptions.errors import PlatformException

_logger = logging.getLogger("fastapicommon.exceptions")


async def platform_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlatformException)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"code": "validation_error", "message": str(exc)},
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": "http_error", "message": str(exc.detail)},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.exception("Unhandled exception while processing request", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": "internal_error", "message": "An unexpected error occurred"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register platform exception handlers on a FastAPI app.

    Called automatically by fastapicommon.bootstrap.create_platform_app.
    """
    app.add_exception_handler(PlatformException, platform_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
