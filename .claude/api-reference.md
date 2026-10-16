# API Reference

The public surface is each subpackage's `__all__` (SemVer over it, [`VERSIONING.md`](../VERSIONING.md); `make api-compat` diffs it against the last tag). Underscore modules (`_errors`, `_scope`) and submodule paths are not public — `fastapicommon.auth.validation.resolve_identity`, for instance, is reachable but **not** re-exported. Signatures are authoritative in the docstrings and type hints (`py.typed`, `mypy --strict`; `make docs-serve`). This file records semantics and gotchas; LLD §5 is the long form.

## 1. API Conventions

- **One composition call.** Services call `create_platform_app(settings=PlatformSettings(), lifespan=...)` and keep their own routers. Every FastAPI kwarg passes through; `lifespan`, `on_startup` / `on_shutdown` and `@app.on_event` all still run.
- **Fail fast on identity.** A missing or invalid observability identity raises `ValueError` at construction (`create_platform_app`, `init_metrics*`, `Context(Async)ServerInterceptor(settings)`), never at the first request.
- **Auth is per route.** Nothing is enforced globally: add `Depends(require_authenticated_user)` or a role dependency.
- **Raise, don't build responses.** Raise a `PlatformException` subclass (or one with `problem_type=`); the handlers render the problem. Subclasses must not set `status_code` / `code` / `error_code` class attributes (`TypeError`).
- **Metrics after init.** The `new_*` constructors raise `MetricsNotInitializedError` before `create_platform_app` / `init_metrics`.

## 2. Usage Rules (what reviewers enforce in consumers)

| Rule | Why |
|---|---|
| Set `OBSERVABILITY_DOMAIN` and `APP_ENV` (`local\|dev\|test\|staging\|prod`) | Required while metrics are on; `production` / `development` are rejected |
| Set `TRUSTED_PROXIES` behind a proxy / load balancer | Otherwise the recorded client IP is the proxy's |
| The gateway **replaces** identity headers | Appended ones are ambiguous → unauthenticated |
| `create_platform_http_client()` only for internal services | It forwards identity to any host (OQ-6) |
| No message-bearing text in `ForbiddenException` / problem `detail` | `detail` is client-visible; 5xx detail is dropped anyway |
| Branch on a problem's `status` / `type` / `errorCode`, never on text | Titles and details are not contract |
| Tier 3 metrics are `<domain>_<service>_*`, counters `_total`, histograms `_seconds` | Validated at construction |
| gRPC handlers check identity themselves; streaming handlers bind context with `request_context_scope` | No gRPC auth interceptor; streaming RPCs get sanitizing only |

## 3. Public API by Subpackage

| Subpackage | `__all__` (abridged where noted) |
|---|---|
| `bootstrap` | `create_platform_app` |
| `config` | `PlatformSettings` |
| `context` | `CurrentUser`, `RequestContext`, `get_request_context`, `get_request_context_or_none`, `get_request_id`, `get_correlation_id`, `get_tenant_id`, `get_user_id`, `get_roles`, `get_trace_id`, `get_client_ip`, `bind_request_context`, `reset_request_context`, `request_context_scope` |
| `auth` | header constants (`USER_ID_HEADER`, `TENANT_ID_HEADER`, `TENANT_ROLES_HEADER`, `TENANT_ROLES_SEPARATOR`, `REQUEST_ID_HEADER`, `CORRELATION_ID_HEADER`, `TRACEPARENT_HEADER`), `MAX_HEADER_VALUE_LEN`, `MAX_ROLE_LEN`, `MAX_REQUEST_ID_LEN`, `CurrentUser`, `get_current_user`, `require_authenticated_user`, `normalize_header_value`, `normalize_request_id`, `parse_roles` |
| `rbac` | `RequireRole`, `RequireAnyRole`, `RequireAllRoles` |
| `exceptions` | `PlatformException` (= `BusinessException`), `ValidationException`, `UnauthorizedException`, `ForbiddenException`, `NotFoundException` (alias of `ResourceNotFoundException`), `ConflictException`, `BusinessRuleViolationException`, `RateLimitException`, `UnsupportedOperationException`, `ServiceUnavailableException`, `UnhandledErrorMiddleware`, `register_exception_handlers`, `problem_response`, `ProblemSettings` |
| `problem` | `Problem`, `FieldError`, `ProblemType`, `problem_type(domain, name, title, status, code)`, the 15 `TYPE_*`, `PLATFORM_TYPES`, `type_for_status`, `type_uri`, `build_problem`, `resolve_trace_id`, `trace_id_from_traceparent`, `sanitized_request_id`, `new_trace_id`, `problem_from_response`, `parse_problem`, `is_problem_content_type`, `utc_timestamp`, constants (`PROBLEM_MEDIA_TYPE`, `DEFAULT_PROBLEM_TYPE_BASE`, `CODE_REQUEST_TIMEOUT`, `CODE_ROUTE_NOT_FOUND`, …) |
| `grpc` (extra) | `ContextServerInterceptor(settings=None)`, `AsyncContextServerInterceptor(settings=None)`, `ContextClientInterceptor()` |
| `telemetry` | `TracingConfig`, `ensure_tracing`, `init_tracing`, `init_tracing_from_env`, `setup_tracing`, `flush_tracing`, `shutdown_tracing`, `active_tracing_config`, `new_tracer`, `noop_tracer`, `span_trace_id`, `record_span_error`, `inject_trace_context`, `extract_trace_context`, `instrument_grpc_client`, `instrument_grpc_server`, `UNKNOWN_SERVICE_NAME` |
| `logging` | `configure_logging(level=None, environment=None)`, `get_logger(name)` |
| `propagation` | `create_platform_http_client(**httpx_kwargs)` |
| `metrics` | `init_metrics`, `init_metrics_from_settings`, `resolve_metrics_options`, `MetricsOptions`, `MetricsIdentity`, `metrics_identity`, `identity_labels`, `metrics_registry`, `platform_metrics`, `platform_metrics_enabled`, `PlatformMetrics`, `PrometheusMiddleware`, `metrics_router`, `new_counter` / `new_gauge` / `new_histogram`, `new_shared_counter` / `new_shared_gauge` / `new_shared_histogram`, `register_labeled`, `ObservabilityRegistry`, `default_registry`, `http_method_label`, `MetricsNotInitializedError`; module `fastapicommon.metrics.compliance` (`validate`, `validate_text`, `Report`) |
| `health` | `health_router`, `create_health_router(readiness_checks=...)`, `ReadinessCheck` |
| `middleware` | `ContextMiddleware(app, trusted_proxies=None)`, `TracingMiddleware`, `LoggingMiddleware`, `TimeoutMiddleware(app, timeout_seconds)` |

`instrument_grpc_server()` nests a second span under the interceptor's; use `init_tracing_from_env()` with the interceptor instead.

## 4. `PlatformSettings` and Environment

| Field / env | Default | Notes |
|---|---|---|
| `app_name` / `APP_NAME` | `platform-service` | `service` label, OTel `service.name` |
| `environment` / `APP_ENV` (then `ENVIRONMENT`) | `""` | trimmed, lowercased; vocabulary `local, dev, test, staging, prod`; `dev` / `local` = console logs, debug, plaintext OTLP, 100% sampling |
| `observability_domain` / `OBSERVABILITY_DOMAIN` | `""` | required while `enable_metrics`; a registry domain |
| `build_version` / `BUILD_VERSION` | `""` → `unknown` | `platform_build_info{version}`, `service.version` |
| `log_level` / `LOG_LEVEL` | `""` → debug in dev/local, else info | `debug\|info\|warn\|error\|critical` |
| `request_timeout_seconds` / `REQUEST_TIMEOUT_SECONDS` | `30.0` | |
| `trusted_proxies` / `TRUSTED_PROXIES` | `""` | comma-separated IPs / CIDRs; invalid → `ValueError` at startup |
| `enable_tracing` / `enable_metrics` | `true` | |
| `otlp_endpoint` / `OTLP_ENDPOINT` | — | overrides `OTEL_EXPORTER_OTLP_ENDPOINT`; `none` / `off` / `disabled` turn export off |
| `problem_type_base` / `PROBLEM_TYPE_BASE` | `""` → `https://errors.company.com` | |

Read outside `PlatformSettings`: `OTEL_EXPORTER_OTLP_ENDPOINT` (default `localhost:4317`), `OTEL_TRACES_EXPORTER=none`, `OTEL_EXPORTER_OTLP_INSECURE`, `OTEL_TRACES_SAMPLER_RATIO` / `_ARG` (1.0 dev/local, else 0.1), `OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED`, `OTEL_PROPAGATOR_BAGGAGE`, `OTEL_SERVICE_NAME` (`init_tracing_from_env` only), `LOG_SAMPLING`, `LOG_FILE`. Booleans: `true/1/yes`, `false/0/no`, case-insensitive; anything else warns once and uses the default. Full table: README § Environment variables.

## 5. `fastapicommon-metricslint`

`check [--url URL | FILE...] [--strict]` (validate a scrape) · `refs PATH...` (legacy / unregistered names in dashboards and rules; `_bucket` / `_sum` / `_count` resolve only for histograms) · `registry` (self-check) · `inventory` (render `metric-registry.md`). Exit codes `0` clean, `1` findings, `2` usage or I/O error. `--strict` fails on `proposed` entries until ratification (OQ-1).
