from fastapicommon.auth.dependencies import get_current_user, require_authenticated_user
from fastapicommon.auth.headers import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TENANT_ROLES_HEADER,
    TENANT_ROLES_SEPARATOR,
    TRACEPARENT_HEADER,
    USER_ID_HEADER,
)
from fastapicommon.auth.validation import (
    MAX_HEADER_VALUE_LEN,
    MAX_REQUEST_ID_LEN,
    MAX_ROLE_LEN,
    normalize_header_value,
    normalize_request_id,
    parse_roles,
)
from fastapicommon.context import CurrentUser

__all__ = [
    "CORRELATION_ID_HEADER",
    "MAX_HEADER_VALUE_LEN",
    "MAX_REQUEST_ID_LEN",
    "MAX_ROLE_LEN",
    "REQUEST_ID_HEADER",
    "TENANT_ID_HEADER",
    "TENANT_ROLES_HEADER",
    "TENANT_ROLES_SEPARATOR",
    "TRACEPARENT_HEADER",
    "USER_ID_HEADER",
    "CurrentUser",
    "get_current_user",
    "normalize_header_value",
    "normalize_request_id",
    "parse_roles",
    "require_authenticated_user",
]
