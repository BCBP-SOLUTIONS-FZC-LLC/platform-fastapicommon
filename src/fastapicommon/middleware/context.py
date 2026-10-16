from __future__ import annotations

from collections.abc import Iterable, Mapping
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from opentelemetry import trace
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon._scope import CLIENT_IP
from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.auth.validation import resolve_identity, resolve_ids
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


TrustedProxies = tuple[IPv4Network | IPv6Network, ...]


def parse_trusted_proxies(values: str | Iterable[str] | None) -> TrustedProxies:
    """IP addresses or CIDRs (a comma-separated string or an iterable) of the proxies whose
    ``X-Forwarded-For`` / ``X-Real-IP`` are believed (Gin ``SetTrustedProxies``). Raises
    ``ValueError`` on an invalid entry, so a typo fails at startup."""
    if values is None:
        return ()
    items = values.split(",") if isinstance(values, str) else list(values)
    return tuple(ip_network(item.strip(), strict=False) for item in items if item.strip())


def _is_trusted(ip: str, trusted: TrustedProxies) -> bool:
    try:
        address = ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in trusted)


def _ip_from_header(value: str, trusted: TrustedProxies) -> str | None:
    """Gin ``validateHeader``: walk the comma-separated hops from the right (the closest proxy)
    and return the first that is not a trusted proxy (or the leftmost); stop at an invalid IP."""
    if not value:
        return None
    hop = ""
    for hop in reversed([h.strip() for h in value.split(",")]):
        try:
            ip_address(hop)
        except ValueError:
            return None
        if not _is_trusted(hop, trusted):
            return hop
    return hop  # every hop is a trusted proxy: the leftmost


def client_ip(
    headers: Mapping[str, str], peer: str | None, trusted: TrustedProxies = ()
) -> str | None:
    """Gin's ``c.ClientIP()`` with ``SetTrustedProxies(trusted)``: ``X-Forwarded-For``, then
    ``X-Real-IP``, are believed **only** when the socket ``peer`` is a trusted proxy; otherwise
    (and with no trusted proxies, the default) the client IP is the peer, so a client cannot
    spoof it. ``headers`` is keyed by lowercase name. Shared by the request context and the
    server span."""
    if not trusted or peer is None or not _is_trusted(peer, trusted):
        return peer
    for name in ("x-forwarded-for", "x-real-ip"):
        ip = _ip_from_header(headers.get(name, ""), trusted)
        if ip:
            return ip
    return peer


def _current_trace_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return trace.format_trace_id(span_context.trace_id)


class ContextMiddleware:
    """Populates the platform RequestContext from inbound auth/tracing headers for the
    duration of the request, generating request/correlation ids when absent, and echoes
    request/trace ids back on the response.

    Pure ASGI (not ``BaseHTTPMiddleware``): the context stays bound while a streaming body is
    produced, and a response cut short by ``UnhandledErrorMiddleware`` stays incomplete, so the
    server drops the connection instead of this layer terminating the stream as a clean 200.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: str | Iterable[str] | None = None) -> None:
        self.app = app
        self._trusted = parse_trusted_proxies(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        headers = request.headers
        request_id, correlation_id = resolve_ids(
            headers.get(REQUEST_ID_HEADER), headers.get(CORRELATION_ID_HEADER)
        )
        # Every value of each identity header: a repeated one is ambiguous (Go: 401), never
        # first-value-wins; repeated roles are joined and capped (resolve_identity).
        identity = resolve_identity(
            headers.getlist(USER_ID_HEADER),
            headers.getlist(TENANT_ID_HEADER),
            headers.getlist(TENANT_ROLES_HEADER),
        )
        user = CurrentUser(
            user_id=identity.user_id, tenant_id=identity.tenant_id, roles=identity.roles
        )
        peer = request.client.host if request.client is not None else None
        context = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            user=user,
            trace_id=_current_trace_id(),
            client_ip=client_ip(headers, peer, self._trusted),
            identity_rejection=identity.rejection,
        )
        scope[CLIENT_IP] = context.client_ip  # the server span's client.address

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers[REQUEST_ID_HEADER] = request_id
                response_headers[CORRELATION_ID_HEADER] = correlation_id
                response_headers[REQUEST_ID_RESPONSE_HEADER] = request_id
                if context.trace_id:
                    response_headers[TRACE_ID_RESPONSE_HEADER] = context.trace_id
            await send(message)

        token = bind_request_context(context)
        try:
            await self.app(scope, receive, send_with_ids)
        finally:
            reset_request_context(token)
