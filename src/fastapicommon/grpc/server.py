from __future__ import annotations

import time
import uuid
from collections.abc import Callable
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
    reset_request_context,
)
from fastapicommon.logging import get_logger
from fastapicommon.metrics.platform import (
    grpc_code_label,
    grpc_method_label,
    init_metrics_from_settings,
    platform_metrics,
    proto_message_size,
)

if TYPE_CHECKING:
    from fastapicommon.config import PlatformSettings

_logger = get_logger(__name__)


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

    Streaming RPCs are passed through unmodified; use
    ``fastapicommon.context.request_context_scope`` directly inside a streaming handler
    if it needs request context.
    """

    def __init__(self, settings: PlatformSettings | None = None) -> None:
        if settings is not None:
            init_metrics_from_settings(settings)

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = continuation(handler_call_details)
        if handler is None or not handler.unary_unary:
            return handler

        method = handler_call_details.method
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
            try:
                response = original_behavior(request, context)
                return response
            except Exception:
                status_code = "ERROR"
                raise
            finally:
                duration = time.perf_counter() - start
                if platform is not None:
                    platform.grpc_inflight_dec(grpc_method)
                    code, panicked = _platform_grpc_code(context, status_code == "ERROR")
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
