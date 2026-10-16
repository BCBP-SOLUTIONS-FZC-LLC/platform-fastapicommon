# Runtime Flows, Concurrency & Failure

Diagrams for every flow are in [`ARCHITECTURE.md`](../ARCHITECTURE.md) (sources in `docs/architecture/mermaid/`); LLD §8 and §9 are the long form with the full outcome tables. This file is the step-by-step contract.

## 1. App Construction — `create_platform_app`

1. Resolve `PlatformSettings`; `configure_logging(level, environment)`.
2. `init_metrics_from_settings` when `enable_metrics` — validates domain / service / environment against the registry (`ValueError` naming `OBSERVABILITY_DOMAIN` when missing).
3. `ensure_tracing(config)` when `enable_tracing` — installs the delegating provider once; logs `otel_tracing_initialized`. An exporter that cannot be built → warning + in-process provider (valid trace ids, nothing exported).
4. Build `FastAPI(**kwargs)`; wrap the router's lifespan so `flush_tracing()` runs after the app's own lifespan exits (all FastAPI lifespan forms keep working).
5. Add middleware (innermost first) → effective order `Tracing → Context → Logging → UnhandledError → Prometheus → Timeout`; include `/metrics` (with metrics), `/health`, `/live`, `/ready`; register exception handlers.

## 2. HTTP Request (success)

1. `TracingMiddleware` resolves the route label, extracts `traceparent`, starts the server span.
2. `ContextMiddleware` validates ids and identity (`resolve_identity` over every header value), resolves the client IP (trusted proxies), binds `RequestContext` for the whole call (streaming body included), sets response id headers on `http.response.start`.
3. `LoggingMiddleware` times the request.
4. `PrometheusMiddleware` increments active requests, counts body bytes sent.
5. `TimeoutMiddleware` runs the handler as a task with the deadline.
6. Route dependencies (`require_authenticated_user`, roles) and the handler run.
7. Unwind: metrics recorded (latency including the body), `http_request` logged, span annotated and ended, context reset.

## 3. Identity Rejected (401 / 403)

- Missing / invalid / ambiguous identity or roles over the caps → `RequestContext` with no user and `identity_rejection` set. Anonymous routes still run.
- `require_authenticated_user` → warn `auth: request rejected` (`claimed_*` values) → `UnauthorizedException(detail)` → 401 problem (`tenantId` never set).
- A role dependency without the role → warn `authz: access denied` → 403 problem.

## 4. Unclassified Exception (500)

1. A handler raises a non-`PlatformException`.
2. `UnhandledErrorMiddleware` (inside the access log) answers the 500 `internal-error` problem itself and never re-raises (so uvicorn never logs the message).
3. `unclassified error` logged with types and frames only; `platform_unclassified_errors_total{transport="http"}` +1; span gets a type-only `exception` event and status `panic`; metrics count a panic.
4. An app built **without** `UnhandledErrorMiddleware`: the exception escapes to `TracingMiddleware`, which marks the span `panic` (type only) and re-raises; Starlette answers 500.

## 5. Aborted and Cancelled Requests

- **Exception after the response started** (streaming body fails): the connection is dropped (pure ASGI; the client sees a protocol error, not a clean 200); `http_request` at debug with `aborted=true` and the status sent; span `http.aborted` (not Error); metrics keep the sent status; not a panic; `unclassified error` still logged.
- **Cancellation** (client gone, graceful shutdown): status 499 (or the status already sent), `4xx` / `client_error`, never a panic. `_scope.request_outcome` decides once for tracing, logging and metrics.

## 6. Timeout

`TimeoutMiddleware` cancels a handler that has not **started** a response in `request_timeout_seconds` → 503 `service-unavailable`, `errorCode` `REQUEST_TIMEOUT`, `platform_http_request_timeouts_total` +1. A streaming response that started in time is never cut. A handler that raises while being cancelled still yields the 503. A cancellation of the request itself during the unwind propagates (it is not turned into a 503 for a client that has left).

## 7. gRPC — Threaded (`ContextServerInterceptor`) and `grpc.aio` (`AsyncContextServerInterceptor`)

1. Unknown method → the interceptor returns `None`; the client gets `UNIMPLEMENTED`.
2. Unary: span named after the bounded method on the **extracted** context (baggage reaches the handler); `RequestContext` from metadata (same validation as HTTP; `-bin` values skipped); handler runs.
3. On exception: a status the handler chose is kept (aio: `AbortError` passed through, no double abort); otherwise `unclassified error`, `platform_unclassified_errors_total{transport="grpc"}` +1, abort `INTERNAL "internal server error"`. A context that cannot abort → the original exception is re-raised and grpcio ends the RPC.
4. aio deadline / client cancel → `DeadlineExceeded` / `Canceled`, never `OK`.
5. `finish()`: metrics, `grpc_request`, context reset, span end — each guarded, so one failing step cannot leave context bound on a reused thread.
6. **Streaming** (server, client, bidirectional): exception sanitizing only — no context, span or metrics (OQ-11). Sync handlers on an aio server keep running in its thread pool with the threaded wrapper.

## 8. Tracing Lifecycle

- `ensure_tracing`: first configured install wins; a provisional (`unknown_service`) install is replaced; a later configured call returns the active one.
- `init_tracing`: installs or swaps the inner SDK provider; tracers and `start_as_current_span` decorators obtained earlier follow per call.
- A provider the service installed itself is never replaced (`otel_provider_kept`), never flushed by `flush_tracing`.
- `flush_tracing()` on app shutdown: export buffered spans (≤ 5 s, off the event loop), keep the provider; a timeout logs `otel_shutdown_flush_failed`. `shutdown_tracing()` is for process exit and tests.

## 9. Concurrency

Context is a `ContextVar`: each request / RPC sees only its own (verified: 2,000 requests at concurrency 50, 0 mismatches). Metrics collectors are thread-safe; the `LOG_SAMPLING` table is bounded (4096 keys, expired windows swept) and never raises into callers.
