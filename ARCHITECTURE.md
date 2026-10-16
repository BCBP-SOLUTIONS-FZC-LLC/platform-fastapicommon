# Architecture

This document describes the internal structure, dependency rules, and runtime data flows
of `fastapicommon`.

`fastapicommon` is a Python library (`pip install platform-fastapicommon`, `pip install
"platform-fastapicommon[grpc]"` for gRPC support — the PyPI distribution name differs from
the importable package name, see the README's Install section). It is never deployed as a
standalone server —
consuming services import it and call `fastapicommon.bootstrap.create_platform_app()`.
`examples/fastapi_service` and `examples/grpc_service` (and their `Dockerfile` /
`docker-compose.yml`) are a reference/demo only, used to exercise the library's logging,
metrics, and tracing against a local observability stack (see [Observability
stack](#observability-stack) below).

This is the Python sibling of the Go/Gin library `platform-gincommon`; see
[`docs/MigrationGuide.md`](docs/MigrationGuide.md) for the concept mapping between the two.

---

## Module dependency graph

`fastapicommon` is **not** organized as a Clean Architecture application (Entities → Use
Cases → Interface Adapters → Frameworks) — it deliberately owns no business/domain logic,
so that layering doesn't apply. It *is* organized so that a handful of framework-free
modules (`context`, `exceptions`, `config`) sit at the bottom of the graph, and everything
that touches FastAPI/Starlette/gRPC/OpenTelemetry directly depends on them rather than the
other way around — that one-way dependency is what lets a consuming service depend on
`CurrentUser` / `RequestContext` / `PlatformException` instead of on Starlette or
OpenTelemetry types directly.

> Source: [`docs/architecture/mermaid/module-dependencies.mmd`](docs/architecture/mermaid/module-dependencies.mmd)

```mermaid
graph TD
    subgraph pub["Entry point"]
        bootstrap["fastapicommon.bootstrap\ncreate_platform_app()"]
    end

    subgraph http["HTTP integration"]
        middleware["fastapicommon.middleware\nContextMiddleware · TimeoutMiddleware"]
        metrics["fastapicommon.metrics\nPrometheusMiddleware · metrics_router\ninit_metrics · PlatformMetrics\nObservabilityRegistry (registry.json)"]
        health["fastapicommon.health\nhealth_router · create_health_router · ReadinessCheck"]
        telemetry["fastapicommon.telemetry\nsetup_tracing · instrument_grpc_server\ninstrument_grpc_client"]
    end

    subgraph identity["Identity & access"]
        auth["fastapicommon.auth\nUSER_ID_HEADER · TENANT_ID_HEADER · TENANT_ROLES_HEADER\nREQUEST_ID_HEADER · CORRELATION_ID_HEADER\nget_current_user · require_authenticated_user"]
        rbac["fastapicommon.rbac\nRequireRole · RequireAnyRole · RequireAllRoles"]
    end

    subgraph core["Core (no framework dependency)"]
        context["fastapicommon.context\nCurrentUser · RequestContext\nbind/reset_request_context · request_context_scope\nget_request_context · get_tenant_id · get_user_id · get_roles"]
        exceptions["fastapicommon.exceptions\nPlatformException hierarchy\nregister_exception_handlers"]
        config["fastapicommon.config\nPlatformSettings"]
    end

    subgraph outbound["Outbound"]
        propagation["fastapicommon.propagation\ncreate_platform_http_client"]
    end

    subgraph grpcmod["gRPC (optional extra)"]
        grpc["fastapicommon.grpc\nContextServerInterceptor\nContextClientInterceptor"]
    end

    subgraph logmod["Logging"]
        logging["fastapicommon.logging\nconfigure_logging · get_logger"]
    end

    bootstrap --> config
    bootstrap --> middleware
    bootstrap --> metrics
    bootstrap --> health
    bootstrap --> telemetry
    bootstrap --> exceptions
    bootstrap --> logging

    middleware --> context
    middleware --> auth
    auth --> context
    auth --> exceptions
    rbac --> context
    rbac --> exceptions
    logging --> context
    propagation --> context
    propagation --> auth
    grpc --> context
    grpc --> auth
    grpc --> logging
    grpc --> metrics

    classDef core fill:#2b6cb0,color:#fff,stroke:#1a4971
    class context,exceptions,config core
```

**Rule:** `context`, `exceptions`, and `config` (highlighted) import nothing from the rest
of the library. Everything else depends on them, never the reverse.
`fastapicommon.grpc` is only imported when the `grpc` extra is installed — the top-level
`fastapicommon/__init__.py` never imports it, so the base install stays free of a grpcio
dependency.

---

## Public API

### fastapicommon.context

| Symbol | Description |
|--------|-------------|
| `CurrentUser` | Frozen dataclass: `user_id`, `tenant_id`, `roles: tuple[str, ...]`. `is_authenticated`, `has_role`, `has_any_role`, `has_all_roles` |
| `RequestContext` | Frozen dataclass: `request_id`, `correlation_id`, `user: CurrentUser`. Exposes `user_id`/`tenant_id`/`roles` passthrough properties |
| `bind_request_context(ctx)` / `reset_request_context(token)` | Low-level contextvar bind/reset, used by middleware and interceptors |
| `request_context_scope(ctx)` | Context manager wrapping bind/reset — use in background tasks, gRPC handlers, and tests |
| `get_request_context()` | Returns the bound `RequestContext`; **raises `LookupError`** if none is bound |
| `get_request_context_or_none()` | Same, returns `None` instead of raising |
| `get_request_id()` / `get_correlation_id()` / `get_tenant_id()` / `get_user_id()` | Return the field or `None` if unbound — never raise |
| `get_roles()` | Returns `()` if unbound — never raises |

### fastapicommon.auth

| Symbol | Description |
|--------|-------------|
| `USER_ID_HEADER`, `TENANT_ID_HEADER`, `TENANT_ROLES_HEADER`, `REQUEST_ID_HEADER`, `CORRELATION_ID_HEADER`, `TRACEPARENT_HEADER` | Canonical header name constants |
| `TENANT_ROLES_SEPARATOR` | `","` — how `x-tenant-roles` is split |
| `get_current_user()` | FastAPI dependency returning the request's `CurrentUser`, authenticated or not |
| `require_authenticated_user()` | FastAPI dependency raising `UnauthorizedException` if `user_id` is unset |

### fastapicommon.middleware

| Symbol | Description |
|--------|-------------|
| `TracingMiddleware` | One OTel SERVER span per request, continuing the inbound `traceparent`; renamed `METHOD route` (Gin syntax, e.g. `GET /items/:item_id`) with gincommon's attributes and status rules |
| `ContextMiddleware` | Reads auth/tracing headers, generates `request_id`/`correlation_id` if absent, binds `RequestContext`, echoes both ids back on the response |
| `LoggingMiddleware` | The `http_request` access log (gincommon's `LoggingMiddleware`): route template, status, `duration_ms`, ids, verified or `claimed_*` identity; level by status |
| `TimeoutMiddleware(timeout_seconds=30.0)` | Wraps `call_next` in `asyncio.wait_for`; returns a 503 `service-unavailable` problem (`REQUEST_TIMEOUT`) on timeout |

### fastapicommon.logging

| Symbol | Description |
|--------|-------------|
| `configure_logging(level=None, environment=None)` | Configures structlog with platform-gincommon's (Zap) output contract (ADR-001): JSON `level`/`ts`/`caller`/`msg` on stderr, console layout in dev/local, `LOG_LEVEL` / `LOG_SAMPLING` / `LOG_FILE`; enriched with request/trace/tenant/user context |
| `sanitize_fields` / `sanitize_log_value` (`fastapicommon.logging.sanitize`) | gincommon's log sanitiser: redact sensitive keys; strip control characters and cap values at 256 bytes |
| `get_logger(name=None)` | Returns a structlog bound logger |

### fastapicommon.metrics

| Symbol | Description |
|--------|-------------|
| `PrometheusMiddleware` | Records the `platform_http_*` metrics per request; a pass-through until `init_metrics` has run |
| `metrics_router` | `GET /metrics` — Prometheus exposition format of `metrics_registry()` (the default `REGISTRY` unless a test passed its own) |
| `MetricsOptions` / `init_metrics(options, *, registry=None)` / `init_metrics_from_settings(settings)` | Observability-standard initialization (Go `metrics.MustInitFromConfig`): env fallbacks, identity validation (`ValueError`, including a missing domain), first call in the process wins; `registry=` bypasses that for tests. `init_metrics_from_settings` does nothing when `settings.enable_metrics` is false |
| `resolve_metrics_options(options)` | Validation only — returns the resolved `MetricsIdentity` |
| `platform_metrics()` / `platform_metrics_enabled()` / `metrics_identity()` / `metrics_registry()` | Active collectors / identity / registry |
| `http_method_label(method)` | Standard HTTP method, else `OTHER` (platform label only) |
| `default_registry()` / `ObservabilityRegistry` | The vendored Platform Observability Registry (`metrics/registry.json`, byte-identical to gincommon's) |

### fastapicommon.telemetry

| Symbol | Description |
|--------|-------------|
| `TracingConfig` / `TracingConfig.from_env(...)` | Identity + export settings, resolved from the `OTEL_*` environment with gincommon's defaults |
| `init_tracing(config, exporter=None)` / `init_tracing_from_env()` / `ensure_tracing(config)` | Install the provider (Go `InitTracingWithConfig` / `InitTracingFromEnv` / `EnsureInitTelemetry`) |
| `flush_tracing()` | Export buffered spans and keep the provider; run by `create_platform_app`'s lifespan on shutdown |
| `shutdown_tracing()` | Flush and stop the active provider for the whole process (Go `Shutdown`); call once at process exit, if at all (the SDK also does at interpreter exit) |
| `new_tracer` / `noop_tracer` / `span_trace_id` / `record_span_error` / `inject_trace_context` | Go's tracing helpers (`NewTracer`, `NoopTracer`, `SpanTraceID`, `RecordSpanError`, `InjectTraceContext`) |
| `setup_tracing(app, service_name, exporter=None)` | Install tracing and add `TracingMiddleware` to an app built without `create_platform_app` |
| `instrument_grpc_client()` / `instrument_grpc_server()` | OTel's generic gRPC instrumentation (client: `traceparent` on outbound calls). The server one is not needed with `ContextServerInterceptor`, which starts its own spans |

### fastapicommon.rbac

| Symbol | Description |
|--------|-------------|
| `RequireRole(role)` | FastAPI dependency; raises `ForbiddenException` if `role` isn't in the current roles |
| `RequireAnyRole(*roles)` | Raises unless at least one role matches |
| `RequireAllRoles(*roles)` | Raises unless every role matches |

### fastapicommon.exceptions

| Symbol | Description |
|--------|-------------|
| `PlatformException(message=None, *, problem_type=None, code=None, validation_errors=())` (alias `BusinessException`) | Base class: `problem_type` fixes title/status (default `platform/internal-error`), `message` is the `detail`, `code` the `errorCode`; rendered by `exceptions.handlers.problem_response` as an RFC 9457 problem |
| `ValidationException` / `UnauthorizedException` / `ForbiddenException` / `ResourceNotFoundException` (= `NotFoundException`) / `ConflictException` / `BusinessRuleViolationException` / `RateLimitException` / `UnsupportedOperationException` / `ServiceUnavailableException` | 400 / 401 / 403 / 404 / 409 / 422 / 429 / 501 / 503 subclasses |
| `register_exception_handlers(app)` | Registers handlers for `PlatformException`, `RequestValidationError`, `StarletteHTTPException`, and generic `Exception` |

### fastapicommon.health

| Symbol | Description |
|--------|-------------|
| `health_router` | Pre-built router: `/health`, `/live` always 200; `/ready` runs no checks (always 200) |
| `create_health_router(readiness_checks=None)` | Build a router whose `/ready` runs the given sync-or-async checks, returning 503 if any fail |
| `ReadinessCheck` | Type alias for a readiness check: `Callable[[], Awaitable[bool] \| bool]` — the element type of `create_health_router`'s `readiness_checks` list |

### fastapicommon.config

| Symbol | Description |
|--------|-------------|
| `PlatformSettings` | `pydantic-settings` `BaseSettings`: `app_name`, `environment` (accepts `APP_ENV` or `ENVIRONMENT` env vars via `AliasChoices`), `log_level` (empty: environment default, as gincommon), `request_timeout_seconds`, `enable_tracing`, `enable_metrics`, `otlp_endpoint`, `observability_domain` (`OBSERVABILITY_DOMAIN`), `build_version` (`BUILD_VERSION`) |

### fastapicommon.bootstrap

| Symbol | Description |
|--------|-------------|
| `create_platform_app(app_name=None, settings=None, **fastapi_kwargs)` | See [Bootstrap flow](#bootstrap-flow) below |

### fastapicommon.propagation

| Symbol | Description |
|--------|-------------|
| `create_platform_http_client(**kwargs)` | `httpx.AsyncClient` with an `httpx.Auth` hook that stamps `x-user-id`/`x-tenant-id`/`x-request-id`/`x-correlation-id` (from the bound `RequestContext`) and `traceparent` (via `opentelemetry.propagate.inject`) onto every outbound request |

### fastapicommon.grpc (requires the `grpc` extra)

| Symbol | Description |
|--------|-------------|
| `ContextServerInterceptor` | Binds a `RequestContext` from inbound metadata around each **unary-unary** RPC; records gRPC metrics; logs completion. **Every** RPC (unary and streaming) has its exceptions sanitized: the handler's own status is kept, any other exception is aborted as `INTERNAL "internal server error"` and logged as `unclassified error` (never the message). Streaming RPCs get no context binding or metrics |
| `ContextClientInterceptor` | Propagates `x-user-id`/`x-tenant-id`/`x-request-id`/`x-correlation-id` onto outbound unary-unary call metadata from the bound `RequestContext` |

---

## Bootstrap flow

`create_platform_app` is the single entry point most services should use. It is not a
factory of independent pieces the way `platform-gincommon`'s
`ObservabilityMiddlewares`/`ProtectedMiddlewares` are — it wires everything in one call, in
a fixed order.

> Source: [`docs/architecture/mermaid/bootstrap-flow.mmd`](docs/architecture/mermaid/bootstrap-flow.mmd)

```mermaid
flowchart TD
    A([create_platform_app called]) --> B[settings = settings or PlatformSettings]
    B --> C{app_name given?}
    C -- yes --> D[settings = settings.model_copy\nupdate app_name]
    C -- no --> E
    D --> E[init_metrics_from_settings settings]
    E --> E2["configure_logging(settings.log_level, environment)\ngincommon layout: JSON on stderr, console in dev/local"]
    E2 --> T{settings.enable_tracing?}
    T -- yes --> T1["ensure_tracing(TracingConfig.from_env(...))\nrouter lifespan wrapped: flush_tracing on exit"]
    T -- no --> F
    T1 --> F[app = FastAPI title=settings.app_name]

    F --> G["add_middleware TimeoutMiddleware\ntimeout_seconds=settings.request_timeout_seconds"]
    G --> H{settings.enable_metrics?}
    H -- yes --> I["add_middleware PrometheusMiddleware\ninclude_router metrics_router"]
    H -- no --> J0
    I --> J0["add_middleware LoggingMiddleware"]
    J0 --> J["add_middleware ContextMiddleware"]
    J --> J2{settings.enable_tracing?}
    J2 -- yes --> J3["add_middleware TracingMiddleware"]
    J2 -- no --> K
    J3 --> K[include_router health_router]
    K --> L[register_exception_handlers app]
    L --> S([return app])

    Note1["Middleware added LAST runs OUTERMOST.\nAdd order here is Timeout, Prometheus, Logging, Context, Tracing —\nso the real request-time order is\nTracing (outer) -> Context -> Logging -> Prometheus -> Timeout (inner) -> route handler,\nplatform-gincommon's ObservabilityMiddlewares order."]
    J3 -.-> Note1
```

---

## Request flow through the full middleware stack

> Source: [`docs/architecture/mermaid/request-flow.mmd`](docs/architecture/mermaid/request-flow.mmd)

```mermaid
sequenceDiagram
    participant Client
    participant TR as TracingMiddleware
    participant CM as ContextMiddleware
    participant LM as LoggingMiddleware
    participant UE as UnhandledErrorMiddleware
    participant PM as PrometheusMiddleware
    participant TM as TimeoutMiddleware
    participant H as Route handler

    Note over TR,TM: Added last, so TracingMiddleware is outermost,<br/>added first, so TimeoutMiddleware is innermost.

    Client ->>+ TR: HTTP request
    Note over TR: extract traceparent (W3C), start SERVER span named after the method

    TR ->>+ CM: app(scope)
    Note over CM: read + validate x-user-id, x-tenant-id, x-tenant-roles,<br/>x-request-id, x-correlation-id (Go gincommon validation rules)<br/>generate request_id/correlation_id if absent<br/>compute trace_id from the active OTel span, client_ip from X-Forwarded-For/X-Real-IP/peer<br/>bind_request_context(...)

    CM ->>+ LM: call_next(request)
    LM ->>+ UE: app(scope)
    UE ->>+ PM: app(scope)
    Note over PM: start = time.perf_counter()

    PM ->>+ TM: call_next(request)
    Note over TM: asyncio.wait_for(call_next(request), timeout_seconds)

    TM ->>+ H: call_next(request)
    H -->>- TM: response

    alt handler exceeded timeout_seconds
        TM -->> Client: 503 application/problem+json (service-unavailable, REQUEST_TIMEOUT)
    else within timeout
        TM -->>- PM: response
    end

    Note over PM: duration = perf_counter() - start<br/>route = http_route_label(scope) (Gin-style template or "unknown")<br/>platform_http_requests_total{method, route, status_class, error_class}.inc()<br/>platform_http_request_duration_seconds{...}.observe(duration)

    PM -->>- UE: response (or an unclassified exception, counted as 500)
    Note over UE: on an exception before the response started:<br/>"unclassified error" log (types + frames, never the message),<br/>platform_unclassified_errors_total{transport="http"}.inc(),<br/>exception event (type only) on the server span,<br/>answer 500 internal-error problem — not re-raised
    UE -->>- LM: response
    Note over LM: http_request log: trace_id, request_id, method, path (route template),<br/>status, duration_ms, tenant_id / user_id (or claimed_*)<br/>info 2xx/3xx · warn 4xx · error 5xx
    LM -->>- CM: response
    Note over CM: response.headers[x-request-id] = request_id<br/>response.headers[x-correlation-id] = correlation_id<br/>response.headers[X-Request-ID] = request_id<br/>response.headers[X-Trace-ID] = trace_id (if set)<br/>reset_request_context(token) in finally

    CM -->>- TR: HTTP response
    Note over TR: rename span METHOD route, set http.route, url.path,<br/>http.response.status_code; Error status for 5xx / exception
    TR -->>- Client: HTTP response
```

A `PlatformException` (or any exception) raised inside the route handler is caught by
Starlette's `ExceptionMiddleware`, which sits *inside* routing — i.e. **inside**
`TimeoutMiddleware` in the diagram above, not shown as a separate box. That means by the
time `PrometheusMiddleware` and `ContextMiddleware` see the response, the exception has
already been converted into a JSON response by `register_exception_handlers` (see below),
so metrics and response headers are recorded correctly even on error paths.

---

## Exception handling flow

> Source: [`docs/architecture/mermaid/exception-handling-flow.mmd`](docs/architecture/mermaid/exception-handling-flow.mmd)

```mermaid
flowchart TD
    A(["Exception raised while handling a request"]) --> B{"Exception type"}

    B -->|"PlatformException subclass"| C["platform_exception_handler\nassert isinstance(exc, PlatformException)"]
    C --> D["problem_response(exc.problem_type, detail=exc.message)\n= RFC 9457 problem (same as gincommon v2)"]

    B -->|"RequestValidationError\n(pydantic/FastAPI validation)"| E["request_validation_exception_handler"]
    E --> F["problem_response(400 validation-failed,\nvalidationErrors from exc.errors())"]

    B -->|"StarletteHTTPException\n(e.g. 404 no matching route)"| G["http_exception_handler\nassert isinstance(exc, StarletteHTTPException)"]
    G --> H["problem_response(type_for_status(exc.status_code))\nunmatched route → ROUTE_NOT_FOUND; 5xx detail dropped"]

    B -->|"any other Exception\n(caught by UnhandledErrorMiddleware,\nnot re-raised to the server)"| I["unhandled_exception_handler"]
    I --> J["ERROR 'unclassified error' (structlog)\nroute · operation · ids · error_type / error_chain · stack frames\nnever the exception message"]
    J --> K["problem_response(500 internal-error) — no detail"]

    subgraph reg["register_exception_handlers(app) — called once by create_platform_app"]
        R1["add_exception_handler(PlatformException, ...)"]
        R2["add_exception_handler(RequestValidationError, ...)"]
        R3["add_exception_handler(StarletteHTTPException, ...)"]
        R4["add_exception_handler(NotImplementedError, ...) → 501"]
        R5["add_exception_handler(Exception, ...)"]
    end

    R1 -.->|"registers"| C
    R2 -.->|"registers"| E
    R3 -.->|"registers"| G
    R5 -.->|"registers"| I
```

An exception nothing classified is a programming defect. `unhandled_exception_handler` answers
a generic 500 and logs one error-level entry, `unclassified error` (logger
`fastapicommon.exceptions`), with `classified=false`, `traceId` / `trace_id` / `request_id`,
`service`, `method`, `route`, `operation` (the endpoint's `module.qualname`), `status`,
`errorCode`, `error_type` / `error_chain` (exception types along `__cause__` / `__context__` and
exception-group members, at most 8) and `stack` (`file:line in function` frames, no source lines).
It **never logs the exception message**, which may carry SQL, PII, tokens or secrets: the same
contract as platform-gincommon's (its LLD OQ-24). Each one also increments
`platform_unclassified_errors_total{transport="http"}` (gRPC: `transport="grpc"`), the metric to alert
on (gincommon LLD OQ-25); the log event is kept for diagnostics.

Starlette's `ServerErrorMiddleware` always re-raises after calling the `Exception` handler, so
the server would log the traceback *with* the message ("Exception in ASGI application").
`UnhandledErrorMiddleware` (installed by `create_platform_app` just inside `LoggingMiddleware`,
which logs the 500 as `http_request`, and outside `PrometheusMiddleware`, which records it first) catches the exception and answers
the problem itself without re-raising. If the response had already started it logs the same
entry and leaves the response incomplete.

---

## RBAC dependency flow

> Source: [`docs/architecture/mermaid/rbac-dependency-flow.mmd`](docs/architecture/mermaid/rbac-dependency-flow.mmd)

```mermaid
sequenceDiagram
    participant H as Route handler
    participant Dep as FastAPI Depends()
    participant RBAC as RequireRole / RequireAnyRole / RequireAllRoles
    participant Ctx as fastapicommon.context

    H ->>+ Dep: request dependencies resolved
    Dep ->>+ RBAC: __call__()
    RBAC ->>+ Ctx: get_roles()
    Ctx -->>- RBAC: current roles (tuple, () if unbound/anonymous)

    alt RequireRole(role)
        RBAC ->> RBAC: role in current?
    else RequireAnyRole(*roles)
        RBAC ->> RBAC: any(r in current for r in roles)?
    else RequireAllRoles(*roles)
        RBAC ->> RBAC: all(r in current for r in roles)?
    end

    alt check fails
        RBAC -->> Dep: raise ForbiddenException(...)
        Dep -->> H: 403 forbidden problem (detail "Requires role '...'")
    else check passes
        RBAC -->>- Dep: None (dependency satisfied)
        Dep -->>- H: handler executes
    end
```

Unlike `platform-gincommon`, where `AuthZMiddleware` must be manually registered *after*
`ContextMiddleware` in the Gin middleware chain (an ordering mistake that's possible to
make), FastAPI dependencies always resolve **inside** the middleware stack — so
`RequireRole` et al. can never run before `ContextMiddleware` has bound the request's
roles. There is no equivalent ordering hazard here.

---

## Tracing initialization

> Source: [`docs/architecture/mermaid/tracing-initialization.mmd`](docs/architecture/mermaid/tracing-initialization.mmd)

```mermaid
flowchart TD
    A([create_platform_app: settings.enable_tracing == true]) --> B["TracingConfig.from_env(app_name, build_version,\n  observability_domain, environment, endpoint=settings.otlp_endpoint)"]
    B --> C["OTEL_* environment, gincommon defaults:\nendpoint localhost:4317 (none/off/disabled or OTEL_TRACES_EXPORTER=none: in-process only)\ninsecure only for dev/local · sample ratio 1.0 dev/local, else 0.1\nbaggage off · ignore_remote_parent_sampled off"]
    C --> D{ensure_tracing: provider already installed?}
    D -- "service's own SDK TracerProvider" --> K([kept, never replaced])
    D -- "this library's, configured" --> K2([kept: first configured install wins])
    D -- "none, or provisional unknown_service" --> E["TracerProvider(\n  resource: service.name, service.version,\n  service.namespace = domain, deployment.environment.name,\n  sampler: ParentBased(root ratio))"]
    E --> F{endpoint set?}
    F -- yes --> G["BatchSpanProcessor(OTLPSpanExporter(endpoint, insecure))"]
    F -- no --> H["no exporter (spans in-process)"]
    G --> I["delegating global provider: inner = new provider\n(trace.set_tracer_provider at most once; old provider flushed)"]
    H --> I
    I --> J["propagator: W3C traceparent (+ baggage when enabled)\nlog otel_tracing_initialized"]
    J --> L["TracingMiddleware: one SERVER span per request\nnamed METHOD route"]
    J --> M["ContextServerInterceptor: one SERVER span per RPC\nnamed after the bounded grpc_method"]
    N(["app shutdown (lifespan)"]) --> O["flush_tracing(): force_flush (5 s); provider kept for other apps, shut down at exit"]
```

Tracing follows platform-gincommon's `otel` package: the same `OTEL_*` variables and defaults,
the same resource attributes, a parent-based ratio sampler, and one `otel_tracing_initialized`
log entry per install with the effective configuration (endpoint, exporter state, sample
ratio, ...) — the first place to look when traces are missing. Export goes to
`localhost:4317` unless `OTEL_EXPORTER_OTLP_ENDPOINT` / `settings.otlp_endpoint` say otherwise;
`none` (or `OTEL_TRACES_EXPORTER=none`) keeps spans in-process. An unreachable collector does
not block requests: `BatchSpanProcessor` retries in the background and SDK errors are logged
as `otel_error`. The global provider is set once, to a delegating provider; a later install
swaps the SDK provider behind it (tracers obtained earlier follow) and flushes the old one,
and a provider the service installed itself is never replaced.

---

## Metrics

> Source: [`docs/architecture/mermaid/metrics-flow.mmd`](docs/architecture/mermaid/metrics-flow.mmd)

```mermaid
flowchart TD
    subgraph init["Startup — init_metrics (create_platform_app / ContextServerInterceptor(settings))"]
        A0{"enable_metrics?"} -->|no| A1["nothing initialized\n(no domain needed)"]
        A0 -->|yes| A2{"domain set?\n(observability_domain or OBSERVABILITY_DOMAIN)"}
        A2 -->|no| AX["raise ValueError\n(names OBSERVABILITY_DOMAIN)"]
        A2 -->|yes| A3["resolve + validate identity against registry.json\n(domain, service, environment)"]
        A3 --> A["PlatformMetrics: platform_* collectors\nregistered on REGISTRY (first call wins)"]
    end

    subgraph http["Per HTTP request — PrometheusMiddleware"]
        B([call_next request]) --> C["route = http_route_label(scope)\n(Gin-style template or 'unknown')"]
        C --> D["platform_http_active_requests +1\nawait call_next request"]
        D --> F["platform_http_requests_total / _request_duration_seconds\n_request_size_bytes / _response_size_bytes\n{method, route, status_class, error_class}"]
        D -.->|exception| P["recorded as 500 + platform_http_panics_total, re-raised"]
    end

    subgraph grpcflow["Per gRPC unary-unary call — ContextServerInterceptor"]
        H([wrapped_behavior request, context]) --> I["original_behavior(request, context)"]
        I --> K["platform_grpc_server_handled_total / _handling_seconds\n_request_size_bytes / _response_size_bytes\n{grpc_method, ...}"]
    end

    subgraph expose["Exposition"]
        M["GET /metrics — metrics_router\n(registered only if settings.enable_metrics)"]
        M --> N["generate_latest(metrics_registry())\nContent-Type: CONTENT_TYPE_LATEST"]
    end

    A -.-> F
    A -.-> K
    A -.-> N
```

#### Platform Observability Standard metrics (`fastapicommon.metrics.platform`)

These are the only HTTP/gRPC metrics the library emits. The domain (`observability_domain` /
`OBSERVABILITY_DOMAIN`) is mandatory while metrics are enabled: without one `init_metrics`
raises `ValueError` naming `OBSERVABILITY_DOMAIN`; with `enable_metrics=False` none is needed.
`init_metrics` (called by `create_platform_app()` and `ContextServerInterceptor(settings)`)
builds the `platform_*`
collectors from their entries in the vendored registry (`src/fastapicommon/metrics/registry.json`
— names, help, label order, buckets) with the registry's const labels `domain`, `service`,
`environment` prepended to each label list (prometheus_client has no const-label support), and
registers them behind a collector that drops prometheus_client's `*_created` samples. The full
list and label semantics are in the README's *Platform Observability Standard metrics* section;
in short: `platform_build_info{version}`; `platform_http_requests_total`,
`platform_http_request_duration_seconds`, `platform_http_request_size_bytes`,
`platform_http_response_size_bytes`, `platform_http_active_requests`,
`platform_http_panics_total`, `platform_http_rate_limited_requests_total`,
`platform_http_request_timeouts_total`; and `platform_grpc_server_handled_total`,
`platform_grpc_server_handling_seconds`, `platform_grpc_server_request_size_bytes`,
`platform_grpc_server_response_size_bytes`, `platform_grpc_server_inflight_requests`,
`platform_grpc_rate_limited_requests_total`, `platform_grpc_panics_total`, and the cross-transport
`platform_unclassified_errors_total{transport}` (both series initialised to 0).

The platform `route` label is resolved *before* the handler runs by matching the request
against the app's routes (recursing into `Mount`s), like Gin's `FullPath()`, so the in-flight
gauge and the counters share one value; it is rendered Gin-style (`/items/:item_id`) and is
`unknown` for unmatched requests. An exception escaping `call_next` (Go: a recovered panic) is
recorded as a 500 plus `platform_http_panics_total`, then re-raised. Because the raw URL path
is never a label value, scanner probes and typos cannot grow label cardinality.

The pre-standard metric names were removed without a compatibility period (nothing had been
deployed); see the CHANGELOG.

---

## gRPC interceptor stack (requires the `grpc` extra)

> Source: [`docs/architecture/mermaid/grpc-interceptors.mmd`](docs/architecture/mermaid/grpc-interceptors.mmd)

```mermaid
sequenceDiagram
    participant Client as gRPC Client
    participant CCI as ContextClientInterceptor
    participant Chan as grpc Channel
    participant CSI as ContextServerInterceptor
    participant H as Service handler (unary-unary)

    Note over Client,CCI: Outbound call made from code running inside\na bound RequestContext (e.g. a client call made from an HTTP handler)

    Client ->>+ CCI: intercept_unary_unary(continuation, details, request)
    CCI ->> CCI: read get_user_id/get_tenant_id/get_request_id/get_correlation_id()
    CCI ->> CCI: append (x-user-id, x-tenant-id, x-request-id,\nx-correlation-id) to metadata, only if set
    CCI ->>+ Chan: continuation(new_details, request)
    Chan ->>+ CSI: intercept_service(continuation, handler_call_details)

    CSI ->> CSI: handler = continuation(handler_call_details)
    alt handler is None or not unary_unary
        CSI -->> Chan: return handler unchanged (streaming RPCs pass through)
    else unary_unary handler
        CSI ->> CSI: start SERVER span (traceparent from metadata),\nnamed after the bounded grpc_method
        CSI ->> CSI: _context_from_metadata(invocation_metadata)\ngenerates request_id/correlation_id if absent, trace_id from the span
        CSI ->> CSI: bind_request_context(rpc_context)
        CSI ->>+ H: original_behavior(request, context)
        H -->>- CSI: response (or raises)
        Note over CSI: finally: platform_grpc_* recorded (handled, handling_seconds, sizes, inflight)<br/>grpc_request log (method, grpc_code, duration_ms, request_id, identity)<br/>reset_request_context(token)<br/>span: rpc.response.status_code, rpc.grpc.status_code; Error for non-OK
        CSI -->>- Chan: response
    end

    Chan -->>- CCI: response
    CCI -->>- Client: response
```

`ContextServerInterceptor` binds context and records metrics for **unary-unary** RPCs only.
Streaming RPCs (unary-stream, stream-unary, stream-stream) are wrapped for error sanitizing
alone — a streaming handler that needs request context must bind it manually with
`fastapicommon.context.request_context_scope`.

**Error sanitizing (all arities).** grpcio answers a handler exception with `UNKNOWN` and
details `"Exception calling application: <message>"`, and logs the traceback with the message.
The interceptor stops both: if the handler chose a status (`context.abort`, or `set_code` then
raise) it re-aborts with that code and its details; otherwise it logs one ERROR
`unclassified error` entry (`classified=false`, `trace_id`, `request_id`, `service`,
`grpc_method`, `grpc_code=Internal`, `error_type` / `error_chain`, `stack`; never the message)
and aborts with `INTERNAL "internal server error"`. An unclassified exception still counts in
`platform_grpc_panics_total` (grpc-python's equivalent of a Go handler panic), now with code
`Internal`. This is platform-gincommon's error-sanitizer contract.

---

## Observability stack

The two per-request signals — Prometheus metrics (synchronous, in-process) and
OpenTelemetry spans (async, exported via OTLP/gRPC) — are independent; enabling one does
not require the other. This diagram reflects the actual `docker-compose.yml` stack in this
repository, verified end-to-end (traces landing in Tempo, metrics scraped by Prometheus,
logs flowing through Promtail into Loki) — see `make docker-up` / `make run` in the
[README § Observability stack (optional)](README.md#observability-stack-optional).

> Source: [`docs/architecture/mermaid/observability-stack.mmd`](docs/architecture/mermaid/observability-stack.mmd)

```mermaid
graph LR
    subgraph app["examples/fastapi_service (or any consuming service)"]
        tracemw["TracingMiddleware\nserver span METHOD route"]
        ctxmw["ContextMiddleware\nrequest_id / correlation_id / tenant_id / user_id / trace_id"]
        logmw["LoggingMiddleware\nhttp_request access log"]
        promw["PrometheusMiddleware"]
        logger["structlog, gincommon (Zap) layout\nJSON on stderr (console in dev/local)"]
    end

    subgraph local["Local stack — docker-compose.yml"]
        logfile["logs/app.log\n(LOG_FILE, JSON)"]
        promtail["promtail"]
        loki["loki :3100"]
        prometheus["prometheus :9090\nscrapes host.docker.internal:APP_PORT/metrics\n+ vendored platform rules"]
        collector["otel-collector :4317/:4318\n(OTLP/gRPC + HTTP)"]
        tempo["tempo :3200"]
        grafana["grafana :3000\n(Prometheus + Loki + Tempo datasources\ntrace <-> log correlation provisioned)"]
    end

    logmw --> logger
    logger --> logfile
    logfile --> promtail
    promtail --> loki
    loki --> grafana

    promw --> prometheus
    prometheus --> grafana

    tracemw -->|"OTLPSpanExporter\n(OTEL_EXPORTER_OTLP_ENDPOINT, default localhost:4317)"| collector
    collector --> tempo
    tempo --> grafana

    ctxmw -.->|"request_id / trace_id\ncarried in log fields"| logger
```

---

## Consuming service wiring

> Source: [`docs/architecture/mermaid/consuming-service-wiring.mmd`](docs/architecture/mermaid/consuming-service-wiring.mmd)

```mermaid
graph LR
    subgraph service["Consuming service (your code)"]
        main["main.py\nsettings = PlatformSettings()"]
        routes["router.py\n@router.get(...)\ndependencies=[Depends(RequireRole('tenant_admin'))]"]
    end

    subgraph lib["fastapicommon"]
        settings_cls["fastapicommon.config.PlatformSettings\napp_name · log_level · request_timeout_seconds\nenable_tracing · enable_metrics · otlp_endpoint"]
        bootstrap["fastapicommon.bootstrap.create_platform_app(settings=...)"]
        get_user["fastapicommon.auth.get_current_user\n/ require_authenticated_user"]
        rbac["fastapicommon.rbac.RequireRole / RequireAnyRole / RequireAllRoles"]
        client["fastapicommon.propagation.create_platform_http_client()"]
    end

    main -->|"env vars / .env"| settings_cls
    settings_cls --> bootstrap
    bootstrap -->|"FastAPI app\n(logging, middleware, metrics,\ntracing, health, exception handlers wired)"| routes
    routes -->|"Depends(get_current_user)"| get_user
    routes -->|"Depends(RequireRole(...))"| rbac
    routes -->|"outbound HTTP calls"| client
```

**Minimal wiring example** (see `examples/fastapi_service/main.py` for the full,
runnable version — this is that exact file, trimmed):

```python
from fastapi import APIRouter, Depends

from fastapicommon.auth import CurrentUser, get_current_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import NotFoundException
from fastapicommon.rbac import RequireRole

app = create_platform_app(settings=PlatformSettings())

router = APIRouter(prefix="/widgets")

@router.get("/me")
async def whoami(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"user_id": user.user_id, "tenant_id": user.tenant_id}

@router.delete("/{widget_id}", dependencies=[Depends(RequireRole("tenant_admin"))])
async def delete_widget(widget_id: str) -> dict:
    if widget_id == "missing":
        raise NotFoundException(f"widget {widget_id} not found")
    return {"status": "deleted"}

app.include_router(router)
```

---

## Documentation assets

> Source: [`docs/architecture/mermaid/documentation-assets.mmd`](docs/architecture/mermaid/documentation-assets.mmd)

```mermaid
graph LR
    subgraph sources["Documentation sources"]
        subgraph code_src["In-code"]
            docstrings["module/function docstrings\n(mypy --strict enforces public signatures)"]
        end

        subgraph arch_src["Architecture docs"]
            mmd_files["docs/architecture/mermaid/\n11 x .mmd source diagrams"]
            arch_md["ARCHITECTURE.md\nembeds mermaid blocks + prose + API tables"]
        end

        subgraph adr_src["Decision records"]
            adrs["docs/adr/ADR-001..004\nlogging · tracing · metrics · context propagation"]
        end

        subgraph other_src["Other docs"]
            readme["README.md — quick start, module table"]
            migration["docs/MigrationGuide.md — Go -> Python mapping"]
        end
    end

    subgraph rendered["Rendered / consumed by"]
        ide["IDE hover / mypy\n(docstrings + type hints)"]
        github_md["GitHub\nMarkdown + Mermaid preview"]
        pypi["PyPI project page\n(README.md as long_description)"]
    end

    docstrings --> ide
    mmd_files -->|"copied into fenced code blocks"| arch_md
    arch_md --> github_md
    adrs --> github_md
    readme --> github_md
    readme --> pypi
    migration --> github_md
```

---

## Execution model

**HTTP** — `create_platform_app`'s add order is Timeout, Prometheus, UnhandledError, Context;
since the *last*-added middleware is *outermost*, the real request-time nesting is:

```
ContextMiddleware ──────────────────────────────────────── (5) echo headers, reset context
  UnhandledErrorMiddleware ─────────────────────────────── (4) unclassified exception → 500 problem, not re-raised
    PrometheusMiddleware ────────────────────────────────── (3) observe duration + status (records the 500 first)
      TimeoutMiddleware ─────────────────────────────────── (2) asyncio.wait_for(...)
        Route handler / FastAPI dependency resolution       (1) executes here
```

Steps run top-to-bottom before the handler, then bottom-to-top after it returns (or times
out). Because `ContextMiddleware` is outermost, `get_request_context()` is safe to call
from *any* dependency or handler, as well as from inside `PrometheusMiddleware` and
`TimeoutMiddleware` if they needed it (they currently don't).

**Cooperative cancellation caveat:** `TimeoutMiddleware` cancels the `call_next(request)`
task via `asyncio.wait_for`. Python cancellation is cooperative — if the handler is
executing a long synchronous, blocking call with no `await` point, cancellation only takes
effect the next time the handler actually yields control. The client still receives the 503
promptly; a slow handler is not guaranteed to stop executing instantly.

**gRPC** — `ContextServerInterceptor` wraps `unary_unary` handlers directly (no interceptor
chain composition helper exists, unlike `platform-gincommon`'s
`FullUnaryInterceptors`/`FullStreamInterceptors`). If you need panic-style recovery,
request-ID generation as a *separate* concern, or per-RPC RBAC on the gRPC side, those are
not provided by this library today — see [Platform contract](#platform-contract) below.

---

## Failure domains

| Component | Failure mode | Impact | Fallback |
|-----------|-------------|--------|----------|
| **Prometheus metrics** | `settings.enable_metrics=False` | `PrometheusMiddleware` never installed, `/metrics` route absent (404) | No recording anywhere; no crash |
| **OTLP tracing export** | Collector unreachable | `BatchSpanProcessor` retries asynchronously in a background thread; logs `Transient error ... retrying` | Request handling is never blocked or failed by export failures |
| **OTLP tracing export** | Export disabled (`OTEL_EXPORTER_OTLP_ENDPOINT=none` / `OTEL_TRACES_EXPORTER=none`) | `otel_tracing_initialized` reports `exporter: disabled (no endpoint)` | Spans created with valid trace IDs but never exported |
| **Structured logging** | N/A | `get_logger()` always returns a usable logger; there is no "nil logger" failure mode in this library | — |
| **RBAC** | `RequireRole`/`RequireAnyRole`/`RequireAllRoles` check fails | `ForbiddenException` → `403` `platform/forbidden` problem via the registered exception handler | Does not affect other routes or middleware |
| **Missing/unbound context** | Code calls `get_request_context()` outside `ContextMiddleware` or `request_context_scope` (e.g. a detached background task) | Raises `LookupError` | Use `get_request_context_or_none()` or the individual `get_*` accessors, which return `None`/`()`/`""` instead |
| **Request timeout** | Handler exceeds `request_timeout_seconds` | `503` `platform/service-unavailable` problem, `errorCode` `REQUEST_TIMEOUT` | Server continues serving other requests; see cooperative-cancellation caveat above |
| **Unhandled exception** | Any exception not a `PlatformException`/`RequestValidationError`/`StarletteHTTPException` | `UnhandledErrorMiddleware` stops it (never re-raised to the server); one ERROR `unclassified error` entry with types and frames, never the message; a `500` `platform/internal-error` problem without `detail` | Does not crash the process |

No external dependency — OTLP collector, Prometheus scrape, or log sink — can cause request
handling to fail; all are either in-process (metric counters), async (OTLP batched
export), or degrade to a JSON error response (RBAC, timeout, unhandled exception).

---

## Platform contract

Services that use this library should uphold the following. Violating these doesn't crash
anything, but produces broken tracing, misleading metrics, or an unintended trust boundary.

| Requirement | Why it matters |
|-------------|----------------|
| **Requests enter through a trusted API gateway** | `ContextMiddleware` reads `x-user-id` / `x-tenant-id` / `x-tenant-roles` verbatim from headers — it does not verify them cryptographically. A client that can reach the service directly can claim any identity. |
| **`/health`, `/live`, `/ready` are never wrapped in an auth-requiring dependency** | They are registered without any RBAC/auth dependency by design, so load balancer and k8s probes (which don't send identity headers) always succeed. Adding `Depends(require_authenticated_user)` to a health route breaks liveness/readiness probing. |
| **Outbound calls use `create_platform_http_client()` (or manually propagate headers)** | Without it, `traceparent`/`x-request-id`/`x-correlation-id` don't reach the next service — the distributed trace and cross-service log correlation break at that call boundary. |
| **`OTEL_EXPORTER_OTLP_ENDPOINT` points at the collector wherever traces are expected** | The default is `localhost:4317` (gincommon's); in a pod without a local collector, set it, or spans are retried and dropped (`otel_error`). |
| **`app_name` is set per service (via `PlatformSettings` or `create_platform_app(app_name=...)`)** | Used as the FastAPI title and the OTel `service.name` resource attribute (Tempo's service map, Grafana's trace search). It is also the `service` const label of every `platform_*` metric (the observability standard). |
| **gRPC streaming RPCs bind context manually if they need it** | `ContextServerInterceptor` binds context for unary-unary calls only; a streaming handler reading `get_request_context()` without first calling `request_context_scope` will raise `LookupError`. |

---

## Key invariants

| Invariant | Where enforced |
|-----------|---------------|
| `CurrentUser.roles` defaults to `()`, never `None` | `context/models.py` — RBAC checks (`role in current`) never need a null check |
| `get_request_context()` raises `LookupError` when unbound; every other accessor (`get_tenant_id`, `get_user_id`, `get_roles`, `get_request_id`, `get_correlation_id`, `get_trace_id`, `get_client_ip`) returns `None`/`()`/`""` instead | `context/vars.py` — code that only *wants* the current tenant if any never needs a try/except; code that truly requires a context opts into the strict accessor |
| Middleware add order is Timeout → Prometheus → Context, so runtime nesting is Context (outer) → Prometheus → Timeout (inner) → handler | `bootstrap/app.py` — context is available to every layer beneath it, including the handler and its dependencies |
| OTLP `insecure` defaults to `True` when `environment` is `dev` / `local`, else `False` | `telemetry/tracing.py::TracingConfig.from_env` — mirrors `platform-gincommon`'s `OTEL_EXPORTER_OTLP_INSECURE` default logic |
| `PrometheusMiddleware` labels by the matched route's path *template*, not the raw URL, when a route matches | `metrics/middleware.py` — keeps cardinality bounded for real traffic; see the caveat above for the unmatched-route (404) case |
| No gRPC exception message reaches a client or a log | `grpc/server.py` `_sanitize` — every arity; the handler's own status is kept, anything else is `INTERNAL "internal server error"` + `unclassified error` (types, frames) |
| Streaming RPCs are never double-instrumented | `grpc/server.py` — only unary-unary calls get context binding and metrics |
| `TimeoutMiddleware(timeout_seconds=...)` always wraps `call_next` in `asyncio.wait_for`, even for very large timeouts | `middleware/timeout.py` — there's no "disabled" sentinel value; set a large `request_timeout_seconds` to effectively disable it |
| Every registered exception handler (and `TimeoutMiddleware`) renders through `problem_response`, producing an RFC 9457 problem identical to gincommon v2's (types, titles, error codes, traceId order, validation messages) | `exceptions/handlers.py`, `problem/` — `tests/unit/problem` checks the platform types against gincommon's `docs/problem-details/registry.md` |
| `fastapicommon.grpc` is never imported by `fastapicommon/__init__.py` | Base install has no `grpcio` dependency; only importing `fastapicommon.grpc` directly requires the `grpc` extra |
