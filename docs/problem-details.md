# RFC 9457 Problem Details in fastapicommon

Every HTTP error from an app built with `create_platform_app` (or `register_exception_handlers`) is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem served as `application/problem+json`. The contract is **platform-gincommon v2's**: the same 15 `platform/*` types, titles and error codes, the same `traceId` resolution, the same validation messages and the same log line. A Go and a Python service answer the same request with the same body. The canonical documents are gincommon's `docs/problem-details/README.md` (the contract) and `docs/problem-details/registry.md` (the registry); `tests/unit/problem` fails if this package's platform types drift from that registry.

## Raising errors

```python
from fastapicommon.exceptions import NotFoundException, PlatformException, ValidationException
from fastapicommon.problem import FieldError, problem_type

TENDER_NOT_FOUND = problem_type("tender", "not-found", "Tender not found", 404, "TENDER_NOT_FOUND")

raise PlatformException("Tender 123 was not found.", problem_type=TENDER_NOT_FOUND)
raise NotFoundException("Widget 42 was not found.")
raise ValidationException(validation_errors=[FieldError("deadline", "must be in the future")])
raise ConflictException("The tender is being edited.", code="TENDER_LOCKED")
```

| Exception | Status | `type` |
|---|---|---|
| `ValidationException`, `RequestValidationError` | 400 | `platform/validation-failed` |
| `UnauthorizedException` | 401 | `platform/unauthorized` |
| `ForbiddenException` (and the `RequireRole*` dependencies) | 403 | `platform/forbidden` |
| `ResourceNotFoundException` / `NotFoundException`; an unmatched route (`ROUTE_NOT_FOUND`) | 404 | `platform/not-found` |
| a route without the method | 405 | `platform/method-not-allowed` |
| `ConflictException` | 409 | `platform/conflict` |
| `BusinessRuleViolationException` | 422 | `platform/business-rule-violation` |
| `RateLimitException` | 429 | `platform/rate-limit-exceeded` |
| `UnsupportedOperationException`, `NotImplementedError` | 501 | `platform/not-implemented` |
| `ServiceUnavailableException`; `TimeoutMiddleware` (`REQUEST_TIMEOUT`) | 503 | `platform/service-unavailable` |
| `HTTPException(status)` | its status | the platform type for the status (`about:blank` if none) |
| any other exception | 500 | `platform/internal-error` — **no detail** |
| `PlatformException(..., problem_type=T)` (`BusinessException`) | `T.status` | `T` |

- `detail` is the exception's message: a sentence the service wrote. Never put exception text, SQL, secrets or tenant information in it. An `internal-error` never sends a detail, and an `HTTPException`'s 5xx detail is dropped.
- `instance` is the request path, without the query string.
- `tenantId` is set only when the request carries an authenticated identity (`x-user-id`), and never on a 401.
- `traceId` is the first usable value of: a valid `traceparent`'s trace-id, a sanitized `x-request-id`, the active span's trace id, or a new 32-hex id.

## Extension members

A problem type may carry its own [RFC 9457 §3.2](https://www.rfc-editor.org/rfc/rfc9457#section-3.2) extension members, such as the version a client needs to retry an optimistic-lock conflict (gincommon `v2.1.0`'s `Error.WithExtension`):

```python
raise ConflictException("The tenant was modified concurrently.").with_extension("record_version", 7)
raise BusinessRuleViolationException("The user is a delegate on active workflows.").with_extensions(
    {"active_workflows": ["wf-1", "wf-2"]}
)
raise ConflictException("Locked.", extensions={"record_version": 7})       # the same, as a keyword
return problem_response(request, TYPE_CONFLICT, detail="...", extensions={"record_version": 7})
```

```json
{"type": "https://errors.company.com/platform/conflict", "title": "Conflict", "status": 409, "...": "...", "record_version": 7}
```

- They are sent at the top level, **after** the standard members, **sorted by name** (code-point order: upper case first), so the body is deterministic and identical to gincommon's. A problem without extensions is byte-for-byte what it was before.
- **A standard member always wins.** A name must be `valid_extension_name(name)`: RFC 9457's letter followed by letters, digits or `_`, at least 3 characters, and not a standard or platform member (`type`, `title`, `status`, `detail`, `instance`, `traceId`, `errorCode`, `tenantId`, `timestamp`, `validationErrors`) in any letter case. Any other name is dropped, so an extension can never replace `status`, `traceId` or `tenantId`.
- An `internal-error` drops every extension, as it drops its detail. Other 5xx types keep theirs.
- Values are client-visible JSON (`dict`, `list`, `tuple`, `str`, `int`, `float`, `bool`, `None`): never error text, SQL, secrets, tokens or tenant data. Convert a model with `model_dump(mode="json")` first. If a value cannot be serialised (a `set`, `NaN`, an arbitrary object), the problem is still sent, **without** its extensions, and a warn `problem extensions dropped` entry is logged with `traceId`, `type`, `status`, `errorCode` and `service`, never the values.
- Extensions are not logged in `problem response`.
- Document a type's extension members in your service's OpenAPI by extending gincommon's `ProblemDetails` schema with `allOf`; clients ignore members they do not know.

## Validation errors

`RequestValidationError` is mapped to the same field paths and messages gincommon derives from validator tags:

| pydantic error | message |
|---|---|
| `missing` | `is required` |
| `string_too_short` / `string_too_long` | `must be at least N characters` / `must be at most N characters` |
| `too_short` / `too_long` | `must be at least N items` / `must be at most N items` |
| `greater_than_equal` / `less_than_equal` / `greater_than` / `less_than` | `must be at least N` / `must be at most N` / `must be greater than N` / `must be less than N` |
| `literal_error` / `enum` | `must be one of: a, b` |
| `*_type` / `*_parsing` | `must be a string`, `must be an integer`, `must be a number`, `must be a boolean`, `must be an array`, `must be an object`, `must be a valid UUID`, `must be a valid URL`, `must be a valid date-time` |
| anything else | `is invalid` |

Field paths drop the location (`body`, `query`, ...) and render indexes as `items[0].sku`. Malformed JSON is `The request body is not valid JSON.`, and a missing body is `The request body is empty.`. A message never echoes the rejected value.

## Logging

Each problem is logged once at WARN as `problem response` on the `fastapicommon.problem` logger, with `traceId`, `type`, `status`, `errorCode`, `service` (`app_name`) and `tenantId` (only when set). The detail is never logged.

### Unclassified exceptions

An exception nothing classified (not a `PlatformException`, `HTTPException`, validation error or `NotImplementedError`) is a **programming defect**. The client gets a generic 500 `internal-error`, and one ERROR entry `unclassified error` is logged on `fastapicommon.exceptions`, the same contract as gincommon's:

```json
{"event": "unclassified error", "level": "error", "classified": false,
 "traceId": "req-abc123", "trace_id": "4bf9…", "request_id": "req-abc123", "service": "orders-api",
 "method": "POST", "route": "/tenders/:tender_id", "operation": "orders.api.create_tender",
 "status": 500, "errorCode": "INTERNAL_ERROR",
 "error_type": "orders.repo.RepoError", "error_chain": ["orders.repo.RepoError", "psycopg.errors.UniqueViolation"],
 "stack": ["/app/orders/repo.py:42 in insert", "..."]}
```

- **Never the exception message.** `str(exc)` can carry SQL, row values, tokens or secrets. The entry has the types along `__cause__` / `__context__` and exception-group members (at most 8) and the stack frames as `file:line in function` (at most 30, no source lines).
- **Not re-raised.** Starlette's `ServerErrorMiddleware` re-raises after the `Exception` handler, so the server would log the message ("Exception in ASGI application"). `UnhandledErrorMiddleware` stops the exception inside the stack; `create_platform_app` installs it just inside `LoggingMiddleware`. An app built with `register_exception_handlers` alone should add it the same way: `app.add_middleware(UnhandledErrorMiddleware)` before `app.add_middleware(LoggingMiddleware)` and `app.add_middleware(ContextMiddleware)`.
- Classified errors, including a deliberate `PlatformException()` (500), are not flagged.
- **Alert on the metric, diagnose with the log.** Each unclassified exception also increments `platform_unclassified_errors_total{transport="http"|"grpc"}` (approved in gincommon's LLD OQ-25; both series start at 0). Alert on `sum by (domain, service, environment, transport) (increase(platform_unclassified_errors_total[5m])) > 0` (gincommon's reference `PlatformUnclassifiedErrors`). The log entry says where and which types. Fix it by raising a `PlatformException` where the failure happens.

## gRPC

RFC 9457 is HTTP-only, but gRPC errors follow the same rule that no exception message reaches a client or a log. `ContextServerInterceptor` sanitizes every RPC: a status the handler chose (`context.abort`, `set_code`) is kept; any other exception is aborted as `INTERNAL "internal server error"` and logged as `unclassified error` with `grpc_method` (instead of `route` / `operation`), the exception types and the stack frames. Without it, grpcio sends `UNKNOWN "Exception calling application: <message>"` and logs the message. It is the same contract as gincommon's gRPC error sanitizer.

## Configuration

`PlatformSettings.problem_type_base` (env `PROBLEM_TYPE_BASE`) sets the type URI base; the default is `https://errors.company.com`. The settings are per app (`app.state.fastapicommon_problem`).

## Clients

```python
from fastapicommon.problem import problem_from_response

problem = problem_from_response(resp)  # None unless application/problem+json
if problem is not None and problem.error_code == "TENDER_NOT_FOUND":
    ...
if problem is not None and problem.status == 409:
    version = problem.extension("record_version", int)  # None when absent (or null)
```

Branch on `status`, `type` and `error_code`, never on the text of `title` or `detail`. Every top-level member the parser does not recognise is kept in `problem.extensions`, whatever its name (a third-party server's `balance` or `x-vendor` too); a standard member in another letter case (`"Status"`) is neither read nor kept. `problem.extension(name, typ, default=None)` decodes one strictly with pydantic (`int`, models, dataclasses, `list[...]`, ...) and raises `ValueError` when it is present but not a `typ` (`"7"` is not an `int`, as in Go).

## Migrating from 0.2.x

| 0.2.x | now |
|---|---|
| `{"error", "status", "trace_id", "request_id"}`, `application/json` | an RFC 9457 problem, `application/problem+json`; the request id stays in the `X-Request-ID` header |
| `ValidationException` and `RequestValidationError` → 422 | 400 `platform/validation-failed` with `validationErrors` |
| `TimeoutMiddleware` → 504 | 503 `platform/service-unavailable`, `errorCode` `REQUEST_TIMEOUT` (gincommon's response) |
| `PlatformException.code` = `"not_found"`-style categories | `code` is the upper-snake `errorCode` (`"NOT_FOUND"`) |
| `UnauthorizedException` default message `Authentication is required` | `Missing or invalid authentication headers.` (gincommon's detail) |
| `ForbiddenException` default message `Access denied` | `The identity is not allowed to perform this operation.` |
| 500 body `An unexpected error occurred` | no `detail` |
