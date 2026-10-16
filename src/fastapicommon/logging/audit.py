"""Request-scoped audit entries, ported from platform-gincommon's auth and authz middleware
(``auth: request rejected`` and ``authz: access denied``, LLD §7.3)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapicommon.context import get_request_context_or_none
from fastapicommon.logging import get_logger
from fastapicommon.logging.sanitize import sanitize_fields, sanitize_log_value

_logger = get_logger("fastapicommon.auth")


def _request_fields() -> dict[str, Any]:
    from fastapicommon.metrics.platform import http_route_label
    from fastapicommon.middleware.logging import current_http_scope

    scope = current_http_scope()
    if scope is None:
        return {}
    return {
        "method": sanitize_log_value(scope.get("method", "")),
        "route": http_route_label(scope),
    }


def log_auth_rejection(reason: str, status: int = 401) -> None:
    """Go ``logAuthRejection``: warn ``auth: request rejected`` with ``reason``, ``method``,
    ``route``, ``status``, ``client_ip``, ``trace_id``, ``request_id`` and the identity the
    request claimed (``claimed_user_id`` / ``claimed_tenant_id``, never as verified fields)."""
    from fastapicommon.auth.headers import TENANT_ID_HEADER, USER_ID_HEADER
    from fastapicommon.middleware.logging import current_http_scope, http_header

    context = get_request_context_or_none()
    fields: dict[str, Any] = {"reason": sanitize_log_value(reason), **_request_fields()}
    fields["status"] = status
    if context is not None:
        fields["client_ip"] = sanitize_log_value(context.client_ip or "")
        fields["trace_id"] = context.trace_id
        fields["request_id"] = context.request_id
    scope = current_http_scope()
    for header, key, value in (
        (USER_ID_HEADER, "claimed_user_id", context and context.user_id),
        (TENANT_ID_HEADER, "claimed_tenant_id", context and context.tenant_id),
    ):
        claimed = value or (sanitize_log_value(http_header(scope, header)) if scope else "")
        if claimed:
            fields[key] = claimed
    _logger.warning("auth: request rejected", **sanitize_fields(fields))


def log_access_denied(action: str, resource: str | None = None, roles: Sequence[str] = ()) -> None:
    """Go ``authz: access denied`` (warn): ``tenant_id``, ``user_id``, ``roles``, ``action``,
    ``resource`` (default: the route template)."""
    context = get_request_context_or_none()
    _logger.warning(
        "authz: access denied",
        tenant_id=(context.tenant_id if context is not None else None) or "",
        user_id=(context.user_id if context is not None else None) or "",
        roles=list(roles or (context.roles if context is not None else ())),
        action=action,
        resource=resource if resource is not None else _request_fields().get("route", ""),
    )
