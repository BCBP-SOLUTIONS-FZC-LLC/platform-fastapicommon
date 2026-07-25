import pytest

from fastapicommon.auth import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    USER_ID_HEADER,
    CurrentUser,
    get_current_user,
    require_authenticated_user,
)
from fastapicommon.context import RequestContext, request_context_scope
from fastapicommon.exceptions import UnauthorizedException


def test_header_constants() -> None:
    assert USER_ID_HEADER == "x-user-id"
    assert TENANT_ID_HEADER == "x-tenant-id"
    assert TENANT_ROLES_HEADER == "x-tenant-roles"
    assert REQUEST_ID_HEADER == "x-request-id"
    assert CORRELATION_ID_HEADER == "x-correlation-id"


def test_get_current_user_returns_bound_user() -> None:
    user = CurrentUser(user_id="u1", tenant_id="t1")
    context = RequestContext(request_id="r1", correlation_id="c1", user=user)
    with request_context_scope(context):
        assert get_current_user() is user


def test_require_authenticated_user_returns_user_when_authenticated() -> None:
    user = CurrentUser(user_id="u1")
    context = RequestContext(request_id="r1", correlation_id="c1", user=user)
    with request_context_scope(context):
        assert require_authenticated_user() is user


def test_require_authenticated_user_raises_when_anonymous() -> None:
    context = RequestContext(request_id="r1", correlation_id="c1", user=CurrentUser())
    with request_context_scope(context), pytest.raises(UnauthorizedException):
        require_authenticated_user()
