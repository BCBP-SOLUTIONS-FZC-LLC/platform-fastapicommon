from __future__ import annotations

from fastapicommon.context import get_roles
from fastapicommon.exceptions import ForbiddenException
from fastapicommon.logging.audit import log_access_denied


class RequireRole:
    """FastAPI dependency requiring a single role, sourced from ``x-tenant-roles``. A denial is
    logged as ``authz: access denied`` (warn; gincommon's AuthZ audit entry) before the 403.

    Usage: ``Depends(RequireRole("tenant_admin"))``.
    """

    def __init__(self, role: str) -> None:
        self.role = role

    def __call__(self) -> None:
        if self.role not in get_roles():
            log_access_denied(f"require_role:{self.role}")
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
            log_access_denied("require_any_role:" + ",".join(self.roles))
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
            log_access_denied("require_all_roles:" + ",".join(self.roles))
            raise ForbiddenException(f"Missing required roles {missing}")
