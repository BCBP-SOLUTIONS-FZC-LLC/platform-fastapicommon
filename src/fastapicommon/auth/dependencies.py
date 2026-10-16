from __future__ import annotations

from fastapicommon.auth.headers import USER_ID_HEADER
from fastapicommon.context import CurrentUser, get_request_context
from fastapicommon.exceptions import UnauthorizedException


def get_current_user() -> CurrentUser:
    """FastAPI dependency returning the CurrentUser for this request, authenticated or not.

    Usage: ``user: CurrentUser = Depends(get_current_user)``.
    """
    return get_request_context().user


def require_authenticated_user() -> CurrentUser:
    """FastAPI dependency that raises UnauthorizedException if no user is authenticated, after
    logging ``auth: request rejected`` (warn; gincommon's ``RequireAuth`` audit entry).

    Usage: ``user: CurrentUser = Depends(require_authenticated_user)``.
    """
    user = get_request_context().user
    if not user.is_authenticated:
        from fastapicommon.logging.audit import log_auth_rejection

        log_auth_rejection(f"missing or invalid {USER_ID_HEADER}")
        raise UnauthorizedException()
    return user
