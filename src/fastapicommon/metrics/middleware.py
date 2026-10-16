from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from fastapicommon.metrics.platform import (
    PlatformMetrics,
    http_route_label,
    platform_metrics,
    request_body_size,
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Records the ``platform_http_*`` metrics of the Enterprise Platform Observability Standard
    for every request (see :mod:`fastapicommon.metrics.platform`), on the same code path as Go
    gincommon's ``MetricsMiddleware`` — requests, latency, request/response size, in-flight
    requests, 429s, and unhandled exceptions (Go: recovered panics), which count as 5xx in
    ``platform_http_requests_total`` as well as in ``platform_http_panics_total``.

    A pass-through until :func:`~fastapicommon.metrics.init_metrics` has run.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        platform = platform_metrics()
        if platform is None:
            return await call_next(request)
        return await self._dispatch_platform(platform, request, call_next)

    async def _dispatch_platform(
        self, platform: PlatformMetrics, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        method = request.method
        route = http_route_label(request.scope)
        request_size = request_body_size(request.headers.get("content-length"))

        platform.http_active_inc(route)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            platform.record_http(method, route, 500, time.perf_counter() - start, request_size, 0)
            platform.record_http_panic(method, route)
            raise
        finally:
            platform.http_active_dec(route)
        duration = time.perf_counter() - start

        status_code = response.status_code
        response_size = _content_length(response)
        platform.record_http(method, route, status_code, duration, request_size, response_size)
        if response_size is None:
            _count_body(
                response,
                lambda size: platform.observe_http_response_size(method, route, status_code, size),
            )
        return response


def _content_length(response: Response) -> float | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return float(max(0, int(raw)))
    except ValueError:
        return None


def _count_body(response: Response, on_done: Callable[[float], None]) -> None:
    """Without a Content-Length (a streaming body), count bytes as they are sent and observe the
    total when the body ends — Go observes the bytes actually written."""
    body: Any = getattr(response, "body_iterator", None)
    if body is None:
        on_done(0.0)
        return

    async def counting() -> AsyncIterator[Any]:
        total = 0
        try:
            async for chunk in body:
                total += len(chunk)
                yield chunk
        finally:
            on_done(float(total))

    response.body_iterator = counting()  # type: ignore[attr-defined]
