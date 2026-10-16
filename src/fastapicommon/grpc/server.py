from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import grpc
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from fastapicommon._errors import add_exception_event, emit_unclassified
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
from fastapicommon.logging import get_logger
from fastapicommon.logging.sanitize import sanitize_fields, sanitize_log_value
from fastapicommon.metrics.platform import (
    TRANSPORT_GRPC,
    UNKNOWN,
    grpc_code_label,
    grpc_method_label,
    init_metrics_from_settings,
    platform_metrics,
    proto_message_size,
)

if TYPE_CHECKING:
    from fastapicommon.config import PlatformSettings

_logger = get_logger("fastapicommon.grpc")

TRACER_SCOPE = "platform-fastapicommon/grpc"
#: One tracer for every RPC: the global (proxy / delegating) tracer follows a provider installed
#: or swapped later.
_TRACER = trace.get_tracer(TRACER_SCOPE)

#: Go ``isClientError``: these codes log ``grpc_request`` at warn, other non-OK codes at error.
_CLIENT_CODES = frozenset(
    {
        "InvalidArgument",
        "NotFound",
        "AlreadyExists",
        "PermissionDenied",
        "Unauthenticated",
        "ResourceExhausted",
        "FailedPrecondition",
        "Aborted",
        "OutOfRange",
        "Canceled",
        "DeadlineExceeded",
    }
)
#: The fixed message of every INTERNAL status the library produces (gincommon's MsgInternal).
MSG_INTERNAL = "internal server error"

#: grpc.StatusCode by its Go-style name (``NotFound``), for the canonical span attributes.
_STATUS_BY_GO_NAME = {grpc_code_label(code.name): code for code in grpc.StatusCode}


def _explicit_code(context: Any) -> Any:
    """The status the handler set (``context.abort`` / ``context.set_code``), or None."""
    code_fn = getattr(context, "code", None)
    code = code_fn() if callable(code_fn) else None
    return None if code is None or getattr(code, "name", None) == "OK" else code


def _log_unclassified_grpc(method: str, exc: BaseException, service: str) -> None:
    where: dict[str, object] = {
        "grpc_method": grpc_method_label(method),
        "grpc_code": grpc_code_label("INTERNAL"),
    }
    emit_unclassified(TRANSPORT_GRPC, exc, where, service)


def _sanitized_status(method: str, exc: Exception, context: Any, service: str) -> tuple[Any, str]:
    """The status to abort a handler exception with, so grpcio never sends (and logs)
    ``"Exception calling application: <message>"``. A status the handler chose (abort /
    set_code) is kept: re-aborting with its own code and details stops grpcio replacing missing
    details with the message. Anything else is unclassified: logged, and INTERNAL."""
    code = _explicit_code(context)
    if code is not None:
        details_fn = getattr(context, "details", None)
        raw = details_fn() if callable(details_fn) else None
        details = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
        return code, details
    _log_unclassified_grpc(method, exc, service)
    return grpc.StatusCode.INTERNAL, MSG_INTERNAL


def _sanitize(method: str, exc: Exception, context: Any, service: str) -> None:
    """Abort with :func:`_sanitized_status` (raises); returns only when ``context`` cannot
    abort, and then the caller re-raises."""
    code, details = _sanitized_status(method, exc, context, service)
    abort = getattr(context, "abort", None)
    if callable(abort):
        abort(code, details)


async def _sanitize_async(method: str, exc: Exception, context: Any, service: str) -> None:
    """:func:`_sanitize` for ``grpc.aio``, whose ``context.abort`` is a coroutine."""
    code, details = _sanitized_status(method, exc, context, service)
    abort = getattr(context, "abort", None)
    if callable(abort):
        result = abort(code, details)
        if inspect.isawaitable(result):
            await result


def _platform_grpc_code(context: Any, raised: bool) -> tuple[str, bool]:
    """The gRPC code the client receives, in Go's spelling, and whether the RPC "panicked".

    An explicit status (``context.abort`` / ``context.set_code``) is reported as-is. An exception
    without one is grpc-python's equivalent of a Go handler panic: the client receives UNKNOWN,
    and it is counted in ``platform_grpc_panics_total``.
    """
    code_fn = getattr(context, "code", None)
    code = code_fn() if callable(code_fn) else None
    name = getattr(code, "name", None) if code is not None else None
    if raised and name in (None, "OK"):
        return "Unknown", True
    if name is None:
        return "OK", False
    return grpc_code_label(name), False


def _split_full_method(full: str) -> tuple[str, str]:
    """Go ``splitFullMethod``: ``/pkg.Service/Method`` → (``pkg.Service``, ``Method``)."""
    stripped = full.lstrip("/")
    service, _, method = stripped.rpartition("/")
    return service, method or "unknown"


def _start_server_span(
    method: str, metadata: tuple[tuple[str, str], ...]
) -> tuple[trace.Span, otel_context.Context]:
    """Go ``startServerSpan``: a server span named after the bounded method; an unrecognised
    method is ``rpc.method=_OTHER`` with the sanitized original in ``rpc.method_original``."""
    carrier = {key.lower(): value for key, value in metadata if isinstance(value, str)}
    parent = propagate.extract(carrier)
    name = grpc_method_label(method)
    span = _TRACER.start_span(name, context=parent, kind=SpanKind.SERVER)
    if name == UNKNOWN:
        span.set_attributes(
            {
                "rpc.system.name": "grpc",
                "rpc.method": "_OTHER",
                "rpc.method_original": sanitize_log_value(method or ""),
            }
        )
    else:
        service, rpc_method = _split_full_method(name)
        span.set_attributes(
            {"rpc.system.name": "grpc", "rpc.service": service, "rpc.method": rpc_method}
        )
    return span, parent


def _finish_server_span(span: trace.Span, code: str, error: BaseException | None) -> None:
    """Go ``finishServerSpan``: ``rpc.response.status_code`` (canonical, e.g. ``NOT_FOUND``) and
    the legacy integer ``rpc.grpc.status_code``; Error status for any non-OK outcome. An
    exception is recorded with its type only (never the message)."""
    status = _STATUS_BY_GO_NAME.get(code, grpc.StatusCode.UNKNOWN)
    span.set_attributes(
        {"rpc.response.status_code": status.name, "rpc.grpc.status_code": int(status.value[0])}
    )
    if error is not None:
        add_exception_event(span, error)
    if code != "OK":
        span.set_status(Status(StatusCode.ERROR, "panic" if error is not None else code))


def _log_grpc_request(method: str, code: str, seconds: float, rpc_context: RequestContext) -> None:
    """Go ``logGRPCRequest``: ``grpc_request`` with the bounded method, the Go code name,
    ``duration_ms``, ``request_id`` and the identity; info for OK, warn for client codes, error
    otherwise."""
    fields: dict[str, Any] = {
        "method": grpc_method_label(method),
        "grpc_code": code,
        "duration_ms": seconds * 1000.0,
        "request_id": rpc_context.request_id,
    }
    if rpc_context.user_id or rpc_context.tenant_id:
        fields["user_id"] = rpc_context.user_id
        fields["tenant_id"] = rpc_context.tenant_id
    safe = sanitize_fields(fields)
    if code == "OK":
        _logger.info("grpc_request", **safe)
    elif code in _CLIENT_CODES:
        _logger.warning("grpc_request", **safe)
    else:
        _logger.error("grpc_request", **safe)


def _context_from_metadata(metadata: tuple[tuple[str, str], ...]) -> RequestContext:
    headers: dict[str, list[str]] = {}
    for key, value in metadata:
        if isinstance(value, str):
            headers.setdefault(key.lower(), []).append(value)
    # Validated like HTTP and Go's request_id interceptor (NormalizeRequestID).
    request_id, correlation_id = resolve_ids(
        _first(headers, REQUEST_ID_HEADER), _first(headers, CORRELATION_ID_HEADER)
    )
    # The identity is validated exactly as on HTTP (ContextMiddleware; Go's ContextBuilder): an
    # invalid user / tenant id is absent, invalid role tokens are dropped. So an identity the
    # HTTP path rejects is never accepted on gRPC, nor logged as verified.
    # A repeated user / tenant id is ambiguous (Go: Unauthenticated "ambiguous <k> metadata")
    # and roles over the caps invalid: the whole identity is then absent, as on HTTP.
    identity = resolve_identity(
        headers.get(USER_ID_HEADER, []),
        headers.get(TENANT_ID_HEADER, []),
        headers.get(TENANT_ROLES_HEADER, []),
    )
    user = CurrentUser(user_id=identity.user_id, tenant_id=identity.tenant_id, roles=identity.roles)
    return RequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        user=user,
        identity_rejection=identity.rejection,
    )


def _first(headers: dict[str, list[str]], key: str) -> str | None:
    values = headers.get(key)
    return values[0] if values else None


class _UnaryRpc:
    """One unary RPC's observability, shared by the threaded and ``grpc.aio`` interceptors: the
    server span, the bound RequestContext, the in-flight gauge, then on :meth:`finish` the
    metrics, the ``grpc_request`` log and the span status."""

    def __init__(self, method: str, context: Any) -> None:
        self.method = method
        metadata = tuple(context.invocation_metadata() or ())
        self.span, parent = _start_server_span(method, metadata)
        # On top of the extracted context (not the current one), so inbound W3C baggage reaches
        # the handler and outbound calls, as on HTTP.
        self.span_token = otel_context.attach(trace.set_span_in_context(self.span, parent))
        rpc_context = _context_from_metadata(metadata)
        span_context = self.span.get_span_context()
        if span_context.is_valid:
            rpc_context = replace(
                rpc_context, trace_id=trace.format_trace_id(span_context.trace_id)
            )
        self.rpc_context = rpc_context
        self.token = bind_request_context(rpc_context)
        self.platform = platform_metrics()
        self.grpc_method = grpc_method_label(method)
        if self.platform is not None:
            self.platform.grpc_inflight_inc(self.grpc_method)
        self.start = time.perf_counter()
        self.error: BaseException | None = None
        self.unclassified = False
        self.cancelled_code: str | None = None

    def failed(self, exc: Exception, context: Any) -> None:
        self.error = exc
        self.unclassified = _explicit_code(context) is None

    def cancelled(self, context: Any) -> None:
        """The handler task was cancelled (grpc.aio: the deadline passed or the client
        cancelled). ``context.code()`` is still unset then; record what the client received."""
        remaining_fn = getattr(context, "time_remaining", None)
        remaining = remaining_fn() if callable(remaining_fn) else None
        expired = remaining is not None and remaining <= 0
        self.cancelled_code = "DeadlineExceeded" if expired else "Canceled"

    def finish(self, context: Any, request: Any, response: Any) -> None:
        """Record the outcome. Each step is guarded so a failing one (a metric, a logger) never
        leaves the RequestContext or OTel context bound on a reused worker thread, nor the span
        unended."""
        duration = time.perf_counter() - self.start
        if self.cancelled_code is not None:
            code, panicked = self.cancelled_code, False
        else:
            code, panicked = _platform_grpc_code(context, self.error is not None)
            # An unclassified exception is grpc-python's handler panic (Go: a recovered panic),
            # now sent as INTERNAL.
            panicked = panicked or self.unclassified
        try:
            try:
                self._record(code, panicked, duration, request, response)
            finally:
                with contextlib.suppress(Exception):
                    _log_grpc_request(self.method, code, duration, self.rpc_context)
        finally:
            reset_request_context(self.token)
            try:
                _finish_server_span(self.span, code, self.error if panicked else None)
            finally:
                self.span.end()
                otel_context.detach(self.span_token)

    def _record(
        self, code: str, panicked: bool, duration: float, request: Any, response: Any
    ) -> None:
        platform = self.platform
        if platform is None:
            return
        try:
            platform.record_grpc(
                self.grpc_method,
                code,
                duration,
                proto_message_size(request),
                proto_message_size(response),
            )
            if panicked:
                platform.record_grpc_panic(self.grpc_method)
        finally:
            platform.grpc_inflight_dec(self.grpc_method)


class ContextServerInterceptor(grpc.ServerInterceptor):  # type: ignore[misc]
    """Traces, binds a RequestContext (from inbound metadata) around, measures and logs each
    unary-unary RPC — platform-gincommon's tracing, context, metrics and logging interceptors in
    one.

    The server span continues the inbound ``traceparent`` and is named after the bounded
    ``grpc_method`` (see :func:`_start_server_span`); the RequestContext carries its trace id.
    Each RPC is logged as ``grpc_request`` (``method``, ``grpc_code``, ``duration_ms``,
    ``request_id``, ``user_id`` / ``tenant_id``).

    Once metrics are initialized it records ``platform_grpc_server_handled_total``,
    ``platform_grpc_server_handling_seconds``, request / response size, in-flight,
    ResourceExhausted and panic counters, labelled ``grpc_method``. Pass ``settings`` to
    initialize them from ``PlatformSettings`` (Go: the ``grpccommon`` interceptor constructors);
    like ``create_platform_app`` this raises ``ValueError`` on a missing domain or an identity
    the registry rejects, and initializes nothing when ``settings.enable_metrics`` is false.

    Every RPC (unary and streaming) has its exceptions sanitized, so grpcio never sends or
    logs ``"Exception calling application: <message>"``. A status the handler chose
    (``context.abort`` / ``set_code``) is kept. Any other exception becomes ``INTERNAL
    "internal server error"``, logged at error level as ``unclassified error`` with the
    exception types and stack frames, never the message (gincommon's error sanitizer
    contract). Streaming RPCs get no request context or metrics; use
    ``fastapicommon.context.request_context_scope`` inside a streaming handler if it needs
    request context.
    """

    def __init__(self, settings: PlatformSettings | None = None) -> None:
        self._service = settings.app_name if settings is not None else ""
        if settings is not None:
            init_metrics_from_settings(settings)

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = continuation(handler_call_details)
        if handler is None:
            return handler
        return self._wrap(handler, handler_call_details.method)

    def _wrap(self, handler: grpc.RpcMethodHandler, method: str) -> grpc.RpcMethodHandler:
        """The observed handler for a sync behaviour (also used by the ``grpc.aio``
        interceptor for sync handlers, which that server runs in its migration thread pool)."""
        if not handler.unary_unary:
            return self._sanitize_streaming(handler, method)

        original_behavior = handler.unary_unary

        def wrapped_behavior(request: Any, context: grpc.ServicerContext) -> Any:
            rpc = _UnaryRpc(method, context)
            response: Any = None
            try:
                response = original_behavior(request, context)
                return response
            except Exception as exc:
                rpc.failed(exc, context)
                # Raises the abort (INTERNAL for an unclassified exception, else the
                # handler's own status); falls through only for a context that cannot abort.
                _sanitize(method, exc, context, self._service)
                raise
            finally:
                rpc.finish(context, request, response)

        return grpc.unary_unary_rpc_method_handler(
            wrapped_behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    def _sanitize_streaming(
        self, handler: grpc.RpcMethodHandler, method: str
    ) -> grpc.RpcMethodHandler:
        """Streaming RPCs keep their behaviour (no request context or metrics), but an
        exception is sanitized like a unary one, including one raised mid-stream."""
        service = self._service

        def guard(context: Any, run: Callable[[], Any]) -> Any:
            try:
                return run()
            except Exception as exc:
                _sanitize(method, exc, context, service)
                raise

        def guard_iter(context: Any, it: Iterator[Any]) -> Iterator[Any]:
            try:
                yield from it
            except Exception as exc:
                _sanitize(method, exc, context, service)
                raise

        kw = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        if handler.unary_stream:
            us = handler.unary_stream
            return grpc.unary_stream_rpc_method_handler(
                lambda req, ctx: guard_iter(ctx, guard(ctx, lambda: us(req, ctx))), **kw
            )
        if handler.stream_unary:
            su = handler.stream_unary
            return grpc.stream_unary_rpc_method_handler(
                lambda reqs, ctx: guard(ctx, lambda: su(reqs, ctx)), **kw
            )
        if handler.stream_stream:
            ss = handler.stream_stream
            return grpc.stream_stream_rpc_method_handler(
                lambda reqs, ctx: guard_iter(ctx, guard(ctx, lambda: ss(reqs, ctx))), **kw
            )
        return handler  # pragma: no cover - grpc handlers always have one arity set


class AsyncContextServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """:class:`ContextServerInterceptor` for ``grpc.aio`` servers: the same server span, request
    context, ``platform_grpc_*`` metrics, ``grpc_request`` log and exception sanitizing::

        server = grpc.aio.server(interceptors=[AsyncContextServerInterceptor(settings)])

    ``async def`` handlers (coroutines and async generators) are wrapped asynchronously. Sync
    handlers keep running in the server's ``migration_thread_pool`` and are wrapped exactly as
    :class:`ContextServerInterceptor` wraps them. A handler's own ``context.abort`` is passed
    through untouched (its status is already sent). Streaming RPCs get exception sanitizing only,
    as with the threaded interceptor.
    """

    def __init__(self, settings: PlatformSettings | None = None) -> None:
        self._service = settings.app_name if settings is not None else ""
        self._sync = ContextServerInterceptor()
        self._sync._service = self._service
        if settings is not None:
            init_metrics_from_settings(settings)

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = await continuation(handler_call_details)
        if handler is None:
            return handler
        method = handler_call_details.method
        behavior = (
            handler.unary_unary
            or handler.unary_stream
            or handler.stream_unary
            or handler.stream_stream
        )
        if not (inspect.iscoroutinefunction(behavior) or inspect.isasyncgenfunction(behavior)):
            return self._sync._wrap(handler, method)
        return _wrap_async(handler, method, self._service)


def _wrap_async(handler: grpc.RpcMethodHandler, method: str, service: str) -> grpc.RpcMethodHandler:
    kw = {
        "request_deserializer": handler.request_deserializer,
        "response_serializer": handler.response_serializer,
    }

    async def on_error(exc: Exception, context: Any) -> None:
        # The handler aborted itself: its status is on the wire; aborting again raises
        # UsageError("Abort already called!").
        if not isinstance(exc, grpc.aio.AbortError):
            await _sanitize_async(method, exc, context, service)

    if handler.unary_unary:
        original = handler.unary_unary

        async def unary_unary(request: Any, context: Any) -> Any:
            rpc = _UnaryRpc(method, context)
            response: Any = None
            try:
                response = await original(request, context)
                return response
            except asyncio.CancelledError:
                rpc.cancelled(context)
                raise
            except Exception as exc:
                rpc.failed(exc, context)
                await on_error(exc, context)
                raise
            finally:
                rpc.finish(context, request, response)

        return grpc.unary_unary_rpc_method_handler(unary_unary, **kw)

    def guarded(behavior: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
        # Keep the behaviour's kind: grpc.aio calls an async generator and a coroutine that
        # writes with context.write differently.
        if inspect.isasyncgenfunction(behavior):

            async def generator(request: Any, context: Any) -> AsyncIterator[Any]:
                try:
                    async for item in behavior(request, context):
                        yield item
                except Exception as exc:
                    await on_error(exc, context)
                    raise

            return generator

        async def coroutine(request: Any, context: Any) -> Any:
            try:
                return await behavior(request, context)
            except Exception as exc:
                await on_error(exc, context)
                raise

        return coroutine

    if handler.unary_stream:
        return grpc.unary_stream_rpc_method_handler(guarded(handler.unary_stream), **kw)
    if handler.stream_unary:
        return grpc.stream_unary_rpc_method_handler(guarded(handler.stream_unary), **kw)
    return grpc.stream_stream_rpc_method_handler(guarded(handler.stream_stream), **kw)
