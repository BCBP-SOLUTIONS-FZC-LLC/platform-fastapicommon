"""Inbound header validation, ported byte-for-byte from `platform-gincommon`'s
`internal/core/domain/validation.go` so a Go-issued identity/request-id/roles header behaves
identically whether it lands on a Go or a Python route.
"""

from __future__ import annotations

import re

from fastapicommon.auth.headers import TENANT_ROLES_SEPARATOR

__all__ = ["normalize_header_value", "normalize_request_id", "parse_roles"]

MAX_HEADER_VALUE_LEN = 256
MAX_ROLE_LEN = 64
MAX_REQUEST_ID_LEN = 128

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
