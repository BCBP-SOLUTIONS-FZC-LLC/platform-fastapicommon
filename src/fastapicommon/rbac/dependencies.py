from __future__ import annotations

from fastapicommon.context import get_roles
from fastapicommon.exceptions import ForbiddenException


class RequireRole:
    """FastAPI dependency requiring a single role, sourced from ``x-tenant-roles``.

    Usage: ``Depends(RequireRole("tenant_admin"))``.
    """

    def __init__(self, role: str) -> None:
        self.role = role

    def __call__(self) -> None:
        if self.role not in get_roles():
            raise ForbiddenException(f"Requires role '{self.role}'")


class RequireAnyRole:
    """FastAPI dependency requiring at least one of the given roles.

    Usage: ``Depends(RequireAnyRole("operator", "admin"))``.
    """

    def __init__(self, *roles: str) -> None:
        self.roles = roles

    def __call__(self) -> None:
        current = get_roles()
        if not any(role in current for role in self.roles):
            raise ForbiddenException(f"Requires any of roles {list(self.roles)}")


class RequireAllRoles:
    """FastAPI dependency requiring all of the given roles.

    Usage: ``Depends(RequireAllRoles("a", "b"))``.
    """

    def __init__(self, *roles: str) -> None:
        self.roles = roles

    def __call__(self) -> None:
        current = get_roles()
        missing = [role for role in self.roles if role not in current]
        if missing:
            raise ForbiddenException(f"Missing required roles {missing}")
