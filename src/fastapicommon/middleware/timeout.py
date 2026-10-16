from __future__ import annotations

import asyncio
import contextlib

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon.exceptions.handlers import problem_response
from fastapicommon.metrics.platform import http_route_label, platform_metrics
from fastapicommon.problem import CODE_REQUEST_TIMEOUT, TYPE_SERVICE_UNAVAILABLE

DETAIL_REQUEST_TIMEOUT = "The request timed out."


class TimeoutMiddleware:
    """Cancels requests that have not started their response within ``timeout_seconds`` and
    answers a 503 service-unavailable problem with errorCode ``REQUEST_TIMEOUT`` — the same
    response as gincommon's ``TimeoutMiddleware`` (it was a 504 before RFC 9457 parity). A
    response that has started (e.g. a streaming body) is not cut off.

    Each timeout is counted in ``platform_http_request_timeouts_total`` once platform metrics
    are enabled (Go: ``RecordHTTPTimeout`` in gincommon's ``TimeoutMiddleware``).

    Pure ASGI (a ``BaseHTTPMiddleware`` would end a failed streaming body as a clean 200).
    """

    def __init__(self, app: ASGIApp, timeout_seconds: float = 30.0) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = asyncio.Event()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                started.set()
            await send(message)

        task = asyncio.ensure_future(self.app(scope, receive, send_wrapper))
        waiter = asyncio.ensure_future(started.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=self.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # Cancelled while waiting (client gone, server shutdown): cancel the handler too.
            # asyncio.wait does not; a later ``await task`` propagates cancellation itself.
            task.cancel()
            raise
        finally:
            waiter.cancel()
        if not done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            platform = platform_metrics()
            if platform is not None:
                platform.record_http_timeout(scope.get("method", ""), http_route_label(scope))
            request = Request(scope)
            response = problem_response(
                request,
                TYPE_SERVICE_UNAVAILABLE,
                detail=DETAIL_REQUEST_TIMEOUT,
                error_code=CODE_REQUEST_TIMEOUT,
            )
            await response(scope, receive, send)
            return
        await task  # the result, or the handler's exception
