from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon.exceptions.handlers import log_unclassified, unhandled_exception_handler


class UnhandledErrorMiddleware:
    """Answer an exception nothing classified with the 500 problem, and do **not**
    re-raise it.

    Starlette's ``ServerErrorMiddleware`` calls the ``Exception`` handler and then always
    re-raises, so the server (uvicorn: "Exception in ASGI application") logs the full
    traceback *with the exception message*, which may carry SQL, PII, tokens or secrets.
    This middleware stops the exception inside the stack, after ``PrometheusMiddleware``
    recorded it as a 500, and logs it once as ``unclassified error`` (types and frames, no
    message). ``create_platform_app`` installs it just inside ``ContextMiddleware`` so the
    request ids are still bound; apps built without it should add it the same way.

    If the response had already started, the problem cannot be sent: the exception is
    logged the same way and the response is left incomplete (the server closes the
    connection), like gincommon's abort after a partial write.

    The exception is also recorded on the active server span (``TracingMiddleware``) as an
    ``exception`` event with its type only, as for a recovered panic in gincommon.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            _record_on_span(exc)
            request = Request(scope)
            if started:
                log_unclassified(request, exc, status=500, error_code="INTERNAL_ERROR", trace_id="")
                return
            response = await unhandled_exception_handler(request, exc)
            await response(scope, receive, send)


def _record_on_span(exc: Exception) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.add_event("exception", {"exception.type": type(exc).__qualname__})
    span.set_status(Status(StatusCode.ERROR, "panic"))
