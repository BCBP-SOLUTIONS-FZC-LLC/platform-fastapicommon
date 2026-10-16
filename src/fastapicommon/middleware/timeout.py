from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from fastapicommon.exceptions.handlers import problem_response
from fastapicommon.metrics.platform import http_route_label, platform_metrics
from fastapicommon.problem import CODE_REQUEST_TIMEOUT, TYPE_SERVICE_UNAVAILABLE

DETAIL_REQUEST_TIMEOUT = "The request timed out."


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Cancels requests that exceed ``timeout_seconds`` and answers a 503 service-unavailable
    problem with errorCode ``REQUEST_TIMEOUT`` — the same response as gincommon's
    ``TimeoutMiddleware`` (it was a 504 before RFC 9457 parity).

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
            return problem_response(
                request,
                TYPE_SERVICE_UNAVAILABLE,
                detail=DETAIL_REQUEST_TIMEOUT,
                error_code=CODE_REQUEST_TIMEOUT,
            )
