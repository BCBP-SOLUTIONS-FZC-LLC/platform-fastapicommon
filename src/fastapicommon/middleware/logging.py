"""The ``http_request`` access log, ported from platform-gincommon's ``LoggingMiddleware``
(``internal/adapter/inbound/http/middleware/logging.go``).

One entry per request, after it completes: ``trace_id``, ``request_id``, ``method`` (sanitized),
``path`` (the **route template** in the registry's Gin syntax, never the raw URL path),
``status``, ``duration_ms`` (float), and the identity — ``tenant_id`` / ``user_id`` from the
request context the platform middleware validated, or, for identity headers that failed
validation, the sanitized raw values as ``claimed_tenant_id`` / ``claimed_user_id`` (never under
the verified names). Info for 1xx-3xx, warn for 4xx, error for 5xx — an exception that escaped
the app is logged as status 500. Sensitive keys are redacted (``sanitize_fields``).
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapicommon._scope import ABORTED
from fastapicommon.auth.headers import TENANT_ID_HEADER, USER_ID_HEADER
from fastapicommon.context import get_request_context_or_none
from fastapicommon.logging import get_logger
from fastapicommon.logging.sanitize import sanitize_fields, sanitize_log_value
from fastapicommon.metrics.platform import http_route_label

_logger = get_logger("fastapicommon.http")

#: The ASGI scope of the request being handled, so request-scoped audit logs (``auth: request
#: rejected``) can name its method and route.
_current_scope: ContextVar[Scope | None] = ContextVar("fastapicommon_http_scope", default=None)


def current_http_scope() -> Scope | None:
    return _current_scope.get()


def http_header(scope: Scope, name: str) -> str:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return str(value.decode("latin-1"))
    return ""


def identity_log_fields(scope: Scope) -> dict[str, Any]:
    """Go ``addIdentityLogFields``: the validated identity as ``tenant_id`` / ``user_id``; raw
    header values that did not validate as ``claimed_*`` (sanitized, omitted when absent)."""
    fields: dict[str, Any] = {}
    context = get_request_context_or_none()
    for header, verified, claimed, value in (
        (TENANT_ID_HEADER, "tenant_id", "claimed_tenant_id", context and context.tenant_id),
        (USER_ID_HEADER, "user_id", "claimed_user_id", context and context.user_id),
    ):
        if value:
            fields[verified] = value
            continue
        raw = sanitize_log_value(http_header(scope, header))
        if raw:
            fields[claimed] = raw
    return fields


class LoggingMiddleware:
    """Pure ASGI middleware emitting ``http_request`` (inside ``ContextMiddleware``, so the
    request and trace ids are bound)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        http_route_label(scope)  # resolve before routing rewrites the scope (Mount)
        token = _current_scope.set(scope)
        raised = False
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            raised = True
            raise
        finally:
            try:
                self._log(scope, start, 500 if raised and status_code == 0 else status_code)
            finally:
                _current_scope.reset(token)

    @staticmethod
    def _log(scope: Scope, start: float, status: int) -> None:
        context = get_request_context_or_none()
        fields: dict[str, Any] = {
            "trace_id": context.trace_id if context is not None else "",
            "request_id": context.request_id if context is not None else "",
            "method": sanitize_log_value(scope.get("method", "")),
            "path": http_route_label(scope),
            "status": status or 500,
            "duration_ms": (time.perf_counter() - start) * 1000.0,
        }
        fields.update(identity_log_fields(scope))
        safe = sanitize_fields(fields)
        if scope.get(ABORTED):
            # Go: an aborted request is not an error (its failure is logged on its own).
            safe["aborted"] = True
            _logger.debug("http_request", **safe)
        elif fields["status"] >= 500:
            _logger.error("http_request", **safe)
        elif fields["status"] >= 400:
            _logger.warning("http_request", **safe)
        else:
            _logger.info("http_request", **safe)
