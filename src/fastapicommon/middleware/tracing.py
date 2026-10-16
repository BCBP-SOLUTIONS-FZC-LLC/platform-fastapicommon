"""HTTP server spans, ported from platform-gincommon's ``TracingMiddleware``
(``internal/adapter/inbound/http/middleware/tracing.go``).

One server span per request, extracted from the inbound W3C ``traceparent`` with the installed
propagator. When the request completes the span is renamed ``<METHOD> <route>`` (the route
template in the registry's Gin syntax, e.g. ``GET /items/:item_id``; a non-standard method is
``HTTP`` and reported as ``http.request.method=_OTHER`` with the truncated original in
``http.request.method_original``), and gets ``http.route``, ``url.path`` (≤ 512 bytes),
``user_agent.original`` (≤ 512), ``server.address`` (≤ 256), ``client.address`` and
``http.response.status_code``. The status is Error for a 5xx, and for an exception that escaped
the app (``status 500``, recorded as an ``exception`` event with its type only — never the
message, which can carry request data).
"""

from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon.logging.sanitize import truncate_utf8
from fastapicommon.metrics.platform import http_method_label, http_route_label
from fastapicommon.middleware.context import client_ip

TRACER_SCOPE = "platform-fastapicommon/http"
MAX_SPAN_ATTR_BYTES = 512
MAX_SPAN_HOST_BYTES = 256
MAX_SPAN_METHOD_BYTES = 32


#: ``value`` truncated to ``max_bytes`` UTF-8 bytes at a character boundary.
truncate_span_attr = truncate_utf8


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def client_address(scope: Scope, headers: dict[str, str]) -> str:
    """Gin's default ``ClientIP()`` (see :func:`fastapicommon.middleware.context.client_ip`)."""
    client = scope.get("client")
    return client_ip(headers, str(client[0]) if client else None) or ""


class TracingMiddleware:
    """Pure ASGI middleware starting the HTTP server span (outermost platform middleware, so the
    request context, logs and problem responses see its trace id)."""

    def __init__(self, app: ASGIApp, scope_name: str = TRACER_SCOPE) -> None:
        self.app = app
        self._scope_name = scope_name or TRACER_SCOPE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        raw_method = scope.get("method", "")
        method, span_method = raw_method, raw_method
        if http_method_label(raw_method) != raw_method:
            method, span_method = "_OTHER", "HTTP"

        parent = propagate.extract(headers)
        tracer = trace.get_tracer(self._scope_name)
        span = tracer.start_span(span_method, context=parent, kind=SpanKind.SERVER)
        token = otel_context.attach(trace.set_span_in_context(span, parent))
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        error: BaseException | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                _annotate(span, scope, headers, method, raw_method, span_method, status_code, error)
            finally:
                span.end()
                otel_context.detach(token)


def _annotate(
    span: Any,
    scope: Scope,
    headers: dict[str, str],
    method: str,
    raw_method: str,
    span_method: str,
    status_code: int,
    error: BaseException | None,
) -> None:
    route = http_route_label(scope)
    span.update_name(f"{span_method} {route}")
    span.set_attributes(
        {
            "http.request.method": method,
            "http.route": route,
            "url.path": truncate_span_attr(scope.get("path", ""), MAX_SPAN_ATTR_BYTES),
            "user_agent.original": truncate_span_attr(
                headers.get("user-agent", ""), MAX_SPAN_ATTR_BYTES
            ),
            "server.address": truncate_span_attr(headers.get("host", ""), MAX_SPAN_HOST_BYTES),
            "client.address": client_address(scope, headers),
        }
    )
    if method != raw_method:
        span.set_attribute(
            "http.request.method_original", truncate_span_attr(raw_method, MAX_SPAN_METHOD_BYTES)
        )
    # An exception that escaped the app is answered 500 by Starlette after this middleware;
    # without the override the span would report 0 (or a status already started).
    code = 500 if error is not None and status_code == 0 else status_code or 500
    span.set_attribute("http.response.status_code", code)
    if error is not None and status_code == 0:
        mark_span_panic(span, error)
    elif code >= 500 and not _already_error(span):
        # UnhandledErrorMiddleware answers an unclassified exception itself and marks the
        # span (exception event, "panic"); keep that status rather than the status text.
        span.set_status(Status(StatusCode.ERROR, _status_text(code)))


def mark_span_panic(span: Any, error: BaseException) -> None:
    """Go's recovered-panic span: an ``exception`` event with the type only (never the message)
    and Error status ``panic``. Used for an exception that escaped the app, and by
    ``UnhandledErrorMiddleware`` for one it answered itself."""
    span.add_event("exception", {"exception.type": type(error).__qualname__})
    span.set_status(Status(StatusCode.ERROR, "panic"))


def _already_error(span: Any) -> bool:
    status = getattr(span, "status", None)
    return getattr(status, "status_code", None) is StatusCode.ERROR


def _status_text(code: int) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""
