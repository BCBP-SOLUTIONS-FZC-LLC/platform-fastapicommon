from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import grpc

from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    TENANT_ROLES_SEPARATOR,
    USER_ID_HEADER,
)
from fastapicommon.context import (
    CurrentUser,
    RequestContext,
    bind_request_context,
    get_request_context_or_none,
    reset_request_context,
)
from fastapicommon.exceptions.handlers import error_chain_types, stack_frames
from fastapicommon.logging import get_logger
from fastapicommon.metrics.platform import (
    TRANSPORT_GRPC,
    grpc_code_label,
    grpc_method_label,
    init_metrics_from_settings,
    platform_metrics,
    proto_message_size,
)

if TYPE_CHECKING:
    from fastapicommon.config import PlatformSettings

_logger = get_logger(__name__)
_unclassified_log = get_logger("fastapicommon.exceptions")

#: The fixed message of every INTERNAL status the library produces (gincommon's MsgInternal).
MSG_INTERNAL = "internal server error"


def _explicit_code(context: Any) -> Any:
    """The status the handler set (``context.abort`` / ``context.set_code``), or None."""
    code_fn = getattr(context, "code", None)
    code = code_fn() if callable(code_fn) else None
    return None if code is None or getattr(code, "name", None) == "OK" else code


def _log_unclassified_grpc(method: str, exc: BaseException, service: str) -> None:
    platform = platform_metrics()
    if platform is not None:
        platform.record_unclassified_error(TRANSPORT_GRPC)
    context = get_request_context_or_none()
    fields: dict[str, object] = {
        "classified": False,
        "trace_id": context.trace_id if context is not None else "",
        "request_id": context.request_id if context is not None else "",
        "grpc_method": grpc_method_label(method),
        "grpc_code": "INTERNAL",
        "error_type": "",
        "error_chain": [],
        "stack": stack_frames(exc),
    }
    chain = error_chain_types(exc)  # never empty: it starts with exc itself
    fields["error_type"], fields["error_chain"] = chain[0], chain
    if service:
        fields["service"] = service
    with contextlib.suppress(Exception):  # a failing logger must not change the RPC result
        _unclassified_log.error("unclassified error", **fields)


def _sanitize(method: str, exc: Exception, context: Any, service: str) -> bool:
    """Turn a handler exception into a status so grpcio never sends (and logs)
    ``"Exception calling application: <message>"``. Raises the abort; returns only when
    ``context`` cannot abort (then the caller re-raises). Returns True for an unclassified
    exception (no status set by the handler)."""
    abort = getattr(context, "abort", None)
    code = _explicit_code(context)
    if code is not None:
        # The handler chose a status (abort / set_code): keep it. Re-aborting with the
        # same code and details stops grpcio replacing missing details with the message.
        if callable(abort):
            details_fn = getattr(context, "details", None)
            raw = details_fn() if callable(details_fn) else None
            details = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
            abort(code, details)
        return False
    _log_unclassified_grpc(method, exc, service)
    if callable(abort):
        abort(grpc.StatusCode.INTERNAL, MSG_INTERNAL)
    return True


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


def _context_from_metadata(metadata: tuple[tuple[str, str], ...]) -> RequestContext:
    headers = {key.lower(): value for key, value in metadata}
    request_id = headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    correlation_id = headers.get(CORRELATION_ID_HEADER) or request_id
    roles_header = headers.get(TENANT_ROLES_HEADER, "")
    roles = tuple(
        role.strip() for role in roles_header.split(TENANT_ROLES_SEPARATOR) if role.strip()
    )
    user = CurrentUser(
        user_id=headers.get(USER_ID_HEADER),
        tenant_id=headers.get(TENANT_ID_HEADER),
        roles=roles,
    )
    return RequestContext(request_id=request_id, correlation_id=correlation_id, user=user)


class ContextServerInterceptor(grpc.ServerInterceptor):  # type: ignore[misc]
    """Binds a RequestContext (from inbound metadata) around each unary-unary RPC, records the
    ``platform_grpc_*`` metrics, and logs completion.

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
        method = handler_call_details.method
        if not handler.unary_unary:
            return self._sanitize_streaming(handler, method)

        original_behavior = handler.unary_unary

        def wrapped_behavior(request: Any, context: grpc.ServicerContext) -> Any:
            metadata = tuple(context.invocation_metadata() or ())
            rpc_context = _context_from_metadata(metadata)
            token = bind_request_context(rpc_context)
            platform = platform_metrics()
            grpc_method = grpc_method_label(method)
            if platform is not None:
                platform.grpc_inflight_inc(grpc_method)
            start = time.perf_counter()
            status_code = "OK"
            response: Any = None
            unclassified = False
            try:
                response = original_behavior(request, context)
                return response
            except Exception as exc:
                status_code = "ERROR"
                # Raises the abort (INTERNAL for an unclassified exception, else the
                # handler's own status); falls through only for a context that cannot abort.
                unclassified = _explicit_code(context) is None
                _sanitize(method, exc, context, self._service)
                raise
            finally:
                duration = time.perf_counter() - start
                if platform is not None:
                    platform.grpc_inflight_dec(grpc_method)
                    code, panicked = _platform_grpc_code(context, status_code == "ERROR")
                    # An unclassified exception is grpc-python's handler panic (Go: a
                    # recovered panic), now sent as INTERNAL.
                    panicked = panicked or unclassified
                    platform.record_grpc(
                        grpc_method,
                        code,
                        duration,
                        proto_message_size(request),
                        proto_message_size(response),
                    )
                    if panicked:
                        platform.record_grpc_panic(grpc_method)
                _logger.info(
                    "grpc_request_completed",
                    method=method,
                    status_code=status_code,
                    duration_seconds=duration,
                )
                reset_request_context(token)

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
