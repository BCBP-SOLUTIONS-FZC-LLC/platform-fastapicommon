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

from collections.abc import Mapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon._errors import add_exception_event
from fastapicommon._scope import PANICKED, request_outcome
from fastapicommon.logging.sanitize import truncate_utf8
from fastapicommon.metrics.platform import http_method_label, http_route_label
from fastapicommon.middleware.context import client_ip

TRACER_SCOPE = "platform-fastapicommon/http"
MAX_SPAN_ATTR_BYTES = 512
MAX_SPAN_HOST_BYTES = 256
MAX_SPAN_METHOD_BYTES = 32


#: ``value`` truncated to ``max_bytes`` UTF-8 bytes at a character boundary.
truncate_span_attr = truncate_utf8


def client_address(scope: Scope, headers: Mapping[str, str]) -> str:
    """Gin's default ``ClientIP()`` (see :func:`fastapicommon.middleware.context.client_ip`)."""
    client = scope.get("client")
    return client_ip(headers, str(client[0]) if client else None) or ""


class TracingMiddleware:
    """Pure ASGI middleware starting the HTTP server span (outermost platform middleware, so the
    request context, logs and problem responses see its trace id)."""

    def __init__(self, app: ASGIApp, scope_name: str = TRACER_SCOPE) -> None:
        self.app = app
        self._scope_name = scope_name or TRACER_SCOPE
        # Obtained once: the global (proxy / delegating) tracer follows a provider installed or
        # swapped later, so there is no need to build one per request.
        self._tracer = trace.get_tracer(self._scope_name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_method = scope.get("method", "")
        method, span_method = raw_method, raw_method
        if http_method_label(raw_method) != raw_method:
            method, span_method = "_OTHER", "HTTP"

        http_route_label(scope)  # resolve before routing rewrites the scope (Mount)
        parent = propagate.extract(headers)
        span = self._tracer.start_span(span_method, context=parent, kind=SpanKind.SERVER)
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
    headers: Mapping[str, str],
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
    code, aborted, panicked = request_outcome(scope, status_code, error)
    span.set_attribute("http.response.status_code", code)
    if aborted:
        # Go spanAttrAborted: annotated, not an Error status (a cut-short or cancelled request).
        span.set_attribute("http.aborted", True)
        span.add_event("http.aborted")
    elif panicked and error is not None:
        mark_span_panic(span, error)
    elif code >= 500 and not scope.get(PANICKED):
        # UnhandledErrorMiddleware answers an unclassified exception itself and marks the
        # span (exception event, "panic"); keep that status rather than the status text.
        span.set_status(Status(StatusCode.ERROR, _status_text(code)))


def mark_span_panic(span: Any, error: BaseException) -> None:
    """Go's recovered-panic span: an ``exception`` event with the type only (never the message)
    and Error status ``panic``. Used for an exception that escaped the app, and by
    ``UnhandledErrorMiddleware`` for one it answered itself."""
    add_exception_event(span, error)
    span.set_status(Status(StatusCode.ERROR, "panic"))


def _status_text(code: int) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""
