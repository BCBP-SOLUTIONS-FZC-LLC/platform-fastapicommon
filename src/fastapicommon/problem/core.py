"""RFC 9457 Problem Details: the platform's single HTTP error contract.

The Python port of platform-gincommon's ``pkg/problem`` (v2.1.0). The type registry, titles,
error codes, trace-id resolution and validation messages are the same as Go's, so a Go and a
Python service answer the same request with the same problem. Framework-free on purpose: the
FastAPI wiring lives in ``fastapicommon.exceptions``.
"""

from __future__ import annotations

import builtins
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, TypeVar

_T = TypeVar("_T")

PROBLEM_MEDIA_TYPE = "application/problem+json"
DEFAULT_PROBLEM_TYPE_BASE = "https://errors.company.com"
PLATFORM_DOMAIN = "platform"

CODE_REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
CODE_ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"

_SLUG_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_CODE_RE = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*")


@dataclass(frozen=True, slots=True)
class ProblemType:
    """An error type registry entry: a stable ``{domain}/{name}`` with a fixed title, status
    and default error code (Go: ``problem.Type``)."""

    domain: str
    name: str
    title: str
    status: int
    code: str

    @property
    def id(self) -> str:
        return f"{self.domain}/{self.name}" if (self.domain or self.name) else ""

    def uri(self, base: str = "") -> str:
        """``{base}/{domain}/{name}``; ``about:blank`` for a type without an id."""
        if not self.id:
            return "about:blank"
        return type_uri(base, self.domain, self.name)


def type_uri(base: str, domain: str, name: str) -> str:
    """``{base}/{domain}/{name}``; an empty base is DEFAULT_PROBLEM_TYPE_BASE and a trailing
    ``/`` on base is ignored."""
    base = base.strip().rstrip("/") or DEFAULT_PROBLEM_TYPE_BASE
    return f"{base}/{domain}/{name}"


def _platform(name: str, title: str, status: int, code: str) -> ProblemType:
    return ProblemType(PLATFORM_DOMAIN, name, title, status, code)


TYPE_BAD_REQUEST = _platform("bad-request", "Bad Request", 400, "BAD_REQUEST")
TYPE_VALIDATION_FAILED = _platform(
    "validation-failed", "Validation failed", 400, "VALIDATION_FAILED"
)
TYPE_UNAUTHORIZED = _platform("unauthorized", "Unauthorized", 401, "UNAUTHORIZED")
TYPE_FORBIDDEN = _platform("forbidden", "Forbidden", 403, "FORBIDDEN")
TYPE_NOT_FOUND = _platform("not-found", "Not Found", 404, "NOT_FOUND")
TYPE_METHOD_NOT_ALLOWED = _platform(
    "method-not-allowed", "Method Not Allowed", 405, "METHOD_NOT_ALLOWED"
)
TYPE_CONFLICT = _platform("conflict", "Conflict", 409, "CONFLICT")
TYPE_PAYLOAD_TOO_LARGE = _platform(
    "payload-too-large", "Payload Too Large", 413, "PAYLOAD_TOO_LARGE"
)
TYPE_UNSUPPORTED_MEDIA_TYPE = _platform(
    "unsupported-media-type", "Unsupported Media Type", 415, "UNSUPPORTED_MEDIA_TYPE"
)
TYPE_BUSINESS_RULE_VIOLATION = _platform(
    "business-rule-violation", "Business rule violation", 422, "BUSINESS_RULE_VIOLATION"
)
TYPE_RATE_LIMIT_EXCEEDED = _platform(
    "rate-limit-exceeded", "Too Many Requests", 429, "RATE_LIMIT_EXCEEDED"
)
TYPE_INTERNAL_ERROR = _platform("internal-error", "Internal Server Error", 500, "INTERNAL_ERROR")
TYPE_NOT_IMPLEMENTED = _platform("not-implemented", "Not Implemented", 501, "NOT_IMPLEMENTED")
TYPE_SERVICE_UNAVAILABLE = _platform(
    "service-unavailable", "Service Unavailable", 503, "SERVICE_UNAVAILABLE"
)
TYPE_GATEWAY_TIMEOUT = _platform("gateway-timeout", "Gateway Timeout", 504, "GATEWAY_TIMEOUT")

PLATFORM_TYPES: tuple[ProblemType, ...] = (
    TYPE_BAD_REQUEST,
    TYPE_VALIDATION_FAILED,
    TYPE_UNAUTHORIZED,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_METHOD_NOT_ALLOWED,
    TYPE_CONFLICT,
    TYPE_PAYLOAD_TOO_LARGE,
    TYPE_UNSUPPORTED_MEDIA_TYPE,
    TYPE_BUSINESS_RULE_VIOLATION,
    TYPE_RATE_LIMIT_EXCEEDED,
    TYPE_INTERNAL_ERROR,
    TYPE_NOT_IMPLEMENTED,
    TYPE_SERVICE_UNAVAILABLE,
    TYPE_GATEWAY_TIMEOUT,
)


def _code_from_title(title: str) -> str:
    return "_".join(re.findall(r"[A-Z0-9]+", title.upper()))


def type_for_status(status: int) -> ProblemType:
    """The platform type for a bare error status (400 is bad-request, not validation-failed);
    an ``about:blank`` type titled with the status phrase when there is none."""
    if status == 400:
        return TYPE_BAD_REQUEST
    for t in PLATFORM_TYPES:
        if t.status == status:
            return t
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    return ProblemType("", "", title, status, _code_from_title(title))


def problem_type(domain: str, name: str, title: str, status: int, code: str) -> ProblemType:
    """Define a domain error type (Go: ``problem.MustType``); raises ValueError on an invalid
    definition. ``domain`` and ``name`` are lower-case kebab slugs, ``platform`` is reserved,
    ``status`` is 4xx/5xx, ``code`` is upper snake case::

        TENDER_NOT_FOUND = problem_type(
            "tender", "not-found", "Tender not found", 404, "TENDER_NOT_FOUND"
        )
    """
    if not _SLUG_RE.fullmatch(domain):
        raise ValueError(f"problem type domain {domain!r} is not a lower-case kebab slug")
    if domain == PLATFORM_DOMAIN:
        raise ValueError(f"problem type domain {domain!r} is reserved for the platform types")
    if not _SLUG_RE.fullmatch(name):
        raise ValueError(f"problem type name {name!r} is not a lower-case kebab slug")
    if not title.strip():
        raise ValueError(f"problem type {domain}/{name} has an empty title")
    if not 400 <= status <= 599:
        raise ValueError(f"problem type {domain}/{name} status {status} is not a 4xx or 5xx")
    if not _CODE_RE.fullmatch(code):
        raise ValueError(f"problem type {domain}/{name} code {code!r} is not upper snake case")
    return ProblemType(domain, name, title, status, code)


@dataclass(frozen=True, slots=True)
class FieldError:
    """One ``validationErrors`` entry. ``message`` never echoes the rejected value."""

    field: str
    message: str


# --- extension members (RFC 9457 §3.2; gincommon problem/extensions.go) ---

#: RFC 9457 §3.2's recommendation for extension member names: a letter, then letters, digits
#: or ``_``, at least three characters in all.
_EXTENSION_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

#: The JSON names of the problem's own members (RFC 9457's and the platform's), exactly
#: gincommon's ``standardMembers``. An extension never replaces one of them.
_STANDARD_MEMBERS: tuple[str, ...] = (
    "type",
    "title",
    "status",
    "detail",
    "instance",
    "traceId",
    "errorCode",
    "tenantId",
    "timestamp",
    "validationErrors",
)
_STANDARD_MEMBERS_FOLDED = frozenset(m.casefold() for m in _STANDARD_MEMBERS)


def _is_standard_member(name: str) -> bool:
    """Whether ``name`` is a problem member, ignoring case (gincommon: ``strings.EqualFold``,
    because Go's encoding/json matches struct members case-insensitively, so ``"Status"``
    would overwrite ``status`` in a Go client)."""
    return name.casefold() in _STANDARD_MEMBERS_FOLDED


def valid_extension_name(name: str) -> bool:
    """Whether ``name`` may be sent as an extension member (Go: ``problem.ValidExtensionName``):
    it follows RFC 9457 §3.2 (a letter, then letters, digits or ``_``, at least three
    characters) and is not a standard or platform member in any letter case (``status``,
    ``TraceID``, ...). Any other name is dropped when the problem is built, so an extension can
    never overwrite a standard member."""
    return (
        isinstance(name, str)
        and _EXTENSION_NAME_RE.fullmatch(name) is not None
        and not _is_standard_member(name)
    )


def _valid_extensions(extensions: Mapping[str, Any] | None) -> dict[str, Any]:
    """``extensions`` without the names :func:`valid_extension_name` rejects, sorted by name
    (code-point order, which for the ASCII names allowed is gincommon's byte order). A shallow
    copy."""
    if not extensions:
        return {}
    return {
        name: extensions[name] for name in sorted(n for n in extensions if valid_extension_name(n))
    }


# --- trace correlation (identical to gincommon problem.ResolveTraceID / pgcommon-py) ---

_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})(?P<rest>-.*)?"
)
MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_RE = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_REQUEST_ID_LENGTH}}}")
_ZERO_TRACE_ID = "0" * 32


def trace_id_from_traceparent(value: str | None) -> str | None:
    """The trace-id of a valid W3C traceparent, else None."""
    if value is None:
        return None
    m = _TRACEPARENT_RE.fullmatch(value.strip())
    if m is None or m["version"] == "ff" or (m["version"] == "00" and m["rest"] is not None):
        return None
    if m["trace_id"] == _ZERO_TRACE_ID or m["parent_id"] == "0" * 16:
        return None
    return m["trace_id"]


def sanitized_request_id(value: str | None) -> str | None:
    """``value`` stripped when it is 1-128 characters of ``[A-Za-z0-9._:-]``, else None."""
    if value is None:
        return None
    value = value.strip()
    return value if _REQUEST_ID_RE.fullmatch(value) else None


def new_trace_id() -> str:
    return secrets.token_hex(16)


def resolve_trace_id(
    traceparent: str | None, request_id: str | None, span_trace_id: str | None
) -> str:
    """traceparent's trace-id, else the sanitized x-request-id, else the active span's trace
    id, else a new random 32-hex id."""
    return (
        trace_id_from_traceparent(traceparent)
        or sanitized_request_id(request_id)
        or (span_trace_id if span_trace_id and span_trace_id != _ZERO_TRACE_ID else None)
        or new_trace_id()
    )


def utc_timestamp(now: datetime | None = None) -> str:
    """RFC 3339 UTC with second precision, e.g. ``2026-10-15T10:45:22Z``."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_problem(
    t: ProblemType,
    *,
    base: str = "",
    detail: str | None = None,
    error_code: str | None = None,
    instance: str | None = None,
    trace_id: str | None = None,
    tenant_id: str | None = None,
    validation_errors: Iterable[FieldError] = (),
    timestamp: datetime | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The problem body (Go: ``Builder.Build``). Empty optional members are omitted; an
    internal-error never carries a detail, validationErrors or extension members, whatever was
    passed, so an exception message cannot leak through it.

    ``extensions`` are RFC 9457 §3.2 extension members (Go: ``Builder.Extension`` /
    ``Extensions``), added after the standard members, sorted by name. A name that is not a
    :func:`valid_extension_name` (a standard or platform member in any letter case, or not a
    letter + ``[A-Za-z0-9_]``, 3+ characters) is dropped, so a standard member always wins.
    The values are client-visible JSON: never error text, SQL, secrets, tokens or tenant data.
    Without extensions the body is exactly what it was before extensions existed."""
    if not t.status:
        t = TYPE_INTERNAL_ERROR
    body: dict[str, Any] = {
        "type": t.uri(base),
        "title": t.title or type_for_status(t.status).title,
        "status": t.status,
    }
    internal = t.id == TYPE_INTERNAL_ERROR.id
    if detail and not internal:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if trace_id:
        body["traceId"] = trace_id
    body["errorCode"] = error_code or t.code
    if tenant_id:
        body["tenantId"] = tenant_id
    body["timestamp"] = utc_timestamp(timestamp)
    fields = [{"field": f.field, "message": f.message} for f in validation_errors]
    if fields and not internal:
        body["validationErrors"] = fields
    if not internal:
        body.update(_valid_extensions(extensions))
    return body


@dataclass(frozen=True, slots=True)
class Problem:
    """A parsed problem (for clients). Branch on status, type and error_code, never on text.

    ``extensions`` holds every top-level member that is not a standard or platform member
    (RFC 9457 §3.2), whatever its name, so a client can read a server's extension members
    (e.g. ``record_version`` on a conflict); read one as a typed value with
    :meth:`extension`. It is empty when the body has none, and is not part of the hash."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    trace_id: str | None = None
    error_code: str | None = None
    tenant_id: str | None = None
    timestamp: str | None = None
    validation_errors: tuple[FieldError, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def extension(self, name: str, typ: builtins.type[_T], default: _T | None = None) -> _T | None:
        """The extension member ``name`` decoded as ``typ`` (Go: ``Problem.Extension``), or
        ``default`` when the problem does not have it or it is JSON ``null`` (as
        ``json.Unmarshal`` leaves its destination unchanged on ``null``; check
        ``name in p.extensions`` to tell the two apart)::

            version = problem.extension("record_version", int)
            workflows = problem.extension("active_workflows", list[Workflow])

        The value is decoded as JSON into ``typ`` with pydantic in strict mode (any type a
        ``TypeAdapter`` accepts: ``int``, ``str``, models, dataclasses, ``TypedDict``, generic
        containers), so ``"7"`` is not an ``int``, as Go's ``json.Unmarshal`` would not take
        it. Raises ``ValueError`` when the member is present but is not a ``typ``.
        """
        value = self.extensions.get(name)
        if value is None:
            return default
        from pydantic import TypeAdapter  # imported here: only clients that read one pay

        try:
            raw = json.dumps(value, allow_nan=False)
            return TypeAdapter(typ).validate_json(raw, strict=True)
        except (TypeError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            raise ValueError(f"problem: extension {name!r}: {exc}") from exc


def is_problem_content_type(value: str | None) -> bool:
    return bool(value) and value.split(";", 1)[0].strip().lower() == PROBLEM_MEDIA_TYPE  # type: ignore[union-attr]


def parse_problem(body: Mapping[str, Any], status: int | None = None) -> Problem:
    """Parse a decoded problem body; ``status`` fills a missing ``status`` member. Members it
    does not recognise are kept in ``Problem.extensions`` (Go: ``Decode`` / ``FromResponse``),
    so a client can read a server's extension members (:meth:`Problem.extension`), and a newer
    server's extensions do not break older clients. A standard member in another letter case
    (``"Status"``) is neither read nor kept."""
    fields = tuple(
        FieldError(str(f.get("field", "")), str(f.get("message", "")))
        for f in body.get("validationErrors") or ()
        if isinstance(f, Mapping)
    )
    return Problem(
        type=str(body.get("type", "about:blank")),
        title=str(body.get("title", "")),
        status=int(body.get("status") or status or 0),
        detail=body.get("detail"),
        instance=body.get("instance"),
        trace_id=body.get("traceId"),
        error_code=body.get("errorCode"),
        tenant_id=body.get("tenantId"),
        timestamp=body.get("timestamp"),
        validation_errors=fields,
        extensions={k: v for k, v in body.items() if not _is_standard_member(k)},
    )


def problem_from_response(response: Any) -> Problem | None:
    """The problem of an ``httpx.Response`` (or anything with ``headers``, ``status_code`` and
    ``json()``), or None when it is not ``application/problem+json``."""
    if not is_problem_content_type(response.headers.get("content-type")):
        return None
    return parse_problem(response.json(), response.status_code)
