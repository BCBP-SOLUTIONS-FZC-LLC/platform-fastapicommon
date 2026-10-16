# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/) — see [VERSIONING.md](./VERSIONING.md) for the
pre-1.0 caveat on what SemVer guarantees currently apply.

## [Unreleased]

### Changed (breaking) — raised dependency floors

The new `lowest-deps` CI job installs every runtime dependency at its declared floor. The old
floors did not work:

- `opentelemetry-instrumentation-fastapi` / `-grpc` below 0.49b0 import `pkg_resources`, so
  `fastapicommon` failed to import on an environment without setuptools.
- With `starlette` below 0.46, an exception raised after a streaming response has started
  reaches the error handling wrapped in an `ExceptionGroup`, which hides its real type from
  the error logs.

New floors: `fastapi>=0.115.10`, `starlette>=0.46`, `opentelemetry-api` / `-sdk` /
`-exporter-otlp-proto-grpc>=1.28`, and `opentelemetry-instrumentation-fastapi` /
`-grpc>=0.49b0`. The locked (tested) versions are unchanged.

### Changed (breaking) — logs and traces aligned with `platform-gincommon`

**Logs** now use gincommon's Zap output contract:

- **Format:**
  - JSON on **stderr** in Zap's production layout, with the keys `level`
    (`debug`/`info`/`warn`/`error`), `ts` (Unix seconds), `caller`, `msg`, the fields, and
    `stacktrace` on error entries.
  - This replaces structlog's `event` / `level` / `timestamp` on stdout. Loki queries on
    `event` must use `msg`.
- **Dev:** `dev` / `local` use Zap's console layout. ADR-001 is amended accordingly.
- **Settings:**
  - `LOG_LEVEL` defaults to debug in dev/local and info otherwise (`PlatformSettings.log_level`
    defaults to `""`).
  - New: `LOG_SAMPLING`.
  - `LOG_FILE` is written by the library. `make run` no longer pipes output through `tee`.
- **`http_request`:** a new access log (`LoggingMiddleware`, installed by
  `create_platform_app`) records `trace_id`, `request_id`, `method`, the route template as
  `path`, `status`, `duration_ms`, and either the verified identity or `claimed_*` values.
- **`grpc_request`** replaces `grpc_request_completed`. Its fields are `method` (bounded),
  `grpc_code` (Go names), `duration_ms` (was `duration_seconds`), `request_id` and the
  identity. The level follows the code.
- **New audit and diagnostic entries:** `auth: request rejected` (`require_authenticated_user`),
  `authz: access denied` (role dependencies), `rate limited` (429, at most 1 per route per
  second), `metrics handler error`, and the `otel_*` entries.
- **Correlation:** every entry written in a request now also carries `trace_id`.
- **Sanitising:** the access, auth and gRPC entries apply gincommon's sanitiser
  (`fastapicommon.logging.sanitize`).

**Traces:**

- **Spans:** the library starts its own server spans, `TracingMiddleware` for HTTP and
  `ContextServerInterceptor` for gRPC.
  - HTTP spans are named `METHOD route` (`GET /items/:item_id`), with gincommon's attributes
    and Error-status rules.
  - gRPC spans are named after the bounded `grpc_method`, with `rpc.*` and both status-code
    attributes.
  - Exceptions are recorded by type only.
  - `FastAPIInstrumentor` is gone, along with the `opentelemetry-instrumentation-fastapi`
    dependency.
- **Rollout:** a service with no collector configured now tries to export to `localhost:4317`
  (TLS outside dev/local) and logs exporter retry warnings. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to
  the collector, or `OTEL_TRACES_EXPORTER=none`, before upgrading. `APP_ENV=development` is no
  longer a dev environment (only `dev` / `local`): it gets JSON logs at info, TLS export and 10%
  sampling.
- **Configuration:**
  - gincommon's `OTEL_*` variables: `OTEL_EXPORTER_OTLP_ENDPOINT` (default
    **`localhost:4317`**, where previously nothing was exported unless `OTLP_ENDPOINT` was
    set; `none` disables export), `OTEL_EXPORTER_OTLP_INSECURE`,
    `OTEL_TRACES_SAMPLER_RATIO` / `_ARG`, `OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED` and
    `OTEL_PROPAGATOR_BAGGAGE`.
  - `OTLP_ENDPOINT` still overrides the endpoint.
- **Sampling:** parent-based, at 1.0 in dev/local and **0.1** otherwise (it was always 1.0).
- **Resource:** adds `service.version`, `service.namespace` (the domain) and
  `deployment.environment.name`.
- **Provider:** set once (delegating). The first configured install wins, a later one swaps
  it safely, a provider the service installed itself is kept, and the app's lifespan flushes
  it on shutdown.
- **New API:**
  - `TracingConfig`, `init_tracing`, `init_tracing_from_env`, `ensure_tracing` and
    `shutdown_tracing`;
  - `new_tracer`, `noop_tracer`, `span_trace_id`, `record_span_error`, `inject_trace_context`
    and `extract_trace_context`.
- **gRPC example:** `instrument_grpc_server()` would now nest a second span, so the example
  uses `init_tracing_from_env()` instead.

**Metrics:** `grpc_method` is bounded as in gincommon: a malformed or over-256-byte method
is `unknown`.

**Fixed:** with FastAPI ≥ 0.12x (0.140 is locked), every route added through
`include_router()` (including `/health`, `/ready` and `/metrics`) was labelled
`route="unknown"`, because FastAPI now keeps included routers as lazy entries. Routes are
now resolved through FastAPI's public `iter_route_contexts`, so they get their full template
(`/widgets/:widget_id`). This applies to the metrics label, the `http_request` log `path`
and the span name.

### Added — the observability standard's metrics schema strategy (parity with `platform-gincommon`)

- **Registry re-synced** with platform-gincommon v2.0.0 (adds the `transport` label and the
  `platform_unclassified_errors_total` entry; fastapicommon does not emit it yet).
- **Metric constructors for a service's own metrics** (Go `NewShared*` / `NewCounterVec` ...):
  - `new_shared_counter` / `new_shared_gauge` / `new_shared_histogram` build Tier 1/2 metrics
    from their registry entry: the name, help, labels and buckets all come from the registry.
  - `new_counter` / `new_gauge` / `new_histogram` build Tier 3 `<domain>_<service>_*` metrics.
    They reject registry and `platform_*` names, wrong suffixes, prohibited labels and the
    const labels.
  - `register_labeled` adds the identity labels to a third-party collector.
  - The constructors raise `MetricsNotInitializedError` before metrics are initialized.
- **`fastapicommon.metrics.compliance`**: `validate()` / `validate_text()` apply the full rule
  set of Go's `obsregistry.Validate`, using the same rule names. They accept Prometheus text
  and OpenMetrics, and support strict mode with `allow_proposed_owners`.
- **`fastapicommon-metricslint`** console script, a port of Go's `metricslint`. It has the
  same `check` / `refs` / `registry` / `inventory` commands and exit codes.
- **`parse_registry()`** applies Go `Parse`'s self-consistency rules. `default_registry()` now
  parses strictly.
- **`docs/observability/`:**
  - `README.md`, `migration.md` and `runbooks.md`;
  - `metric-registry.md`, generated by `make metrics-inventory` and identical to gincommon's.
- **Reference Prometheus rules** vendored from gincommon (`docker/prometheus/rules/`:
  recording rules, SLO burn-rate alerts, promtool tests). The local Prometheus loads them.
- **CI:** `make metrics-lint` runs the registry self-check, rule refs, promtool and an
  inventory drift check. `make gincommon-parity` compares the registry, rules and inventory
  with platform-gincommon at `GINCOMMON_REF`; this is required on `main` and for a release.
  The integration test validates the scrape with `validate()` (0 errors).

### Changed — CI/CD pipeline (parity with `platform-gincommon`)

- `.github/` now follows platform-gincommon's pipeline, translated to Python as
  platform-pgcommon-py and platform-eventcommon did: reusable `validate-test.yml` /
  `validate-quality.yml` (replacing `validate.yml`, `quality.yml` and `test.yml`; the
  required check names are unchanged), a `changes` job that skips build/test jobs on
  documentation-only changes, SHA-pinned actions, and `changelog-check.yml` / `docs.yml`.
- Tests run on Python 3.11–3.14 and with every runtime dependency at its floor (`lowest-deps`).
- `api-compat` (griffe) reports public-API breaks against the last tag; `release.yml` fails a
  release that may not break.
- The example image is pushed (on `main` and on release) only when its content fingerprint
  matches the image Trivy and the smoke tests judged. The image is Cosign keyless-signed.
- `release.yml` checks the tag first: it must be at HEAD and on `main`, the package version
  must match, and the CHANGELOG section must be dated. It builds the wheel and sdist once,
  reproducibly, from the hash-locked `build-constraints.txt`. The GitHub Release carries
  the wheel, the sdist, a Cosign-signed `SHA256SUMS`, the image's SLSA provenance and its
  SBOM.
- The example image's base images are digest-pinned (`make pin-base-images`). It installs
  the locked dependency set with hashes and runs as numeric UID 10001. Its HEALTHCHECK now
  uses Python instead of `curl`, which is no longer installed.
- Dependabot uses the `uv` ecosystem.

### Fixed — production-readiness review

- **`@app.on_event` / `on_startup` / `on_shutdown` handlers run again with tracing on.**
  `create_platform_app` passed its own `lifespan` to FastAPI, which then skipped them. It now wraps
  the router's lifespan after FastAPI built it, so every form FastAPI accepts runs: those
  handlers, an `@asynccontextmanager` lifespan, and a plain (async) generator lifespan, which used
  to fail at startup with `'async_generator' object does not support the asynchronous context
  manager protocol`.
- **An app's shutdown no longer stops tracing for the rest of the process.** It calls the new
  `flush_tracing()` (export buffered spans, keep the provider) instead of `shutdown_tracing()`, so
  other apps and gRPC servers sharing the provider keep recording. A provider the service
  installed itself is not touched. The SDK shuts the provider down at interpreter exit.
- **`LOG_SAMPLING=true` can no longer crash a request.** The sampler raised `structlog.DropEvent`
  (a `BaseException`) out of the formatter into the caller of a stdlib logger, from the 101st
  identical entry in a second. It is now a handler filter that drops the entry, and decides once
  per record, so stderr and `LOG_FILE` count an entry once.
- **`OTLP_ENDPOINT=none` (also `off` / `disabled`) disables export** instead of building an
  exporter for the host `none`, and `OTEL_TRACES_EXPORTER=none` also applies when
  `OTLP_ENDPOINT` is set.
- **An explicit environment decides the dev trace defaults.** `TracingConfig.from_env(environment=
  "local")` with `APP_ENV=prod` got the `local` label but production insecure / sampling defaults.
- **`new_tracer(...).start_as_current_span(...)` as a decorator follows the provider per call.** It
  bound the tracer when applied, so a function decorated at import never produced spans, or kept
  writing to a provider that had been shut down.
- **`PlatformException` subclasses with the old class attributes are rejected.** `status_code`,
  `code` or `error_code` set on a subclass no longer select the response, so such a subclass
  silently answered 500 `internal-error`. Defining one now raises `TypeError` pointing at
  `problem_type`.
- **The route label is matched once per request** and kept in the scope, instead of once each in
  the tracing, logging, metrics, timeout and error paths.
- **`AsyncContextServerInterceptor`: a handler's own `context.abort` is passed through.** It was
  aborted a second time, so grpc.aio logged `UsageError('Abort already called!')` at ERROR on
  every deliberate 4xx. Sync handlers on a `grpc.aio` server keep running in its migration
  thread pool, wrapped like `ContextServerInterceptor`'s (they were wrapped as coroutines, so a
  sync stream sent no messages and a sync `abort` did nothing). `context.write` streams work.
- **Route labels under a `Mount` are right without metrics.** With `enable_metrics=False` the
  label was resolved after routing rewrote the scope (`/admin/users/1` logged as `/users/:uid`);
  `TracingMiddleware` and `LoggingMiddleware` now resolve it before calling the app.
- **A failure after the response started is logged as an aborted request** (gincommon's
  `ErrAbortHandler` path): `http_request` at debug with `"aborted": true` and the status sent, the
  span annotated `http.aborted` (no Error status), and the `unclassified error` entry carries the
  real `traceId` instead of `""`.
- **`OTEL_TRACES_SAMPLER_RATIO=nan` falls back to the default** instead of crashing startup.
- **A metric named `..._created` is no longer dropped from scrapes**: only prometheus_client's
  `<family>_created` timestamp samples of counters, histograms and summaries are removed.
- **The `LOG_SAMPLING` counter table is bounded** (expired windows swept, at most 4096 keys).
- The HTTP and gRPC server-span code obtains its tracer once instead of per request.
- Internal: one Gin `ClientIP()` (`fastapicommon.middleware.context.client_ip`), one UTF-8
  truncation (`fastapicommon.logging.sanitize.truncate_utf8`), one panic-span helper, and no
  serialise-then-parse of the 500 problem body. The caller field no longer uses structlog's private
  `_frames` API.

### Changed (breaking) — `PlatformSettings.environment` defaults to `""`

It defaulted to `"development"`, which is not in the registry's vocabulary (`local`, `dev`,
`test`, `staging`, `prod`) and was ignored everywhere. Unset now reads as unset: production log
and trace defaults, and `create_platform_app` still rejects it while metrics are enabled. Code
reading `settings.environment == "development"` must set `APP_ENV`.

### Added — `AsyncContextServerInterceptor` for `grpc.aio` servers

The asyncio counterpart of `ContextServerInterceptor`: server span, request context,
`platform_grpc_*` metrics, `grpc_request` log, and exception sanitizing (unary and streaming).

### Security — unclassified errors (parity with `platform-gincommon` OQ-24 / OQ-25)

- **gRPC exception messages no longer reach clients or logs.** grpcio answered a handler exception with `UNKNOWN "Exception calling application: <message>"` and logged the traceback with the message. `ContextServerInterceptor` now sanitizes every RPC (unary and streaming). A status the handler chose (`context.abort`, or `set_code` then raise) is kept, re-aborted with its own details. Any other exception is aborted as `INTERNAL "internal server error"` and logged at ERROR as `unclassified error` (`grpc_method`, `grpc_code=Internal`, ids, `error_type` / `error_chain`, `stack`; never the message). This is the same contract as platform-gincommon's gRPC error sanitizer. Callers that matched on `UNKNOWN` or on the details text must use status codes. Unclassified exceptions still count in `platform_grpc_panics_total`, now with code `Internal`.
- **Unclassified exceptions are logged as a defect, without their message** (parity with platform-gincommon's OQ-24 resolution). `unhandled_exception_handler` used to call `logger.exception(...)`, which logged the exception text and the full traceback. It now logs one error-level `unclassified error` entry: `classified=false`, ids, `service`, `method`, `route`, `operation`, `status`, `errorCode`, `error_type` / `error_chain` (exception types) and `stack` (`file:line in function` frames, no source lines). The client response is unchanged: a 500 `internal-error` without detail.
- **New `UnhandledErrorMiddleware`**, installed by `create_platform_app` just inside `LoggingMiddleware` (so `http_request` logs the 500). It also records the exception type on the server span (`exception` event, status `panic`). Starlette's `ServerErrorMiddleware` re-raises after the `Exception` handler, so the server (uvicorn) logged the traceback *with the message*. The middleware answers the 500 problem itself and does not re-raise. Apps that call `register_exception_handlers` without `create_platform_app` should add it.
- **`platform_unclassified_errors_total{transport}`** (platform-gincommon registry, `approved`): incremented for every unclassified exception, HTTP (`transport="http"`) and gRPC (`transport="grpc"`). Both series start at 0. This is the alerting signal; the `unclassified error` log event stays for diagnostics. The vendored `registry.json` is re-synced (new label `transport`).

### Security — dependencies

- Locked dependency upgrades for the `pip-audit` gate: `cryptography` 50.0.2 (PYSEC-2026-3552), `urllib3` 2.8.0 (PYSEC-2026-4175/4176/4177), `virtualenv` 21.14.5 (PYSEC-2026-4011–4014) and `pip` 26.2.1 (PYSEC-2026-3721).
- The example service image removes `pip` from its runtime stage. pip's vendored `urllib3`, `msgpack` and `setuptools` were what Trivy flagged, even with the base image's current pip, and nothing runs pip at runtime.

### Changed (breaking) — RFC 9457 Problem Details (parity with `platform-gincommon` v2.0.0)

- Every HTTP error is an RFC 9457 problem (`application/problem+json`) instead of
  `{error, status, trace_id, request_id}`: `type`, `title`, `status`, `detail`, `instance`,
  `traceId`, `errorCode`, `tenantId`, `timestamp`, `validationErrors`. The types, titles, error
  codes, trace-id resolution and validation messages are gincommon v2's; see
  [docs/problem-details.md](docs/problem-details.md).
- `ValidationException` and `RequestValidationError` are **400** `platform/validation-failed`
  (was 422), with `validationErrors` carrying JSON field paths and gincommon's fixed messages.
- `TimeoutMiddleware` answers **503** `platform/service-unavailable` with `errorCode`
  `REQUEST_TIMEOUT` (was 504), the response gincommon's `TimeoutMiddleware` sends.
- `PlatformException.code` is now the upper-snake `errorCode` (`NOT_FOUND`); `status_code` derives
  from the new `problem_type`. Default messages of `UnauthorizedException` and
  `ForbiddenException` are gincommon's details. A 5xx never carries a detail.

### Added

- `fastapicommon.problem`: `ProblemType`, the 15 platform types, `problem_type(...)` for domain
  types, `build_problem`, `resolve_trace_id`, `problem_from_response` / `parse_problem` for
  clients.
- Exceptions `BusinessException` (alias of `PlatformException`, which now takes
  `problem_type=`, `code=` and `validation_errors=`), `ResourceNotFoundException`,
  `BusinessRuleViolationException` (422), `RateLimitException` (429),
  `UnsupportedOperationException` (501), `ServiceUnavailableException` (503); `NotImplementedError`
  maps to 501. Unmatched routes are `ROUTE_NOT_FOUND`; 405 is `method-not-allowed`.
- `PlatformSettings.problem_type_base` (`PROBLEM_TYPE_BASE`) and
  `register_exception_handlers(app, type_base=, service=)`; `problem_response(request, type, ...)`.
- One WARN `problem response` log line per problem (`traceId`, `type`, `status`, `errorCode`,
  `service`, `tenantId`).

### Fixed

- Re-vendored `src/fastapicommon/metrics/registry.json` from gincommon (it had drifted behind
  gincommon v1.5.0's four Tier 2 `iam_*` metrics).

### Added — Enterprise Platform Observability Standard (parity with `platform-gincommon`)

- `platform_*` metrics — now the only HTTP/gRPC metrics the library emits — for the registry
  domain (`PlatformSettings.observability_domain` / `OBSERVABILITY_DOMAIN`), with the const labels
  `domain`, `service` (`app_name`) and `environment` on every series:
  `platform_build_info{version}`, `platform_http_requests_total`,
  `platform_http_request_duration_seconds`, `platform_http_request_size_bytes`,
  `platform_http_response_size_bytes` (all `{method, route, status_class, error_class}`),
  `platform_http_active_requests{route}`, `platform_http_panics_total`,
  `platform_http_rate_limited_requests_total`, `platform_http_request_timeouts_total` (all
  `{method, route}`), `platform_grpc_server_handled_total{grpc_method, grpc_code, status_class,
  error_class}`, `platform_grpc_server_handling_seconds`,
  `platform_grpc_server_request_size_bytes`, `platform_grpc_server_response_size_bytes` (all
  `{grpc_method, status_class, error_class}`), `platform_grpc_server_inflight_requests`,
  `platform_grpc_rate_limited_requests_total` and `platform_grpc_panics_total` (all
  `{grpc_method}`). Names, help, label order and buckets come from the Platform Observability
  Registry, vendored byte-for-byte from gincommon at `src/fastapicommon/metrics/registry.json`.
- The platform `method` label is bounded (standard methods, else `OTHER`); `route` is the
  Gin-style route template (`/items/:item_id`) or `unknown`, never the raw path.
- New `PlatformSettings` fields `observability_domain` (required while `enable_metrics` is on)
  and `build_version` (`BUILD_VERSION`). The environment label is the explicitly provided `environment`
  (`APP_ENV`, then `ENVIRONMENT`), trimmed and lowercased, and must be one of
  `local, dev, test, staging, prod` (no aliases).
- `create_platform_app()` raises `ValueError` at startup on a missing domain (the message names
  `OBSERVABILITY_DOMAIN`), an unknown domain, an invalid service label
  (`^[a-z][a-z0-9_-]{0,62}$`), or a missing/out-of-vocabulary environment. With
  `enable_metrics=False` no domain is needed; a configured one is still validated.
- `ContextServerInterceptor(settings=None)`: pass `PlatformSettings` to initialize metrics in a
  gRPC-only service (same validation; nothing is initialized with `enable_metrics=False`).
- `fastapicommon.metrics`: `MetricsOptions`, `MetricsIdentity`, `init_metrics`,
  `init_metrics_from_settings`, `resolve_metrics_options`, `platform_metrics`,
  `platform_metrics_enabled`, `metrics_identity`, `metrics_registry`, `http_method_label`,
  `PlatformMetrics`, `ObservabilityRegistry`, `default_registry`.
- Compliance test `tests/integration/test_metrics_compliance.py` checks every emitted
  `platform_*` sample against the vendored registry's labels and vocabularies.

### Changed

- `metrics_router` serves `metrics_registry()` (prometheus_client's default `REGISTRY` unless
  `init_metrics(..., registry=...)` was given one) instead of always the default registry.
- **Breaking:** `OBSERVABILITY_DOMAIN` (or `PlatformSettings.observability_domain`) is now
  mandatory whenever metrics are enabled (the default). `create_platform_app()`,
  `init_metrics()`, `init_metrics_from_settings()` and `ContextServerInterceptor(settings)` raise
  `ValueError` without one; set `enable_metrics=False` to run without metrics.
- `init_metrics_from_settings()` honours `settings.enable_metrics` (initializes nothing when it
  is false). `PrometheusMiddleware` is a pass-through until metrics are initialized.

### Removed

- **Breaking:** the legacy metrics `http_requests_total`, `http_request_duration_seconds`,
  `grpc_requests_total` and `grpc_request_duration_seconds` are no longer defined or emitted
  (status `removed` in the registry). Use the `platform_*` equivalents.
- **Breaking:** the `fastapicommon.metrics` exports `HTTP_REQUESTS_TOTAL`,
  `HTTP_REQUEST_DURATION_SECONDS`, `GRPC_REQUESTS_TOTAL`, `GRPC_REQUEST_DURATION_SECONDS` and
  `record_grpc_request`, and the `fastapicommon.metrics.definitions` module.
- The `disable_legacy_metrics` setting (`DISABLE_LEGACY_METRICS`) and
  `MetricsOptions.disable_legacy_metrics` / `MetricsIdentity.disable_legacy_metrics` /
  `MetricsIdentity.platform_enabled`.
- There is **no compatibility period**: the Enterprise Platform Observability Standard's
  parallel-emission window is unnecessary because neither `platform-fastapicommon` nor
  `platform-gincommon` was ever deployed to production or dev, so no dashboard, alert or
  recording rule depends on the legacy names.

## [0.2.0] - 2026-07-27

### Added

- New CI job **Cross-language compatibility** (`ci.yml`) calls the shared
  `platform-interop-tests` reusable workflow against this branch's ref, checking this
  library's wire behavior against the Go `platform-gincommon` library and the other
  platform sibling repos on every PR.

### Fixed — cross-language compatibility with the Go `platform-gincommon` library

**Breaking:** these changes bring the interop-critical HTTP surface into byte-for-byte parity
with `platform-gincommon`, since a Go service and a Python service must produce/consume
identical headers and error bodies over the same call. Full-parity items still open (a
pluggable `Authorizer` port, blanket route-group auth enforcement) are tracked separately.

- `exceptions.errors.PlatformException` no longer serializes to `{"code": ..., "message": ...}`.
  Every registered exception handler (`exceptions/handlers.py`) now returns
  `{"error": str, "status": int, "trace_id": str, "request_id": str}` — matching Go
  `gincommon`'s `ErrorResponse` exactly. `PlatformException.code` remains as a Python-side
  categorization attribute; it is no longer part of the wire response. `TimeoutMiddleware`'s
  504 response uses the same shape.
- `context.RequestContext` gained `trace_id: str` (`""` when no OTel span is active) and
  `client_ip: str | None` fields, for parity with Go's `RequestContext{TenantID, UserID, Roles,
  TraceID, ClientIP}`. New `get_trace_id()`/`get_client_ip()` accessors in `context.vars`.
- `ContextMiddleware` now validates `x-user-id`/`x-tenant-id` (non-empty, <=256 bytes, no
  control characters) and `x-request-id` (<=128 bytes, `^[a-zA-Z0-9._-]+$`) using the same rules
  as Go's `internal/core/domain/validation.go`; an invalid value is treated as absent rather
  than accepted. `x-tenant-roles` parsing now validates each token against `^[a-zA-Z0-9_-]+$`,
  enforces a 64-byte cap, and deduplicates — matching Go's `ParseRoles` exactly (new
  `auth.validation` module: `normalize_header_value`, `normalize_request_id`, `parse_roles`).
- `ContextMiddleware` now echoes `X-Request-ID` and (when a valid trace is active) `X-Trace-ID`
  response headers, matching Go's exact casing, in addition to the existing lowercase
  `x-request-id`/`x-correlation-id` pair.
- `propagation.create_platform_http_client()` now propagates `x-tenant-roles` (comma-joined,
  only when non-empty) on outbound requests — Go's `PropagateHeaders` always included it;
  Python's outbound propagation previously omitted it.

## [0.1.1] - 2026-07-26

### Added

- `tests/e2e/test_smoke.py` — a real `tests/e2e` tier (`pytest.mark.e2e`), matching
  `platform-pgcommon-py`'s `unit`/`integration`/`e2e` convention. `tests/integration`'s
  `TestClient` runs the ASGI app in-process via `ASGITransport` and neither tier ever
  imports the app the way `uvicorn examples.fastapi_service.main:app` actually does or
  verifies the packaged Docker image works at all — `tests/e2e` builds the real image,
  runs it as a real container, and makes real HTTP calls against it over a real socket
  (health/readiness, metrics, anonymous `/widgets/me`, and RBAC-gated `DELETE
  /widgets/{id}` both without and with the required role). Requires Docker; skipped
  automatically if the `docker` CLI isn't on `PATH`. An `E2E_IMAGE_TAG` env var lets a
  pipeline that already built the image (e.g. CI's "Smoke tests" job) reuse it instead of
  building another one. New `make test-e2e` target (`--no-cov`, see Fixed below);
  `make test-all`/`make ci` now run all three tiers together. `make smoke-test` is now an
  alias for `make test-e2e`, and CI's "Smoke tests" job now runs `tests/e2e` against the
  image it already built with cached buildx, instead of a separate raw
  `docker run`/`curl` script — the two were duplicating the same check.

### Changed

- `tests/` restructured into `tests/unit/` and `tests/integration/` tiers, matching
  `platform-pgcommon-py`'s `unit`/`integration`/`e2e` convention (the `e2e` tier followed
  separately — see Added, above). `tests/bootstrap/test_bootstrap.py` moved to
  `tests/integration/test_bootstrap.py` and is now tagged `pytest.mark.integration`
  (registered in `pyproject.toml`, `--strict-markers` enabled); everything else moved
  under `tests/unit/<module>/` unchanged. `Makefile` gained `test-unit`, `test-int`,
  `test-all` (replacing `test`), and `cover` (replacing `coverage`); `lint` now runs ruff
  only, with mypy split into a new `typecheck` target — also matching `pgcommon-py`. `make
  ci` is now `format-check + lint + typecheck + test-all + build`.
- Local/CI dependency management migrated from plain `pip` + `venv` to
  [`uv`](https://docs.astral.sh/uv/), matching `platform-pgcommon-py`'s convention.
  `uv.lock` is now committed for reproducible installs. `[project.optional-dependencies]`
  keeps `grpc` and `examples` as real, publicly installable extras; local/CI-only tooling
  (pytest, ruff, mypy, black, pre-commit, pip-audit, twine, plus `grpcio`/`uvicorn`
  duplicated for convenience) moved to a new `[dependency-groups].dev`
  (`[tool.uv] default-groups = ["dev"]`). `Makefile` targets now run `uv sync` / `uv run`
  / `uv build` instead of manually managing `.venv` with `pip`; `.pre-commit-config.yaml`'s
  local `mypy`/`pytest` hooks now invoke `uv run` instead of `.venv/bin/python -m`; all 5
  CI jobs that installed dependencies now use `astral-sh/setup-uv` + `uv sync` instead of
  `actions/setup-python` + `pip install -e`.

### Fixed

- While migrating, `uvicorn` was briefly moved out of the `examples` extra entirely into
  the new dev group — which would have broken `Dockerfile`'s
  `pip install ".[grpc,examples]"` (the `examples` extra wouldn't have existed). Caught
  before committing by actually rebuilding the Docker image; `examples` now stays a real
  project extra (for the Dockerfile's plain-pip install) *and* is duplicated into the dev
  group (for local `uv sync` convenience), matching the same pattern already used for
  `grpc`.
- Running `tests/e2e` alone (`make test-e2e`) failed pytest's default `--cov=fastapicommon`
  coverage gate at 0%: `tests/e2e` runs the app inside a separate Docker container
  process, so coverage.py — instrumenting the host pytest process — genuinely collects
  nothing. `make test-e2e` now passes `--no-cov`; `make test-all`/`make ci` are unaffected
  since unit/integration tests in that same combined run still produce valid coverage
  data (verified: coverage stays 99.81% whether or not `tests/e2e` is included alongside
  them). CI's per-Python-version "Coverage" matrix job and the required "Validate / Test /
  test" check now scope their pytest invocation to `tests/unit tests/integration`
  explicitly, so they don't build a Docker image on every run — `tests/e2e` runs
  separately in CI's "Smoke tests" job instead (see Added, above).
- CI failed with "Unable to resolve action `astral-sh/setup-uv@v9`, unable to find
  version `v9`": as of v8.0.0, `astral-sh/setup-uv` publishes only immutable exact-version
  release tags and no longer maintains floating major/minor tags. All 7 usages across
  `ci.yml`/`test.yml`/`quality.yml`/`release.yml` now pin `@v9.0.0`.
- `ruff check` failed in CI on a missing blank line between the third-party `import grpc`
  and first-party `fastapicommon.*` import groups in `src/fastapicommon/grpc/{client,server}.py`
  and `examples/grpc_service/server.py` — never caught locally because a stale
  `.ruff_cache` predating the ruff 0.4→0.16 bump (see uv migration, above) masked it, and
  CI never reached the lint step before now (it was failing earlier at the `setup-uv`
  action-resolution step above).
- Follow-up doc-vs-code audit: `docs/architecture/mermaid/module-dependencies.mmd` (and
  its embedded copy in `ARCHITECTURE.md`) drew a `health -> config` dependency edge that
  doesn't exist in code; `ARCHITECTURE.md`'s `fastapicommon.health` Public API table was
  missing `ReadinessCheck`, which `health/__init__.py`'s `__all__` already exports.

## [0.1.0] - 2026-07-25

First release of `fastapicommon`.

### Added

- Authentication context (`x-user-id`, `x-tenant-id`, `x-tenant-roles`, `x-request-id`,
  `x-correlation-id`) via `fastapicommon.auth` and `fastapicommon.context`.
- contextvars-based request context propagation, safe across async tasks and gRPC handlers.
- Structured JSON logging via `fastapicommon.logging`, enriched with request/tenant/user
  context.
- OpenTelemetry tracing setup for FastAPI and gRPC in `fastapicommon.telemetry`.
- Prometheus HTTP/gRPC metrics and `/metrics` router in `fastapicommon.metrics`.
- Context propagation and request timeout middleware in `fastapicommon.middleware`.
- Outbound header propagation via `fastapicommon.propagation.create_platform_http_client`.
- RBAC dependencies (`RequireRole`, `RequireAnyRole`, `RequireAllRoles`) in `fastapicommon.rbac`.
- `PlatformException` hierarchy and FastAPI exception handlers in `fastapicommon.exceptions`.
- Health probe router (`/health`, `/ready`, `/live`) in `fastapicommon.health`.
- `PlatformSettings` in `fastapicommon.config`.
- `create_platform_app()` bootstrap helper in `fastapicommon.bootstrap`.
- gRPC server/client interceptors for logging, tracing, and context propagation in
  `fastapicommon.grpc`.
- Local observability stack (`docker-compose.yml`, `docker/`) with Grafana, Prometheus,
  Tempo, and Loki; `Dockerfile` for the example FastAPI service; `Makefile` targets
  (`docker-up`, `docker-down`, `run`, `docker-build`) — see the README's "Local testing
  with the observability stack".
- `ARCHITECTURE.md` with Mermaid diagrams under `docs/architecture/mermaid/`, covering the
  module dependency graph, bootstrap flow, request/exception/RBAC flows, tracing and
  metrics initialization, the gRPC interceptor stack, and the observability stack.
- CI/CD: reusable `validate.yml` workflow, `ci.yml` (validate → coverage → build → Docker
  image push to GHCR), and tag-triggered `release.yml` (adds a GitHub Release from the
  matching `CHANGELOG.md` section). Plus `CODEOWNERS`, `dependabot.yml` (pip,
  github-actions, docker), `SECURITY.md`-linked issue templates, and a PR template.
- `VERSIONING.md` and root `SECURITY.md` documenting the SemVer policy, trust model, and
  vulnerability reporting process.
- `make format-check`, `make vulncheck` (pip-audit), and `make build` targets; `make ci`
  now runs `format-check + lint + test + build` (previously `lint + test + docker-build`,
  which made the local pipeline needlessly slow). `validate.yml` runs the same format
  check in CI.
- `release.yml`'s `pypi-publish` job — publishes the built wheel/sdist to public PyPI via
  Trusted Publishing (OIDC), no API token stored anywhere. Requires a one-time Trusted
  Publisher registration on PyPI's side — see
  [VERSIONING.md § Publishing to PyPI](./VERSIONING.md#publishing-to-pypi).
- CI jobs restructured to match the org's `protect-main-branch` required-status-checks
  ruleset by name: `validate.yml` now fans out into two nested reusable workflows
  (`quality.yml`, `test.yml`), producing `Validate / Quality / quality` and
  `Validate / Test / test`. New real (not just renamed) checks in `ci.yml`: **Lint
  Dockerfile** (hadolint), **Build image (cache)**, **Trivy CVE scan**
  (`--ignore-unfixed`, HIGH/CRITICAL), **Smoke tests** (build + run + curl
  `/health`/`/ready`/`/metrics`/`/widgets/me`), and **PR summary** (posts/updates a PR
  comment). New `make lint-docker`, `make scan`, `make smoke-test` targets run the
  Dockerfile/Trivy/smoke checks locally. Fixed a real `DL3008` hadolint finding
  (unpinned `apt-get install curl`) in `Dockerfile` along the way.

### Changed

- `create_platform_app` now wires `PlatformSettings.otlp_endpoint` through to an
  `OTLPSpanExporter` when set (the field existed but was previously unused — tracing was
  always recorded-but-not-exported regardless of this setting).
- Declared and CI-tested Python support extended to 3.13 and 3.14 (previously 3.11/3.12
  only in classifiers and the CI matrix, even though `requires-python = ">=3.11"` already
  had no upper bound). Verified: all key dependencies (`grpcio`, `pydantic-core`, `fastapi`,
  `opentelemetry-*`, `uvicorn`) ship genuine prebuilt `cp314` wheels — no source
  compilation required — and the full test suite passes under Python 3.14.6.
- **PyPI distribution renamed to `platform-fastapicommon`** (`[project].name` in
  `pyproject.toml`). The name `fastapicommon` was rejected by PyPI as too similar to an
  existing, unrelated package (`fastapi-common`). This only changes what you `pip install`
  — the importable package is still `fastapicommon`
  (`from fastapicommon.bootstrap import create_platform_app` is unaffected). Updated
  everywhere this was documented: README, ARCHITECTURE.md, VERSIONING.md, and the
  `pypi-publish` job's environment URL in `release.yml`.

### Fixed

- `PlatformSettings.environment` now accepts the `APP_ENV` environment variable (the name
  used throughout `Dockerfile`, `Makefile`, and `.env-example`, matching
  `platform-gincommon`'s convention) in addition to `ENVIRONMENT`. Previously `APP_ENV` was
  silently ignored — `settings.environment` always fell back to its `"development"`
  default regardless of what `APP_ENV` was set to, so `OTLP_EXPORTER` insecure-mode
  detection and any environment-conditional behavior never actually responded to it.
- The `Makefile`'s `export` of `APP_NAME`/`OTLP_ENDPOINT`/etc. was previously file-wide,
  leaking into `make test`'s pytest subprocess and silently overriding
  `PlatformSettings` defaults in tests that assert on them. These are now set only on the
  `run` target, which is the only place a real environment variable (as opposed to a Make
  variable) is actually needed.
- `tests/bootstrap/test_create_platform_app_wires_otlp_exporter_without_raising` made a
  real HTTP request through an app wired with a real `OTLPSpanExporter` targeting
  `localhost:4317`, which spawned a genuine background export thread that spent the rest
  of the test run retrying a connection to a collector that doesn't exist in CI/local
  dev, spamming stderr with `Transient error ... Failed to export traces` after every
  test run. The test now only asserts construction doesn't raise (its actual purpose);
  request-handling with instrumentation enabled is already covered elsewhere with
  `otlp_endpoint` unset.
- Full doc-vs-code audit (README, ARCHITECTURE.md, mermaid sources): `examples/fastapi_service/main.py`
  had a `POST /widgets/` route while the README's "Complete HTTP example" and "Calling the
  API" curl commands both documented a `DELETE /widgets/{widget_id}` route with
  `NotFoundException` handling — the README's own `curl -X DELETE` example would have 404'd
  against the real example service. The example app now matches exactly what's documented
  (verified with real requests, not just read). Also fixed: `ARCHITECTURE.md`'s "Minimal
  wiring example" still showed the old `POST` snippet; the `fastapicommon.config` public-API
  table entry didn't mention the `APP_ENV`/`ENVIRONMENT` alias; `documentation-assets.mmd`
  (and its embedded copy) said "10 x .mmd source diagrams" when there are 11; and the
  README's CI section implied `pypi-publish` and `docker` run strictly sequentially in
  `release.yml` when they actually run in parallel (both depend only on `build`).
- `docker/setup-buildx-action` occasionally fails to boot with `context deadline exceeded`
  pulling `moby/buildkit` from Docker Hub (a transient network/rate-limit blip on the
  shared GitHub-hosted runner IP). All 5 places it's used across `ci.yml`/`release.yml`
  now retry once with the same action before failing the job.

[Unreleased]: https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/releases/tag/v0.1.0
