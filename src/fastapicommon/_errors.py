"""The one place that turns an exception into observability data — types and code locations,
**never the message** (which may carry SQL, PII, tokens or secrets): the ``unclassified error``
log entry of both transports, and the type-only ``exception`` span event."""

from __future__ import annotations

import contextlib
import traceback
from typing import Any

from fastapicommon.context import get_request_context_or_none
from fastapicommon.logging import get_logger

#: Bounds of the "unclassified error" entry (gincommon caps error_chain at 8 too).
MAX_ERROR_CHAIN = 8
MAX_STACK_FRAMES = 30

_unclassified_log = get_logger("fastapicommon.exceptions")


def exception_type_name(exc: BaseException) -> str:
    """The exception's fully qualified type (``module.Class``; builtins bare): OTel's
    ``exception.type``, and ``error_type`` in logs. A code identifier, never exception text."""
    t = type(exc)
    return t.__qualname__ if t.__module__ == "builtins" else f"{t.__module__}.{t.__qualname__}"


def error_chain_types(exc: BaseException, limit: int = MAX_ERROR_CHAIN) -> list[str]:
    """The exception types along ``__cause__`` / ``__context__`` and exception-group
    members, outermost first, at most ``limit`` (gincommon's ``error_chain``). Type names
    are code identifiers, never exception text."""
    out: list[str] = []
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue and len(out) < limit:
        e = queue.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        out.append(exception_type_name(e))
        if isinstance(e, BaseExceptionGroup):
            queue.extend(e.exceptions)
        nxt = e.__cause__ or (None if e.__suppress_context__ else e.__context__)
        if nxt is not None:
            queue.append(nxt)
    return out


def stack_frames(exc: BaseException, limit: int = MAX_STACK_FRAMES) -> list[str]:
    """The traceback as ``file:line in function`` entries, innermost last, at most
    ``limit`` (the innermost kept). No source lines and no exception message: frames are
    code locations only."""
    frames = traceback.extract_tb(exc.__traceback__)[-limit:]
    return [f"{f.filename}:{f.lineno} in {f.name}" for f in frames]


def add_exception_event(span: Any, exc: BaseException) -> None:
    """The span's ``exception`` event with ``exception.type`` only — no message, no stack trace
    (unlike ``span.record_exception``). Used by every span the library annotates."""
    span.add_event("exception", {"exception.type": exception_type_name(exc)})


def emit_unclassified(
    transport: str, exc: BaseException, where: dict[str, object], service: str
) -> None:
    """Count ``platform_unclassified_errors_total{transport}`` and log the error-level
    ``unclassified error`` entry (gincommon's OQ-24 / OQ-25 contract) for HTTP and gRPC alike:
    the request ids, ``where`` (the transport's location fields), the exception types and stack
    frames, and ``service``. A failing metric or logger never changes the response."""
    from fastapicommon.metrics.platform import platform_metrics  # avoid an import cycle

    with contextlib.suppress(Exception):
        platform = platform_metrics()
        if platform is not None:
            platform.record_unclassified_error(transport)
    context = get_request_context_or_none()
    chain = error_chain_types(exc)  # never empty: it starts with exc itself
    fields: dict[str, object] = {
        "classified": False,
        **{k: v for k, v in where.items() if k == "traceId"},
        "trace_id": context.trace_id if context is not None else "",
        "request_id": context.request_id if context is not None else "",
        **{k: v for k, v in where.items() if k != "traceId"},
        "error_type": chain[0],
        "error_chain": chain,
        "stack": stack_frames(exc),
    }
    if service:
        fields["service"] = service
    with contextlib.suppress(Exception):
        _unclassified_log.error("unclassified error", **fields)
