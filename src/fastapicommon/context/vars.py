from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from fastapicommon.context.models import RequestContext

_request_context: ContextVar[RequestContext | None] = ContextVar(
    "fastapicommon_request_context", default=None
)


def bind_request_context(context: RequestContext) -> Token[RequestContext | None]:
    """Bind a RequestContext to the current async context. Safe across await points and
    background tasks spawned from the same task, but does not leak across concurrent
    requests since each gets its own copy of the contextvar."""
    return _request_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)


@contextmanager
def request_context_scope(context: RequestContext) -> Iterator[RequestContext]:
    """Context manager form of bind/reset, useful in background tasks, gRPC handlers,
    and tests."""
    token = bind_request_context(context)
    try:
        yield context
    finally:
        reset_request_context(token)


def get_request_context_or_none() -> RequestContext | None:
    return _request_context.get()


def get_request_context() -> RequestContext:
    """Return the bound RequestContext, raising if none has been bound.

    Middleware installed via fastapicommon.middleware always binds one for the lifetime
    of a request; call this only from code that runs within a request or an explicit
    request_context_scope().
    """
    context = _request_context.get()
    if context is None:
        raise LookupError(
            "No RequestContext is bound to the current context. Ensure the platform "
            "context middleware is installed, or use request_context_scope() in "
            "background tasks and tests."
        )
    return context


def get_request_id() -> str | None:
    context = _request_context.get()
    return context.request_id if context is not None else None


def get_correlation_id() -> str | None:
    context = _request_context.get()
    return context.correlation_id if context is not None else None


def get_tenant_id() -> str | None:
    context = _request_context.get()
    return context.tenant_id if context is not None else None


def get_user_id() -> str | None:
    context = _request_context.get()
    return context.user_id if context is not None else None


def get_roles() -> tuple[str, ...]:
    context = _request_context.get()
    return context.roles if context is not None else ()
