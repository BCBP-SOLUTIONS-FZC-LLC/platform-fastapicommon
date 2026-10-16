# Migration Guide: platform-gincommon (Go) -> fastapicommon (Python)

This guide maps concepts from the Go/Gin shared middleware library, `platform-gincommon`,
to their Python/FastAPI equivalents in this package, for teams porting a Gin service to
FastAPI.

| Go (Gin) | Python (FastAPI) | Notes |
|---|---|---|
| Gin middleware | Starlette middleware (`fastapicommon.middleware`) | Same request/response interception model. |
| `context.Context` values | `contextvars` (`fastapicommon.context`) | Both are async/goroutine-safe; Python's contextvars are per-`asyncio.Task` instead of per-goroutine. |
| Zap logger (`pkg/logger`) | structlog (`fastapicommon.logging`) | Same output contract: Zap's JSON layout (`level`, `ts`, `caller`, `msg`, fields, `stacktrace`) on stderr, its console layout in dev/local, `LOG_LEVEL` / `LOG_SAMPLING` / `LOG_FILE`, and the same log events (`http_request`, `grpc_request`, `auth: request rejected`, `authz: access denied`, `problem response`, `rate limited`, `otel_*`). |
| Prometheus client (Go) | `prometheus_client` (`fastapicommon.metrics`) | Both libraries emit only the `platform_*` metrics — identical names, labels, label values and buckets from the shared Platform Observability Registry — so dashboards are portable. Both require a domain (`OBSERVABILITY_DOMAIN`, gincommon `Config.Domain`) and fail at startup without one. The pre-standard names (`http_requests_total`, `http_request_duration_seconds`, `grpc_requests_total`, `grpc_request_duration_seconds`) were removed from both libraries with no compatibility period, as neither had been deployed. |
| OpenTelemetry (Go SDK) | OpenTelemetry (Python SDK) (`fastapicommon.telemetry`) | Same `OTEL_*` configuration and defaults, resource attributes, parent-based sampling, span names and attributes (`METHOD route`, bounded `grpc_method`), and helpers (`new_tracer`, `span_trace_id`, `record_span_error`, `inject_trace_context`). |
| gRPC interceptors (Go) | gRPC interceptors (Python) (`fastapicommon.grpc`) | Python interceptors here cover unary-unary; streaming needs manual context binding (see ADR-004). |
| Health endpoints (Gin routes) | `fastapicommon.health` router | Same `/health`, `/ready`, `/live` paths and semantics. |

## Migration steps

1. Replace the service's Gin router bootstrap with `fastapicommon.bootstrap.create_platform_app()`.
2. Port service-specific routes to FastAPI routers; mount them on the returned `app`.
3. Replace direct header reads (`x-user-id`, `x-tenant-id`, etc.) with
   `fastapicommon.context.get_request_context()` / `fastapicommon.auth.get_current_user`.
4. Replace Go RBAC middleware calls with `fastapicommon.rbac.RequireRole` /
   `RequireAnyRole` / `RequireAllRoles` as FastAPI dependencies.
5. Set `OBSERVABILITY_DOMAIN` (and `APP_ENV`) for the service — required, as with gincommon's
   `Config.Domain`.
6. Point log/metrics/tracing dashboards at the new service; metric names, label names and
   header contracts are the same, so existing `platform_*` dashboards work without
   modification.
