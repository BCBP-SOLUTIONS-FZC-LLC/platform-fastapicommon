from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Cancels requests that exceed ``timeout_seconds`` and returns 504 Gateway Timeout."""

    def __init__(self, app: Starlette, timeout_seconds: float = 30.0) -> None:
        super().__init__(app)
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout_seconds)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "code": "gateway_timeout",
                    "message": f"Request exceeded the {self.timeout_seconds}s timeout",
                },
            )
