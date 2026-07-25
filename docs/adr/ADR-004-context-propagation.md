# ADR-004: Context Propagation

## Status

Accepted

## Context

Request-scoped data (identity, tenant, request/correlation ids) needs to be available
throughout a request's lifetime — across `await` points, background tasks, and gRPC
handlers — without threading it explicitly through every function signature.

## Decision

Use `contextvars` (`fastapicommon.context`) as the single mechanism for request-scoped
context propagation. `fastapicommon.middleware.ContextMiddleware` binds a
`RequestContext` from inbound headers at the start of each HTTP request and resets it at
the end; `fastapicommon.grpc.ContextServerInterceptor` does the equivalent for unary-unary
gRPC calls. Code anywhere in the call stack reads it via `get_request_context()` /
`get_tenant_id()` / etc. rather than receiving it as a parameter.

## Consequences

- Async-safe and task-safe by construction — `contextvars` values are copied per-task, so
  concurrent requests never see each other's context.
- Background tasks spawned outside the request's task (e.g. via a task queue) do not
  inherit the context automatically; use `fastapicommon.context.request_context_scope` to
  rebind it explicitly if the task needs it.
- Streaming gRPC handlers are not wrapped by the interceptor and must bind context
  manually if needed — see `fastapicommon.grpc.ContextServerInterceptor`'s docstring.
