from __future__ import annotations

from fastapicommon.auth.headers import TENANT_ID_HEADER, USER_ID_HEADER
from fastapicommon.context import CurrentUser, get_request_context
from fastapicommon.exceptions import UnauthorizedException

#: gincommon's 401 details for a rejected gateway identity (``DetailAmbiguousUserID``, ...),
#: keyed by the rejection reason ``resolve_identity`` records.
_REJECTION_DETAILS = {
    f"ambiguous {USER_ID_HEADER} header": "Ambiguous x-user-id header.",
    f"ambiguous {TENANT_ID_HEADER} header": "Ambiguous x-tenant-id header.",
    "invalid tenant roles": "Invalid tenant roles.",
}


def get_current_user() -> CurrentUser:
    """FastAPI dependency returning the CurrentUser for this request, authenticated or not.

    Usage: ``user: CurrentUser = Depends(get_current_user)``.
    """
    return get_request_context().user


def require_authenticated_user() -> CurrentUser:
    """FastAPI dependency that raises UnauthorizedException if no user is authenticated, after
    logging ``auth: request rejected`` (warn; gincommon's ``RequireAuth`` audit entry).

    A rejected identity — a repeated ``x-user-id`` / ``x-tenant-id``, or a roles header over
    gincommon's caps — is a 401 with gincommon's detail: ``Ambiguous x-user-id header.``,
    ``Ambiguous x-tenant-id header.`` or ``Invalid tenant roles.``.

    Usage: ``user: CurrentUser = Depends(require_authenticated_user)``.
    """
    from fastapicommon.logging.audit import log_auth_rejection

    context = get_request_context()
    rejection = context.identity_rejection
    if rejection is not None:
        log_auth_rejection(rejection)
        raise UnauthorizedException(_REJECTION_DETAILS.get(rejection))
    user = context.user
    if not user.is_authenticated:
        log_auth_rejection(f"missing or invalid {USER_ID_HEADER}")
        raise UnauthorizedException()
    return user
