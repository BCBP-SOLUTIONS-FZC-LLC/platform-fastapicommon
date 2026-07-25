from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

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


class ContextMiddleware(BaseHTTPMiddleware):
    """Populates the platform RequestContext from inbound auth/tracing headers for the
    duration of the request, generating request/correlation ids when absent, and echoes
    both ids back on the response.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        headers = request.headers
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
        context = RequestContext(request_id=request_id, correlation_id=correlation_id, user=user)

        token = bind_request_context(context)
        try:
            response = await call_next(request)
        finally:
            reset_request_context(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
