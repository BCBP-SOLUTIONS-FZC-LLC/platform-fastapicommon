# Migration Guide: platform-gincommon (Go) -> fastapicommon (Python)

This guide maps concepts from the Go/Gin shared middleware library, `platform-gincommon`,
to their Python/FastAPI equivalents in this package, for teams porting a Gin service to
FastAPI.

| Go (Gin) | Python (FastAPI) | Notes |
|---|---|---|
| Gin middleware | Starlette middleware (`fastapicommon.middleware`) | Same request/response interception model. |
| `context.Context` values | `contextvars` (`fastapicommon.context`) | Both are async/goroutine-safe; Python's contextvars are per-`asyncio.Task` instead of per-goroutine. |
| Structured logger (e.g. zap) | structlog (`fastapicommon.logging`) | JSON-only output on both sides; same enrichment fields (`request_id`, `correlation_id`, `tenant_id`, `user_id`). |
| Prometheus client (Go) | `prometheus_client` (`fastapicommon.metrics`) | Both libraries emit only the `platform_*` metrics — identical names, labels, label values and buckets from the shared Platform Observability Registry — so dashboards are portable. Both require a domain (`OBSERVABILITY_DOMAIN`, gincommon `Config.Domain`) and fail at startup without one. The pre-standard names (`http_requests_total`, `http_request_duration_seconds`, `grpc_requests_total`, `grpc_request_duration_seconds`) were removed from both libraries with no compatibility period, as neither had been deployed. |
| OpenTelemetry (Go SDK) | OpenTelemetry (Python SDK) (`fastapicommon.telemetry`) | Same `traceparent` propagation; spans are interoperable across the two SDKs. |
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
