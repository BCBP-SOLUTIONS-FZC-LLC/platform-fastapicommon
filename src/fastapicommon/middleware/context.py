from __future__ import annotations

import uuid
from collections.abc import Mapping

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.auth.validation import normalize_header_value, normalize_request_id, parse_roles
from fastapicommon.context import (
    CurrentUser,
    RequestContext,
    bind_request_context,
    reset_request_context,
)

#: Exact casing matches Go's gincommon response headers (`internal/core/domain/transport.go`) —
#: a shared dashboard/client parsing either language's response must find the same header names.
TRACE_ID_RESPONSE_HEADER = "X-Trace-ID"
REQUEST_ID_RESPONSE_HEADER = "X-Request-ID"


def client_ip(headers: Mapping[str, str], peer: str | None) -> str | None:
    """Gin's default (no custom trusted-proxy config) ``c.ClientIP()``: the first
    ``X-Forwarded-For`` entry, then ``X-Real-IP``, then the direct socket ``peer``. ``headers``
    is keyed by lowercase name. Shared by the request context and the server span."""
    first = headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if first:
        return first
    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return peer


def _client_ip(request: Request) -> str | None:
    return client_ip(request.headers, request.client.host if request.client is not None else None)


def _current_trace_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return trace.format_trace_id(span_context.trace_id)


class ContextMiddleware(BaseHTTPMiddleware):
    """Populates the platform RequestContext from inbound auth/tracing headers for the
    duration of the request, generating request/correlation ids when absent, and echoes
    request/trace ids back on the response.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        headers = request.headers
        request_id = normalize_request_id(headers.get(REQUEST_ID_HEADER)) or str(uuid.uuid4())
        correlation_id = headers.get(CORRELATION_ID_HEADER) or request_id
        roles = parse_roles(headers.get(TENANT_ROLES_HEADER))

        user = CurrentUser(
            user_id=normalize_header_value(headers.get(USER_ID_HEADER)),
            tenant_id=normalize_header_value(headers.get(TENANT_ID_HEADER)),
            roles=roles,
        )
        context = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            user=user,
            trace_id=_current_trace_id(),
            client_ip=_client_ip(request),
        )

        token = bind_request_context(context)
        try:
            response = await call_next(request)
        finally:
            reset_request_context(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        response.headers[REQUEST_ID_RESPONSE_HEADER] = request_id
        if context.trace_id:
            response.headers[TRACE_ID_RESPONSE_HEADER] = context.trace_id
        return response
