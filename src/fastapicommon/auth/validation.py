"""Inbound header validation, ported byte-for-byte from `platform-gincommon`'s
`internal/core/domain/validation.go` so a Go-issued identity/request-id/roles header behaves
identically whether it lands on a Go or a Python route.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from fastapicommon.auth.headers import TENANT_ID_HEADER, TENANT_ROLES_SEPARATOR, USER_ID_HEADER

__all__ = [
    "IdentityResolution",
    "InvalidRolesError",
    "normalize_header_value",
    "normalize_request_id",
    "parse_roles",
    "parse_roles_strict",
    "resolve_identity",
]

MAX_HEADER_VALUE_LEN = 256
MAX_ROLE_LEN = 64
MAX_REQUEST_ID_LEN = 128
#: Go ``MaxRolesHeaderLen`` / ``MaxRoles``: the joined x-tenant-roles header and the number of
#: distinct valid roles it may yield.
MAX_ROLES_HEADER_LEN = 4096
MAX_ROLES = 64

_ROLE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# U+2028/U+2029 are deliberately rejected alongside ASCII control characters and DEL — some log
# parsers treat them as newlines, enabling log injection (matches Go's exact rationale/rule).
_FORBIDDEN_CODEPOINTS = frozenset({0x7F, 0x2028, 0x2029})


def _has_control_char(s: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) in _FORBIDDEN_CODEPOINTS for ch in s)


def normalize_header_value(raw: str | None) -> str | None:
    """Validate an identity header value (`x-user-id`/`x-tenant-id`): non-empty after trim,
    <=256 bytes (UTF-8), no ASCII control characters/DEL/U+2028/U+2029. Returns `None` if `raw`
    is missing or fails validation — callers treat that identically to the header being absent.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or len(s.encode("utf-8")) > MAX_HEADER_VALUE_LEN:
        return None
    if _has_control_char(s):
        return None
    return s


def normalize_request_id(raw: str | None) -> str | None:
    """Validate `x-request-id`: non-empty after trim, <=128 bytes (UTF-8), matches
    `^[a-zA-Z0-9._-]+$`. Returns `None` if `raw` is missing or invalid — callers generate a new
    UUID in that case rather than trusting an unvalidated client-supplied correlation id.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or len(s.encode("utf-8")) > MAX_REQUEST_ID_LEN:
        return None
    if not _REQUEST_ID_RE.match(s):
        return None
    return s


def resolve_ids(raw_request_id: str | None, raw_correlation_id: str | None) -> tuple[str, str]:
    """The request's ``(request_id, correlation_id)`` from inbound ``x-request-id`` /
    ``x-correlation-id``, for HTTP and gRPC alike. Both are validated like Go's
    ``NormalizeRequestID`` (see :func:`normalize_request_id`): an invalid request id is replaced
    by a new UUID, an invalid or missing correlation id falls back to the request id. Neither
    raw value is ever echoed, logged or propagated."""
    request_id = normalize_request_id(raw_request_id) or str(uuid.uuid4())
    return request_id, normalize_request_id(raw_correlation_id) or request_id


class InvalidRolesError(ValueError):
    """Go ``ErrInvalidRoles``: the roles header exceeds ``MAX_ROLES_HEADER_LEN`` bytes or
    yields more than ``MAX_ROLES`` distinct valid roles."""


def parse_roles_strict(header: str | None) -> tuple[str, ...]:
    """Go ``ParseRolesStrict``: :func:`parse_roles` (invalid tokens dropped, duplicates removed),
    but raises :class:`InvalidRolesError` when the header is over ``MAX_ROLES_HEADER_LEN`` bytes
    or yields more than ``MAX_ROLES`` distinct valid roles."""
    if not header:
        return ()
    if len(header.encode("utf-8")) > MAX_ROLES_HEADER_LEN:
        raise InvalidRolesError("invalid tenant roles")
    roles = parse_roles(header)
    if len(roles) > MAX_ROLES:
        raise InvalidRolesError("invalid tenant roles")
    return roles


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """The validated identity of a request, or why it was rejected (``rejection``: an audit
    reason such as ``"ambiguous x-user-id header"``). A rejected identity is entirely absent."""

    user_id: str | None = None
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()
    rejection: str | None = None


def resolve_identity(
    user_ids: Sequence[str], tenant_ids: Sequence[str], role_values: Sequence[str]
) -> IdentityResolution:
    """The identity from every received value of ``x-user-id``, ``x-tenant-id`` and
    ``x-tenant-roles`` (HTTP headers or gRPC metadata alike), with gincommon's rules:

    - a repeated ``x-user-id`` or ``x-tenant-id`` is **ambiguous** (Go: 401 "ambiguous <h>
      header"), never resolved by first or last value wins;
    - repeated ``x-tenant-roles`` values are joined with ``,`` and parsed strictly (caps:
      ``MAX_ROLES_HEADER_LEN`` bytes, ``MAX_ROLES`` roles; Go: 401 "invalid tenant roles");
    - each id is validated by :func:`normalize_header_value` (an invalid one is absent).

    On a rejection the whole identity is absent (fail closed) and ``rejection`` says why."""
    for header, values in ((USER_ID_HEADER, user_ids), (TENANT_ID_HEADER, tenant_ids)):
        if len(values) > 1:
            return IdentityResolution(rejection=f"ambiguous {header} header")
    try:
        roles = parse_roles_strict(TENANT_ROLES_SEPARATOR.join(role_values))
    except InvalidRolesError:
        return IdentityResolution(rejection="invalid tenant roles")
    return IdentityResolution(
        user_id=normalize_header_value(user_ids[0]) if user_ids else None,
        tenant_id=normalize_header_value(tenant_ids[0]) if tenant_ids else None,
        roles=roles,
    )


def parse_roles(header: str | None) -> tuple[str, ...]:
    """Parse `x-tenant-roles`: split on `,`, trim each token, validate against
    `^[a-zA-Z0-9_-]+$` and <=64 bytes (UTF-8), deduplicate (first occurrence wins), and silently
    drop invalid/empty/oversized tokens rather than failing the request.
    """
    if not header:
        return ()
    seen: set[str] = set()
    roles: list[str] = []
    for part in header.split(TENANT_ROLES_SEPARATOR):
        role = part.strip()
        if not role or len(role.encode("utf-8")) > MAX_ROLE_LEN or not _ROLE_TOKEN_RE.match(role):
            continue
        if role in seen:
            continue
        seen.add(role)
        roles.append(role)
    return tuple(roles)
