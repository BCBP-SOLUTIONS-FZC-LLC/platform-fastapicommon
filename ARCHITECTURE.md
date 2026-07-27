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
        metrics["fastapicommon.metrics\nPrometheusMiddleware · metrics_router\nHTTP_REQUESTS_TOTAL · HTTP_REQUEST_DURATION_SECONDS\nGRPC_REQUESTS_TOTAL · GRPC_REQUEST_DURATION_SECONDS\nrecord_grpc_request"]
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
| `ContextMiddleware` | Reads auth/tracing headers, generates `request_id`/`correlation_id` if absent, binds `RequestContext`, echoes both ids back on the response |
| `TimeoutMiddleware(timeout_seconds=30.0)` | Wraps `call_next` in `asyncio.wait_for`; returns 504 JSON on timeout |

### fastapicommon.logging

| Symbol | Description |
|--------|-------------|
| `configure_logging(level=logging.INFO)` | Configures structlog for JSON-only output (ADR-001), enriched with request/tenant/user context via a custom processor |
| `get_logger(name=None)` | Returns a structlog bound logger |

### fastapicommon.metrics

| Symbol | Description |
|--------|-------------|
| `HTTP_REQUESTS_TOTAL` / `HTTP_REQUEST_DURATION_SECONDS` | Counter/Histogram, labels `method, path, status_code` / `method, path` |
| `GRPC_REQUESTS_TOTAL` / `GRPC_REQUEST_DURATION_SECONDS` | Counter/Histogram, labels `method, status_code` / `method` |
| `record_grpc_request(method, status_code, duration_seconds)` | Records one gRPC call — called by `ContextServerInterceptor` |
| `PrometheusMiddleware` | Records the two HTTP metrics per request |
| `metrics_router` | `GET /metrics` — Prometheus exposition format |

### fastapicommon.telemetry

| Symbol | Description |
|--------|-------------|
| `setup_tracing(app, service_name, exporter=None)` | Builds a `TracerProvider`, optionally attaches a `BatchSpanProcessor(exporter)`, instruments the FastAPI app. Returns the provider |
| `instrument_grpc_server()` / `instrument_grpc_client()` | Instrument gRPC server/client channels for tracing (requires the `grpc` extra) |

### fastapicommon.rbac

| Symbol | Description |
|--------|-------------|
| `RequireRole(role)` | FastAPI dependency; raises `ForbiddenException` if `role` isn't in the current roles |
| `RequireAnyRole(*roles)` | Raises unless at least one role matches |
| `RequireAllRoles(*roles)` | Raises unless every role matches |

### fastapicommon.exceptions

| Symbol | Description |
|--------|-------------|
| `PlatformException(message=None)` | Base class; `status_code=500`, `code="internal_error"` (Python-side categorization only, not on the wire); serialized via `exceptions.handlers._error_response` → `{error, status, trace_id, request_id}` |
| `UnauthorizedException` / `ForbiddenException` / `ValidationException` / `ConflictException` / `NotFoundException` | 401 / 403 / 422 / 409 / 404 subclasses |
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
| `PlatformSettings` | `pydantic-settings` `BaseSettings`: `app_name`, `environment` (accepts `APP_ENV` or `ENVIRONMENT` env vars via `AliasChoices`), `log_level`, `request_timeout_seconds`, `enable_tracing`, `enable_metrics`, `otlp_endpoint` |

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
| `ContextServerInterceptor` | Binds a `RequestContext` from inbound metadata around each **unary-unary** RPC; records gRPC metrics; logs completion. Streaming RPCs pass through unmodified |
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
    D --> E[configure_logging settings.log_level]
    E --> F[app = FastAPI title=settings.app_name]

    F --> G["add_middleware TimeoutMiddleware\ntimeout_seconds=settings.request_timeout_seconds"]
    G --> H{settings.enable_metrics?}
    H -- yes --> I["add_middleware PrometheusMiddleware\ninclude_router metrics_router"]
    H -- no --> J
    I --> J["add_middleware ContextMiddleware"]

    J --> K[include_router health_router]
    K --> L[register_exception_handlers app]
    L --> M{settings.enable_tracing?}
    M -- yes --> N["_build_span_exporter settings"]
    N --> O{settings.otlp_endpoint set?}
    O -- yes --> P["OTLPSpanExporter\ninsecure = environment in dev/development/local"]
    O -- no --> Q["exporter = None\nspans recorded, not exported"]
    P --> R[setup_tracing app, service_name, exporter]
    Q --> R
    M -- no --> S
    R --> S([return app])

    Note1["Middleware added LAST runs OUTERMOST.\nAdd order here is Timeout, Prometheus, Context —\nso the real request-time order is\nContext (outer) -> Prometheus -> Timeout (inner) -> route handler.\nContext is bound before metrics/timeout run."]
    J -.-> Note1
```

---

## Request flow through the full middleware stack

> Source: [`docs/architecture/mermaid/request-flow.mmd`](docs/architecture/mermaid/request-flow.mmd)

```mermaid
sequenceDiagram
    participant Client
    participant CM as ContextMiddleware
    participant PM as PrometheusMiddleware
    participant TM as TimeoutMiddleware
    participant H as Route handler

    Note over CM,TM: Added last, so ContextMiddleware is outermost,<br/>added first, so TimeoutMiddleware is innermost.

    Client ->>+ CM: HTTP request
    Note over CM: read + validate x-user-id, x-tenant-id, x-tenant-roles,<br/>x-request-id, x-correlation-id (Go gincommon validation rules)<br/>generate request_id/correlation_id if absent<br/>compute trace_id from the active OTel span, client_ip from X-Forwarded-For/X-Real-IP/peer<br/>bind_request_context(...)

    CM ->>+ PM: call_next(request)
    Note over PM: start = time.perf_counter()

    PM ->>+ TM: call_next(request)
    Note over TM: asyncio.wait_for(call_next(request), timeout_seconds)

    TM ->>+ H: call_next(request)
    H -->>- TM: response

    alt handler exceeded timeout_seconds
        TM -->> Client: 504 {"error":"...", "status":504, "trace_id":"...", "request_id":"..."}
    else within timeout
        TM -->>- PM: response
    end

    Note over PM: duration = perf_counter() - start<br/>route = request.scope["route"] (path template)<br/>HTTP_REQUESTS_TOTAL.labels(method, path, status_code).inc()<br/>HTTP_REQUEST_DURATION_SECONDS.labels(method, path).observe(duration)

    PM -->>- CM: response
    Note over CM: response.headers[x-request-id] = request_id<br/>response.headers[x-correlation-id] = correlation_id<br/>response.headers[X-Request-ID] = request_id<br/>response.headers[X-Trace-ID] = trace_id (if set)<br/>reset_request_context(token) in finally

    CM -->>- Client: HTTP response
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
    C --> D["_error_response(exc.status_code, exc.message)\n= {error, status, trace_id, request_id}\n(matches Go gincommon's ErrorResponse exactly)"]

    B -->|"RequestValidationError\n(pydantic/FastAPI validation)"| E["request_validation_exception_handler"]
    E --> F["_error_response(422, str(exc))"]

    B -->|"StarletteHTTPException\n(e.g. 404 no matching route)"| G["http_exception_handler\nassert isinstance(exc, StarletteHTTPException)"]
    G --> H["_error_response(exc.status_code, str(exc.detail))"]

    B -->|"any other Exception"| I["unhandled_exception_handler"]
    I --> J["logging.getLogger('fastapicommon.exceptions')\n.exception(...)"]
    J --> K["_error_response(500, 'An unexpected error occurred')"]

    subgraph reg["register_exception_handlers(app) — called once by create_platform_app"]
        R1["add_exception_handler(PlatformException, ...)"]
        R2["add_exception_handler(RequestValidationError, ...)"]
        R3["add_exception_handler(StarletteHTTPException, ...)"]
        R4["add_exception_handler(Exception, ...)"]
    end

    R1 -.->|"registers"| C
    R2 -.->|"registers"| E
    R3 -.->|"registers"| G
    R4 -.->|"registers"| I
```

Note that `unhandled_exception_handler` logs via the stdlib `logging` module directly
(`logging.getLogger("fastapicommon.exceptions")`), not `fastapicommon.logging.get_logger` —
a deliberate choice to avoid the exceptions module depending on the logging module's
structlog configuration state. The log line for unhandled exceptions is therefore plain
stdlib logging, not structlog JSON, even though the rest of the application's logs are.

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
        Dep -->> H: 403 {"error":"Requires role '...'","status":403,"trace_id":"...","request_id":"..."}
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
    A([create_platform_app: settings.enable_tracing == true]) --> B["_build_span_exporter(settings)"]
    B --> C{settings.otlp_endpoint set?}
    C -- no --> D["return None\n(spans recorded, never exported)"]
    C -- yes --> E["OTLPSpanExporter(\n  endpoint=settings.otlp_endpoint,\n  insecure=environment in ('dev','development','local')\n)"]

    D --> F[setup_tracing app, service_name, exporter]
    E --> F

    F --> G["Resource.create({SERVICE_NAME: service_name})"]
    G --> H["provider = TracerProvider(resource=resource)"]
    H --> I{exporter is not None?}
    I -- yes --> J["provider.add_span_processor(\n  BatchSpanProcessor(exporter))"]
    I -- no --> K
    J --> K["trace.set_tracer_provider(provider)"]
    K --> L["FastAPIInstrumentor.instrument_app(\n  app, tracer_provider=provider)"]
    L --> M([return provider])

    N(["instrument_grpc_server() / instrument_grpc_client()\n(called separately by the gRPC service, requires the grpc extra)"]) -.-> O["GrpcInstrumentorServer / GrpcInstrumentorClient\n.instrument()"]
```

If a collector is unreachable at the configured `otlp_endpoint`, `BatchSpanProcessor`
retries exports asynchronously in the background (visible as
`Transient error ... retrying` log lines) — it does not block or fail request handling.
If `otlp_endpoint` is simply never set, there is no error or warning at all; spans are
created (valid trace IDs, visible to `FastAPIInstrumentor`) but silently never leave the
process. This is a common source of "why don't I see any traces" confusion — check
`PlatformSettings.otlp_endpoint` / the `OTLP_ENDPOINT` env var first.

---

## Metrics

> Source: [`docs/architecture/mermaid/metrics-flow.mmd`](docs/architecture/mermaid/metrics-flow.mmd)

```mermaid
flowchart TD
    subgraph init["Module import (once per process — no explicit Init call needed)"]
        A["fastapicommon.metrics.definitions\nmodule-level Counter/Histogram objects\nregistered on prometheus_client's default REGISTRY at import time"]
    end

    subgraph http["Per HTTP request — PrometheusMiddleware"]
        B([call_next request]) --> C["start = time.perf_counter()"]
        C --> D[await call_next request]
        D --> E["route = request.scope.get('route')\npath = route.path if route else request.url.path"]
        E --> F["HTTP_REQUESTS_TOTAL.labels(method, path, status_code).inc()"]
        F --> G["HTTP_REQUEST_DURATION_SECONDS.labels(method, path).observe(duration)"]
    end

    subgraph grpcflow["Per gRPC unary-unary call — ContextServerInterceptor"]
        H([wrapped_behavior request, context]) --> I["original_behavior(request, context)"]
        I --> J["record_grpc_request(method, status_code, duration_seconds)"]
        J --> K["GRPC_REQUESTS_TOTAL.labels(method, status_code).inc()"]
        J --> L["GRPC_REQUEST_DURATION_SECONDS.labels(method).observe(duration_seconds)"]
    end

    subgraph expose["Exposition"]
        M["GET /metrics — metrics_router\n(registered only if settings.enable_metrics)"]
        M --> N["generate_latest()\nContent-Type: CONTENT_TYPE_LATEST"]
    end

    A -.-> F
    A -.-> G
    A -.-> K
    A -.-> L
    A -.-> N
```

#### Registered Prometheus metrics

| Metric | Type | Labels |
|--------|------|--------|
| `http_requests_total` | Counter | method, path, status_code |
| `http_request_duration_seconds` | Histogram | method, path |
| `grpc_requests_total` | Counter | method, status_code |
| `grpc_request_duration_seconds` | Histogram | method |

Unlike `platform-gincommon`, these metrics carry **no built-in `service`/`version` const
label** — `fastapicommon` has no equivalent of the Go library's `Config.ServiceName`
applied as a const label at registration time. If you run several `fastapicommon` services
behind one Prometheus, rely on Prometheus's own `job`/`instance` labels (one scrape target
per service) to distinguish them, not a label emitted by this library.

**Cardinality caveat:** `PrometheusMiddleware` uses the matched route's path *template*
(e.g. `/widgets/{id}`) when a route matches, which keeps cardinality bounded for real
traffic. When no route matches at all (404s from scanner probes, typos, etc.),
`request.scope.get("route")` is `None` and the middleware falls back to the **raw URL
path** — so arbitrary unmatched paths each get their own `http_requests_total` label
combination. `platform-gincommon` explicitly normalizes this case to a constant
`"unknown"` label; `fastapicommon` does not (yet) do the same. If you expose a service to
untrusted traffic, be aware this can grow the `path` label's cardinality over time.

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
        CSI ->> CSI: _context_from_metadata(invocation_metadata)\ngenerates request_id/correlation_id if absent
        CSI ->> CSI: bind_request_context(rpc_context)
        CSI ->>+ H: original_behavior(request, context)
        H -->>- CSI: response (or raises)
        Note over CSI: finally: record_grpc_request(method, status_code, duration)<br/>logger.info("grpc_request_completed", ...)<br/>reset_request_context(token)
        CSI -->>- Chan: response
    end

    Chan -->>- CCI: response
    CCI -->>- Client: response
```

`ContextServerInterceptor` only wraps **unary-unary** RPCs. Streaming RPCs (unary-stream,
stream-unary, stream-stream) are returned unmodified — a streaming handler that needs
request context must bind it manually with
`fastapicommon.context.request_context_scope`.

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
        ctxmw["ContextMiddleware\nrequest_id / correlation_id / tenant_id / user_id"]
        promw["PrometheusMiddleware"]
        logger["structlog JSON logs\n(get_logger, stdout)"]
        tracer["OTel spans\n(FastAPIInstrumentor)"]
    end

    subgraph local["Local stack — docker-compose.yml"]
        logfile["logs/app.log\n(stdout teed by `make run`)"]
        promtail["promtail"]
        loki["loki :3100"]
        prometheus["prometheus :9090\nscrapes host.docker.internal:APP_PORT/metrics"]
        collector["otel-collector :4317/:4318\n(OTLP/gRPC + HTTP)"]
        tempo["tempo :3200"]
        grafana["grafana :3000\n(Prometheus + Loki + Tempo datasources\ntrace <-> log correlation provisioned)"]
    end

    logger --> logfile
    logfile --> promtail
    promtail --> loki
    loki --> grafana

    promw --> prometheus
    prometheus --> grafana

    tracer -->|"OTLPSpanExporter\n(only if OTLP_ENDPOINT / settings.otlp_endpoint set)"| collector
    collector --> tempo
    tempo --> grafana

    ctxmw -.->|"request_id/correlation_id\ncarried in log fields"| logger
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

**HTTP** — `create_platform_app`'s add order is Timeout, Prometheus, Context; since the
*last*-added `BaseHTTPMiddleware` is *outermost*, the real request-time nesting is:

```
ContextMiddleware ──────────────────────────────────────── (4) echo headers, reset context
  PrometheusMiddleware ──────────────────────────────────── (3) observe duration + status
    TimeoutMiddleware ───────────────────────────────────── (2) asyncio.wait_for(...)
      Route handler / FastAPI dependency resolution         (1) executes here
```

Steps run top-to-bottom before the handler, then bottom-to-top after it returns (or times
out). Because `ContextMiddleware` is outermost, `get_request_context()` is safe to call
from *any* dependency or handler, as well as from inside `PrometheusMiddleware` and
`TimeoutMiddleware` if they needed it (they currently don't).

**Cooperative cancellation caveat:** `TimeoutMiddleware` cancels the `call_next(request)`
task via `asyncio.wait_for`. Python cancellation is cooperative — if the handler is
executing a long synchronous, blocking call with no `await` point, cancellation only takes
effect the next time the handler actually yields control. The client still receives a 504
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
| **OTLP tracing export** | `otlp_endpoint` unset | No error, no warning | Spans created with valid trace IDs but never exported |
| **Structured logging** | N/A | `get_logger()` always returns a usable logger; there is no "nil logger" failure mode in this library | — |
| **RBAC** | `RequireRole`/`RequireAnyRole`/`RequireAllRoles` check fails | `ForbiddenException` → `403 {"error":"...", "status":403, "trace_id":"...", "request_id":"..."}` via the registered exception handler | Does not affect other routes or middleware |
| **Missing/unbound context** | Code calls `get_request_context()` outside `ContextMiddleware` or `request_context_scope` (e.g. a detached background task) | Raises `LookupError` | Use `get_request_context_or_none()` or the individual `get_*` accessors, which return `None`/`()`/`""` instead |
| **Request timeout** | Handler exceeds `request_timeout_seconds` | `504 {"error":"...", "status":504, "trace_id":"...", "request_id":"..."}` | Server continues serving other requests; see cooperative-cancellation caveat above |
| **Unhandled exception** | Any exception not a `PlatformException`/`RequestValidationError`/`StarletteHTTPException` | Logged via stdlib `logging`, then `500 {"error":"...", "status":500, "trace_id":"...", "request_id":"..."}` | Does not crash the process |

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
| **`OTLP_ENDPOINT` / `settings.otlp_endpoint` is set wherever traces are expected** | Tracing is silently a no-op (spans recorded, never exported) when unset — there is no error to alert on. |
| **`app_name` is set per service (via `PlatformSettings` or `create_platform_app(app_name=...)`)** | Used as the FastAPI title and the OTel `service.name` resource attribute (Tempo's service map, Grafana's trace search). It is *not* applied as a Prometheus const label — see the metrics cardinality note above for why Prometheus `job`/`instance` labels, not this library, distinguish services in dashboards. |
| **gRPC streaming RPCs bind context manually if they need it** | `ContextServerInterceptor` only wraps unary-unary calls; a streaming handler reading `get_request_context()` without first calling `request_context_scope` will raise `LookupError`. |

---

## Key invariants

| Invariant | Where enforced |
|-----------|---------------|
| `CurrentUser.roles` defaults to `()`, never `None` | `context/models.py` — RBAC checks (`role in current`) never need a null check |
| `get_request_context()` raises `LookupError` when unbound; every other accessor (`get_tenant_id`, `get_user_id`, `get_roles`, `get_request_id`, `get_correlation_id`, `get_trace_id`, `get_client_ip`) returns `None`/`()`/`""` instead | `context/vars.py` — code that only *wants* the current tenant if any never needs a try/except; code that truly requires a context opts into the strict accessor |
| Middleware add order is Timeout → Prometheus → Context, so runtime nesting is Context (outer) → Prometheus → Timeout (inner) → handler | `bootstrap/app.py` — context is available to every layer beneath it, including the handler and its dependencies |
| OTLP `insecure` defaults to `True` when `environment` is `dev`/`development`/`local`, else `False` | `bootstrap/app.py::_build_span_exporter` — mirrors `platform-gincommon`'s `OTEL_EXPORTER_OTLP_INSECURE` default logic |
| `PrometheusMiddleware` labels by the matched route's path *template*, not the raw URL, when a route matches | `metrics/middleware.py` — keeps cardinality bounded for real traffic; see the caveat above for the unmatched-route (404) case |
| `ContextServerInterceptor` passes through any handler where `handler.unary_unary` is falsy | `grpc/server.py` — streaming RPCs are never wrapped, never double-instrumented |
| `TimeoutMiddleware(timeout_seconds=...)` always wraps `call_next` in `asyncio.wait_for`, even for very large timeouts | `middleware/timeout.py` — there's no "disabled" sentinel value; set a large `request_timeout_seconds` to effectively disable it |
| Every registered exception handler serializes through the same `_error_response(status, error)` helper, always producing exactly `{"error": ..., "status": ..., "trace_id": ..., "request_id": ...}` — matching Go `gincommon`'s `ErrorResponse` field-for-field | `exceptions/handlers.py` — API consumers (Go or Python) can parse errors uniformly regardless of which handler fired or which language served the response |
| `fastapicommon.grpc` is never imported by `fastapicommon/__init__.py` | Base install has no `grpcio` dependency; only importing `fastapicommon.grpc` directly requires the `grpc` extra |
