import httpx

from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.context import CurrentUser, RequestContext, request_context_scope
from fastapicommon.propagation import create_platform_http_client
from fastapicommon.propagation.client import _PropagationAuth


def test_create_platform_http_client_configures_auth() -> None:
    client = create_platform_http_client()
    assert isinstance(client.auth, _PropagationAuth)


def test_auth_flow_stamps_headers_from_bound_context() -> None:
    context = RequestContext(
        request_id="r1",
        correlation_id="c1",
        user=CurrentUser(user_id="u1", tenant_id="t1"),
    )
    request = httpx.Request("GET", "https://example.com/resource")

    with request_context_scope(context):
        flow = _PropagationAuth().auth_flow(request)
        stamped_request = next(flow)

    assert stamped_request.headers[USER_ID_HEADER] == "u1"
    assert stamped_request.headers[TENANT_ID_HEADER] == "t1"
    assert stamped_request.headers[REQUEST_ID_HEADER] == "r1"
    assert stamped_request.headers[CORRELATION_ID_HEADER] == "c1"


def test_auth_flow_omits_headers_when_unbound() -> None:
    request = httpx.Request("GET", "https://example.com/resource")
    flow = _PropagationAuth().auth_flow(request)
    stamped_request = next(flow)

    assert USER_ID_HEADER not in stamped_request.headers
    assert TENANT_ID_HEADER not in stamped_request.headers


def test_auth_flow_propagates_roles_when_present() -> None:
    # Matches Go's PropagateHeaders: x-tenant-roles is injected, comma-joined, when non-empty.
    context = RequestContext(
        request_id="r1",
        correlation_id="c1",
        user=CurrentUser(user_id="u1", tenant_id="t1", roles=("admin", "operator")),
    )
    request = httpx.Request("GET", "https://example.com/resource")

    with request_context_scope(context):
        flow = _PropagationAuth().auth_flow(request)
        stamped_request = next(flow)

    assert stamped_request.headers[TENANT_ROLES_HEADER] == "admin,operator"


def test_auth_flow_omits_roles_header_when_no_roles() -> None:
    request = httpx.Request("GET", "https://example.com/resource")
    flow = _PropagationAuth().auth_flow(request)
    stamped_request = next(flow)

    assert TENANT_ROLES_HEADER not in stamped_request.headers
