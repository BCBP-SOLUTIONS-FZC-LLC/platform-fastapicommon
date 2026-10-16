from __future__ import annotations

from opentelemetry import trace
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon._scope import ABORTED, PANICKED
from fastapicommon.exceptions.handlers import (
    log_unclassified,
    problem_trace_id,
    unhandled_exception_handler,
)


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
            request = Request(scope)
            if started:
                # Go: re-panic with http.ErrAbortHandler; the access log and span record an
                # aborted request with the status already sent, not a 200 success.
                scope[ABORTED] = True
                log_unclassified(
                    request,
                    exc,
                    status=500,
                    error_code="INTERNAL_ERROR",
                    trace_id=problem_trace_id(request),
                )
                return
            _record_on_span(exc)
            scope[PANICKED] = True
            response = await unhandled_exception_handler(request, exc)
            await response(scope, receive, send)


def _record_on_span(exc: Exception) -> None:
    from fastapicommon.middleware.tracing import mark_span_panic  # avoid an import cycle

    span = trace.get_current_span()
    if span.is_recording():
        mark_span_panic(span, exc)
