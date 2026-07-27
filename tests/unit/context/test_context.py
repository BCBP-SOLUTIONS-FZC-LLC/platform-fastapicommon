import pytest

from fastapicommon.context import (
    CurrentUser,
    RequestContext,
    bind_request_context,
    get_client_ip,
    get_correlation_id,
    get_request_context,
    get_request_context_or_none,
    get_request_id,
    get_roles,
    get_tenant_id,
    get_trace_id,
    get_user_id,
    request_context_scope,
    reset_request_context,
)


def test_current_user_role_helpers() -> None:
    user = CurrentUser(user_id="u1", tenant_id="t1", roles=("admin", "operator"))
    assert user.is_authenticated
    assert user.has_role("admin")
    assert not user.has_role("viewer")
    assert user.has_any_role("viewer", "operator")
    assert not user.has_any_role("viewer", "guest")
    assert user.has_all_roles("admin", "operator")
    assert not user.has_all_roles("admin", "guest")


def test_anonymous_user_is_not_authenticated() -> None:
    user = CurrentUser()
    assert not user.is_authenticated
    assert user.roles == ()


def test_request_context_properties() -> None:
    user = CurrentUser(user_id="u1", tenant_id="t1", roles=("admin",))
    context = RequestContext(request_id="r1", correlation_id="c1", user=user)
    assert context.user_id == "u1"
    assert context.tenant_id == "t1"
    assert context.roles == ("admin",)


def test_request_context_trace_id_and_client_ip_default() -> None:
    context = RequestContext(request_id="r1", correlation_id="c1")
    assert context.trace_id == ""
    assert context.client_ip is None


def test_request_context_trace_id_and_client_ip_can_be_set() -> None:
    context = RequestContext(
        request_id="r1", correlation_id="c1", trace_id="a" * 32, client_ip="203.0.113.5"
    )
    assert context.trace_id == "a" * 32
    assert context.client_ip == "203.0.113.5"


def test_get_request_context_raises_when_unbound() -> None:
    assert get_request_context_or_none() is None
    with pytest.raises(LookupError):
        get_request_context()


def test_getters_return_none_or_empty_when_unbound() -> None:
    assert get_request_id() is None
    assert get_correlation_id() is None
    assert get_tenant_id() is None
    assert get_user_id() is None
    assert get_roles() == ()
    assert get_trace_id() == ""
    assert get_client_ip() is None


def test_bind_and_reset_request_context() -> None:
    context = RequestContext(
        request_id="r1",
        correlation_id="c1",
        user=CurrentUser(user_id="u1", tenant_id="t1"),
        trace_id="a" * 32,
        client_ip="203.0.113.5",
    )
    token = bind_request_context(context)
    try:
        assert get_request_context() is context
        assert get_request_id() == "r1"
        assert get_correlation_id() == "c1"
        assert get_tenant_id() == "t1"
        assert get_user_id() == "u1"
        assert get_trace_id() == "a" * 32
        assert get_client_ip() == "203.0.113.5"
    finally:
        reset_request_context(token)
    assert get_request_context_or_none() is None


def test_request_context_scope() -> None:
    context = RequestContext(request_id="r2", correlation_id="c2", user=CurrentUser())
    with request_context_scope(context):
        assert get_request_context() is context
    assert get_request_context_or_none() is None
