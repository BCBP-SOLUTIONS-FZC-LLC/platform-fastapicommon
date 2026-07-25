from __future__ import annotations

import collections
from collections.abc import Callable
from typing import Any

import grpc
from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.context import get_correlation_id, get_request_id, get_tenant_id, get_user_id


class _ClientCallDetails(
    collections.namedtuple(
        "_ClientCallDetails",
        ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
    ),
    grpc.ClientCallDetails,  # type: ignore[misc]
):
    pass


class ContextClientInterceptor(grpc.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    """Propagates the current RequestContext (``x-user-id``, ``x-tenant-id``,
    ``x-request-id``, ``x-correlation-id``) onto outbound unary-unary gRPC call
    metadata.
    """

    def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.ClientCallDetails, Any], Any],
        client_call_details: grpc.ClientCallDetails,
        request: Any,
    ) -> Any:
        metadata = list(client_call_details.metadata or [])

        user_id = get_user_id()
        tenant_id = get_tenant_id()
        request_id = get_request_id()
        correlation_id = get_correlation_id()

        if user_id:
            metadata.append((USER_ID_HEADER, user_id))
        if tenant_id:
            metadata.append((TENANT_ID_HEADER, tenant_id))
        if request_id:
            metadata.append((REQUEST_ID_HEADER, request_id))
        if correlation_id:
            metadata.append((CORRELATION_ID_HEADER, correlation_id))

        new_details = _ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=metadata,
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
            compression=client_call_details.compression,
        )
        return continuation(new_details, request)
