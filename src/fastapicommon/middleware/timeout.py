from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fastapicommon.context import get_request_context_or_none
from fastapicommon.metrics.platform import http_route_label, platform_metrics


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Cancels requests that exceed ``timeout_seconds`` and returns 504 Gateway Timeout.

    Each timeout is counted in ``platform_http_request_timeouts_total`` once platform metrics
    are enabled (Go: ``RecordHTTPTimeout`` in gincommon's ``TimeoutMiddleware``).
    """

    def __init__(self, app: Starlette, timeout_seconds: float = 30.0) -> None:
        super().__init__(app)
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout_seconds)
        except TimeoutError:
            platform = platform_metrics()
            if platform is not None:
                platform.record_http_timeout(request.method, http_route_label(request.scope))
            # Same {error, status, trace_id, request_id} shape as fastapicommon.exceptions —
            # ContextMiddleware runs outermost (see bootstrap.create_platform_app), so the
            # RequestContext is already bound here.
            context = get_request_context_or_none()
            return JSONResponse(
                status_code=504,
                content={
                    "error": f"Request exceeded the {self.timeout_seconds}s timeout",
                    "status": 504,
                    "trace_id": context.trace_id if context is not None else "",
                    "request_id": context.request_id if context is not None else "",
                },
            )
