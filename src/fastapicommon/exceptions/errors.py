from __future__ import annotations

from collections.abc import Iterable

from fastapicommon.problem import (
    TYPE_BUSINESS_RULE_VIOLATION,
    TYPE_CONFLICT,
    TYPE_FORBIDDEN,
    TYPE_INTERNAL_ERROR,
    TYPE_NOT_FOUND,
    TYPE_NOT_IMPLEMENTED,
    TYPE_RATE_LIMIT_EXCEEDED,
    TYPE_SERVICE_UNAVAILABLE,
    TYPE_UNAUTHORIZED,
    TYPE_VALIDATION_FAILED,
    FieldError,
    ProblemType,
)


class PlatformException(Exception):
    """An exception rendered as an RFC 9457 problem (the platform's BusinessException).

    ``problem_type`` fixes the title and status; ``message`` is the occurrence's ``detail``
    (a sentence the service wrote — never exception text, SQL, secrets or tenant
    information); ``code`` is the ``errorCode``. Raise it directly with a domain type::

        raise PlatformException("Tender 123 was not found.", problem_type=TENDER_NOT_FOUND)

    A 5xx internal-error never sends its detail.
    """

    problem_type: ProblemType = TYPE_INTERNAL_ERROR
    default_message: str | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        problem_type: ProblemType | None = None,
        code: str | None = None,
        validation_errors: Iterable[FieldError] = (),
    ) -> None:
        if problem_type is not None:
            self.problem_type = problem_type
        self.message = message if message is not None else self.default_message
        self.code = code or self.problem_type.code
        self.validation_errors = tuple(validation_errors)
        super().__init__(self.message or self.problem_type.title)

    @property
    def status_code(self) -> int:
        return self.problem_type.status


#: Go name for the base class (``problem.Error`` / ``problem.NewError``).
BusinessException = PlatformException


class ValidationException(PlatformException):
    """400 validation-failed; pass ``validation_errors=[FieldError(...)]``."""

    problem_type = TYPE_VALIDATION_FAILED


class UnauthorizedException(PlatformException):
    """401 unauthorized (AuthenticationException)."""

    problem_type = TYPE_UNAUTHORIZED
    default_message = "Missing or invalid authentication headers."


class ForbiddenException(PlatformException):
    """403 forbidden (AuthorizationException)."""

    problem_type = TYPE_FORBIDDEN
    default_message = "The identity is not allowed to perform this operation."


class ResourceNotFoundException(PlatformException):
    """404 not-found."""

    problem_type = TYPE_NOT_FOUND


NotFoundException = ResourceNotFoundException


class ConflictException(PlatformException):
    """409 conflict."""

    problem_type = TYPE_CONFLICT


class BusinessRuleViolationException(PlatformException):
    """422 business-rule-violation."""

    problem_type = TYPE_BUSINESS_RULE_VIOLATION


class RateLimitException(PlatformException):
    """429 rate-limit-exceeded."""

    problem_type = TYPE_RATE_LIMIT_EXCEEDED


class UnsupportedOperationException(PlatformException):
    """501 not-implemented."""

    problem_type = TYPE_NOT_IMPLEMENTED


class ServiceUnavailableException(PlatformException):
    """503 service-unavailable; the detail must be a fixed sentence."""

    problem_type = TYPE_SERVICE_UNAVAILABLE
