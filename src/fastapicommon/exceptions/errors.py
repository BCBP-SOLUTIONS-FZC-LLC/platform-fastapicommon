from __future__ import annotations


class PlatformException(Exception):
    """Base class for platform exceptions that map to a structured JSON error response.

    Subclasses set ``status_code`` and ``code`` as class attributes; ``message`` may be
    overridden per-instance to add detail. ``code`` is a Python-side categorization attribute
    only — it is not part of the wire response body (see ``exceptions.handlers``), which mirrors
    the Go ``gincommon`` library's ``{error, status, trace_id, request_id}`` shape exactly so a
    Go client parsing either language's error response succeeds unchanged.
    """

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class UnauthorizedException(PlatformException):
    status_code = 401
    code = "unauthorized"
    default_message = "Authentication is required"


class ForbiddenException(PlatformException):
    status_code = 403
    code = "forbidden"
    default_message = "Access denied"


class ValidationException(PlatformException):
    status_code = 422
    code = "validation_error"
    default_message = "The request failed validation"


class ConflictException(PlatformException):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with the current state of the resource"


class NotFoundException(PlatformException):
    status_code = 404
    code = "not_found"
    default_message = "The requested resource was not found"
