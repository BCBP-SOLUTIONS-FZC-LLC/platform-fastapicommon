import pytest

from fastapicommon.context import CurrentUser, RequestContext, request_context_scope
from fastapicommon.exceptions import ForbiddenException
from fastapicommon.rbac import RequireAllRoles, RequireAnyRole, RequireRole


def _context_with_roles(*roles: str) -> RequestContext:
    return RequestContext(
        request_id="r1", correlation_id="c1", user=CurrentUser(user_id="u1", roles=roles)
    )


def test_require_role_passes_when_present() -> None:
    with request_context_scope(_context_with_roles("tenant_admin")):
        RequireRole("tenant_admin")()


def test_require_role_raises_when_missing() -> None:
    with request_context_scope(_context_with_roles("viewer")), pytest.raises(ForbiddenException):
        RequireRole("tenant_admin")()


def test_require_any_role_passes_when_one_present() -> None:
    with request_context_scope(_context_with_roles("operator")):
        RequireAnyRole("operator", "admin")()


def test_require_any_role_raises_when_none_present() -> None:
    with request_context_scope(_context_with_roles("viewer")), pytest.raises(ForbiddenException):
        RequireAnyRole("operator", "admin")()


def test_require_all_roles_passes_when_all_present() -> None:
    with request_context_scope(_context_with_roles("a", "b")):
        RequireAllRoles("a", "b")()


def test_require_all_roles_raises_when_missing_one() -> None:
    with request_context_scope(_context_with_roles("a")), pytest.raises(ForbiddenException):
        RequireAllRoles("a", "b")()
