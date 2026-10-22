# Architecture

The Go module's layers (`internal/core`, adapters, `pkg/`) become one package under `src/fastapicommon/`: a framework-free core, FastAPI / Starlette / gRPC / OpenTelemetry adapters, and one composition function. Python has no `internal/` rule: the public surface is each subpackage's `__all__`; underscore modules (`_errors`, `_scope`) are private by convention. The narrative with 16 diagrams is [`ARCHITECTURE.md`](../ARCHITECTURE.md); exact signatures and invariants are in the [LLD](../docs/lld/platform-fastapicommon-lld.md) §3–§6.

## Package Layout

```
platform-fastapicommon/
├── src/fastapicommon/
│   ├── __init__.py              # __version__ ("0.4.0", pinned by tests/unit/test_package.py), create_platform_app — imports only bootstrap
│   ├── bootstrap/app.py         # create_platform_app(app_name=None, settings=None, **fastapi_kwargs): logging → metrics → tracing,
│   │                            #   middleware order, health + metrics routers, exception handlers, lifespan wrapping (flush_tracing on exit, when tracing is on)
│   ├── config/settings.py       # PlatformSettings (pydantic-settings; .env + environment)
│   ├── context/{models,vars}.py # CurrentUser, RequestContext (+ identity_rejection), contextvars accessors, request_context_scope
│   ├── auth/
│   │   ├── headers.py           # header-name constants
│   │   ├── validation.py        # normalize_header_value, normalize_request_id, parse_roles(_strict), resolve_identity, resolve_ids,
│   │   │                        #   MAX_ROLES_HEADER_LEN = 4096, MAX_ROLES = 64 (gincommon domain/validation.go)
│   │   └── dependencies.py      # get_current_user, require_authenticated_user (401 details per rejection reason)
│   ├── rbac/dependencies.py     # RequireRole, RequireAnyRole, RequireAllRoles (403 + authz: access denied)
│   ├── middleware/
│   │   ├── context.py           # ContextMiddleware(app, trusted_proxies=None); client_ip (Gin ClientIP + SetTrustedProxies),
│   │   │                        #   parse_trusted_proxies; sets scope[CLIENT_IP]
│   │   ├── tracing.py           # TracingMiddleware: server span METHOD route, client.address, aborted / panic marking
│   │   ├── logging.py           # LoggingMiddleware: http_request, identity_log_fields (verified vs claimed_*)
│   │   └── timeout.py           # TimeoutMiddleware: 503 REQUEST_TIMEOUT if no response started in time
│   ├── exceptions/
│   │   ├── errors.py            # PlatformException (= BusinessException) + one subclass per platform type
│   │   ├── handlers.py          # register_exception_handlers, problem_response, unclassified logging
│   │   └── middleware.py        # UnhandledErrorMiddleware: answers the 500 itself, never re-raises to the server
│   ├── problem/{core,validation}.py  # RFC 9457 model, ProblemType registry (15 platform types), build_problem,
│   │                            #   resolve_trace_id, problem_from_response / parse_problem, field_path
│   ├── logging/
│   │   ├── config.py            # configure_logging: Zap JSON / console renderers, LOG_LEVEL / LOG_SAMPLING / LOG_FILE, caller +
│   │   │                        #   stacktrace, env_bool (shared with tracing)
│   │   ├── logger.py · sanitize.py · audit.py   # get_logger; redaction + truncate_utf8; auth / authz audit entries
│   ├── metrics/
│   │   ├── registry.json        # VENDORED from platform-gincommon (byte-identical; never edit here)
│   │   ├── registry.py          # ObservabilityRegistry: strict parse, Tier 3 checks, lookup
│   │   ├── platform.py          # PlatformMetrics (collectors from registry entries), init_metrics, label helpers, http_route_label
│   │   ├── middleware.py        # PrometheusMiddleware (records at request end; body bytes sent; "rate limited" log)
│   │   ├── api.py               # new_counter / gauge / histogram (Tier 3), new_shared_* (Tier 1/2), register_labeled
│   │   ├── router.py · compliance.py · references.py · inventory.py · lint.py   # /metrics; validate(); refs; inventory; CLI
│   ├── telemetry/tracing.py     # TracingConfig, delegating provider, ensure / init / flush / shutdown, tracer helpers
│   ├── grpc/{server,client}.py  # Context(Async)ServerInterceptor, ContextClientInterceptor (grpc extra)
│   ├── health/router.py         # /health, /live, /ready (create_health_router(readiness_checks=...))
│   ├── propagation/client.py    # create_platform_http_client (httpx.Auth hook stamping ids, identity, traceparent)
│   ├── _errors.py               # exception type names, error chain, frames, type-only span event, emit_unclassified
│   └── _scope.py                # per-request scope keys (ABORTED, PANICKED, CLIENT_IP), 499, request_outcome()
├── examples/fastapi_service · grpc_service   # reference services (the image runs fastapi_service)
├── tests/unit/<subpackage>/ · integration/ · e2e/   # conftest.py isolates metrics and tracing per test
├── docs/{lld,architecture,observability,adr}/ · problem-details.md · MigrationGuide.md
├── docker/ · docker-compose.yml # local observability stack; vendored Prometheus rules
├── .github/{workflows,scripts}/ # gincommon's CI/CD, translated to Python
└── .claude/                     # this reference set
```

## Dependency Rules

| Package | May import (internal) |
|---|---|
| `context`, `config`, `problem`, `health`, `_scope` | nothing |
| `auth`, `rbac` | `context`, `exceptions`, `logging` |
| `metrics` | `_scope`, `config`, `logging` |
| `logging` | `auth`, `context` (+ `metrics`, `middleware` lazily) |
| `exceptions` | `_errors`, `_scope`, `context`, `logging`, `problem` (+ `metrics`, `middleware` lazily) |
| `middleware` | `_errors`, `_scope`, `auth`, `context`, `exceptions`, `logging`, `metrics`, `problem` |
| `telemetry` | `_errors`, `logging` (+ `middleware` lazily) |
| `grpc` | `_errors`, `auth`, `config`, `context`, `logging`, `metrics` |
| `propagation` | `auth`, `context` |
| `bootstrap` | the composition layer |

No import-linter contract enforces this; keep new imports inside the table and break cycles with a function-local import commented `# avoid an import cycle`. Graph: [`module-dependencies.mmd`](../docs/architecture/mermaid/module-dependencies.mmd).

## Middleware Order

`create_platform_app` calls `add_middleware` innermost first; the effective order, outermost first:

| # | Layer | Why here |
|---|---|---|
| 1 | `TracingMiddleware` (if `enable_tracing`) | the span covers everything, including context binding and the access log |
| 2 | `ContextMiddleware` | validated ids / identity / client IP for every inner layer; response id headers |
| 3 | `LoggingMiddleware` | `http_request` after the response, with the final status |
| 4 | `UnhandledErrorMiddleware` | turns an unclassified exception into the 500 problem *inside* the log, so `http_request` records 500 |
| 5 | `PrometheusMiddleware` (if `enable_metrics`) | records at request end (latency includes the body) |
| 6 | `TimeoutMiddleware` | innermost: only the handler is timed |

The route label (Gin syntax, `/items/:item_id`, or `unknown`) is matched once per request and kept in the scope; Tracing and Logging resolve it before calling the app, so Mounts are labelled correctly without metrics.

## Process State

- **Metrics:** `init_metrics(options, registry=None)` builds one `PlatformMetrics` (process-global); before it, `PrometheusMiddleware` is a pass-through and the `new_*` constructors raise `MetricsNotInitializedError`. The identity (`domain`, `service`, `environment`) is validated against the registry at startup — `ValueError` before serving.
- **Tracing:** one delegating global `TracerProvider` installed once; a later configured install swaps the inner SDK provider (tracers and decorators captured earlier follow), a provisional `unknown_service` install is replaced, a provider the service installed itself is kept. App shutdown calls `flush_tracing()` (≤ 5 s, off the event loop), never `shutdown_tracing()`.
- **Logging:** `configure_logging` configures structlog through the stdlib root logger; loggers are cached, while level and stack threshold are read per entry.
- **Request context:** a `ContextVar`, bound for the whole HTTP request (streaming body included) and each unary RPC; reset in `finally` even when metrics or logging fail.
