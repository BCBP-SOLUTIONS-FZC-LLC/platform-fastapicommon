from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

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
from fastapicommon.metrics import record_grpc_request

_logger = get_logger(__name__)


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
    """Binds a RequestContext (from inbound metadata) around each unary-unary RPC,
    records ``grpc_requests_total`` / ``grpc_request_duration_seconds``, and logs
    completion.

    Streaming RPCs are passed through unmodified; use
    ``fastapicommon.context.request_context_scope`` directly inside a streaming handler
    if it needs request context.
    """

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
            start = time.perf_counter()
            status_code = "OK"
            try:
                return original_behavior(request, context)
            except Exception:
                status_code = "ERROR"
                raise
            finally:
                duration = time.perf_counter() - start
                record_grpc_request(
                    method=method, status_code=status_code, duration_seconds=duration
                )
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
