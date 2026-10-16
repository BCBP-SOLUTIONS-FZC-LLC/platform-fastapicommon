"""Log sanitising, ported from platform-gincommon's ``internal/core/service/log_sanitizer.go``.

Two separate mechanisms, as in Go:

- :func:`sanitize_fields` redacts the values of sensitive keys (``password``, ``token``,
  ``secret``, ``*_key``, ...) — applied to the access, auth and gRPC request logs;
- :func:`sanitize_log_value` strips control characters and truncates to 256 bytes at a UTF-8
  boundary — applied explicitly to header-derived values, paths and error strings, so client
  input cannot inject log lines.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
MAX_LOG_VALUE_BYTES = 256

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "credential",
        "private_key",
        "jwt",
        "bearer",
        "session",
        "session_id",
        "cert",
    }
)
_SENSITIVE_PARTS = ("password", "secret", "token", "credential", "session", "bearer")


def is_sensitive_log_key(key: str) -> bool:
    """Go ``isSensitiveLogKey``: an exact sensitive name, a name containing ``password``,
    ``secret``, ``token``, ``credential``, ``session`` or ``bearer``, or ``key`` / ``*_key``
    (but not ``monkey`` or ``key_metrics``)."""
    k = key.strip().lower()
    if not k:
        return False
    if k in _SENSITIVE_KEYS or any(part in k for part in _SENSITIVE_PARTS):
        return True
    return k == "key" or k.endswith("_key")


def sanitize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``fields`` with the values of sensitive keys replaced by ``[REDACTED]``
    (Go ``service.Sanitize``)."""
    return {k: (REDACTED if is_sensitive_log_key(k) else v) for k, v in fields.items()}


def sanitize_log_value(value: str, max_bytes: int = MAX_LOG_VALUE_BYTES) -> str:
    """``value`` without control characters, truncated to ``max_bytes`` UTF-8 bytes at a
    character boundary (Go ``SanitizeString`` / ``sanitizeLogValue``)."""
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
