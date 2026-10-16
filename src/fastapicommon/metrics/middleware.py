from __future__ import annotations

import threading
import time

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon.logging import get_logger
from fastapicommon.logging.sanitize import sanitize_log_value
from fastapicommon.metrics.platform import (
    http_route_label,
    platform_metrics,
    request_body_size,
)

_logger = get_logger("fastapicommon.metrics")

#: Go ``MetricsMiddleware``: at most one ``rate limited`` warning per route per second.
RATE_LIMIT_LOG_INTERVAL_SECONDS = 1.0


class _RateLimitLog:
    """One ``rate limited`` warn entry per route per interval; the entries skipped since the
    last one are reported as ``suppressed``."""

    def __init__(self, interval: float = RATE_LIMIT_LOG_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._state: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def __call__(self, route: str, method: str, status: int) -> None:
        now = time.monotonic()
        with self._lock:
            last, suppressed = self._state.get(route, (float("-inf"), 0))
            if now - last < self._interval:
                self._state[route] = (last, suppressed + 1)
                return
            self._state[route] = (now, 0)
        _logger.warning(
            "rate limited",
            route=route,
            method=sanitize_log_value(method),
            status=status,
            suppressed=suppressed,
        )


_log_rate_limited = _RateLimitLog()


class PrometheusMiddleware:
    """Records the ``platform_http_*`` metrics of the Enterprise Platform Observability Standard
    for every request (see :mod:`fastapicommon.metrics.platform`), on the same code path as Go
    gincommon's ``MetricsMiddleware`` — requests, latency, request/response size, in-flight
    requests, 429s, and unhandled exceptions (Go: recovered panics), which count as 5xx in
    ``platform_http_requests_total`` as well as in ``platform_http_panics_total``.

    Pure ASGI (a ``BaseHTTPMiddleware`` would end a failed streaming body as a clean 200).
    Recorded when the request finishes, as in Go: the latency includes the body, the response
    size is the bytes sent. An exception after the response started is recorded with the status
    already sent and is not a panic (Go: an ``http.ErrAbortHandler`` abort).

    A pass-through until :func:`~fastapicommon.metrics.init_metrics` has run.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        platform = platform_metrics() if scope["type"] == "http" else None
        if platform is None:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        route = http_route_label(scope)
        request_size = request_body_size(Headers(scope=scope).get("content-length"))
        status = 0
        response_size = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_size
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                response_size += len(message.get("body", b""))
            await send(message)

        platform.http_active_inc(route)
        start = time.perf_counter()
        raised = False
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            raised = True
            raise
        finally:
            platform.http_active_dec(route)
            code = status or 500
            if raised and not status:
                platform.record_http_panic(method, route)
            platform.record_http(
                method, route, code, time.perf_counter() - start, request_size, float(response_size)
            )
            if code == 429:
                _log_rate_limited(route, method, code)
