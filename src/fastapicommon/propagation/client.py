from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
from opentelemetry.propagate import inject

from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.context import get_correlation_id, get_request_id, get_tenant_id, get_user_id


class _PropagationAuth(httpx.Auth):
    """httpx auth hook that stamps outbound requests with the current platform request
    context and W3C trace headers on every send, including redirects and retries.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        user_id = get_user_id()
        tenant_id = get_tenant_id()
        request_id = get_request_id()
        correlation_id = get_correlation_id()

        if user_id:
            request.headers[USER_ID_HEADER] = user_id
        if tenant_id:
            request.headers[TENANT_ID_HEADER] = tenant_id
        if request_id:
            request.headers[REQUEST_ID_HEADER] = request_id
        if correlation_id:
            request.headers[CORRELATION_ID_HEADER] = correlation_id

        inject(request.headers)

        yield request


def create_platform_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient that propagates auth/tracing headers (``x-user-id``,
    ``x-tenant-id``, ``x-request-id``, ``x-correlation-id``, ``traceparent``) from the
    current RequestContext onto every outbound request.
    """
    return httpx.AsyncClient(auth=_PropagationAuth(), **kwargs)
