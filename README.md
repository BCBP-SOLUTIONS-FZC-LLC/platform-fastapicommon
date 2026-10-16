# fastapicommon

Shared **FastAPI and gRPC** middleware library for platform services: authentication
headers, tenant context propagation, request tracing, structured logging, Prometheus
metrics (HTTP and gRPC), per-request timeouts, outbound header propagation, and optional
RBAC.

**Repository:** [github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon)

This project is consumed as a **Python package** by other services. Those services keep
their own routers, repositories, use cases, and business logic and only import from
`fastapicommon`. You do **not** need to deploy `examples/` in production — it exists to
exercise this library's logging, metrics, and tracing locally (see [Local development](#local-development-this-repo)).

## Mental model

This library provides three layers your service sits on top of:

| Layer | What it does | Your responsibility |
|-------|--------------|----------------------|
| **Observability** | Request/correlation ID, tracing, Prometheus metrics, structured logging | Nothing on the HTTP side — `create_platform_app()` wires it automatically. On gRPC, add `ContextServerInterceptor` yourself (see [gRPC — quick start](#grpc--quick-start)) |
| **Security** | Header-based identity extraction; RBAC role checks | Add `Depends(require_authenticated_user)` / `Depends(RequireRole(...))` per route — **nothing is enforced globally**, see [Important rules](#important-rules-for-handler-authors) |
| **Context** | Builds `RequestContext{request_id, correlation_id, user: CurrentUser, trace_id, client_ip}` and binds it via `contextvars` | Read it via `fastapicommon.context.get_request_context()` / `fastapicommon.auth.get_current_user`; propagate outbound via `create_platform_http_client()` |

Your service only implements **business logic** on top of these layers. Log format, span
setup, error response shape, and metric names are standardised here.

> **Tracing guarantee:** when `settings.enable_tracing` is true (the default), every
> incoming request gets one OTel server span named `METHOD route` (e.g. `GET /items/:item_id`),
> continuing the distributed trace when a `traceparent` header is provided — the same spans,
> attributes, sampling and `OTEL_*` configuration as platform-gincommon. Spans are exported over
> OTLP/gRPC to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `localhost:4317`; `none` keeps them
> in-process). See [Logs and traces](#logs-and-traces-parity-with-platform-gincommon) and
> [ARCHITECTURE.md § Tracing initialization](./ARCHITECTURE.md#tracing-initialization).

---

## Why this library exists

Every backend service in the platform needs the same cross-cutting infrastructure: request
correlation, distributed tracing, structured logging, Prometheus metrics, identity
extraction, and access control. Without a shared library each team re-implements these
differently, leading to:

- Inconsistent log formats that break cross-service queries
- Missing trace context that breaks distributed traces
- Auth bugs from hand-rolled header validation
- Metrics with different label names that can't be aggregated

This library **standardises** these concerns once so every service gets:

- The same log lines as Go services (platform-gincommon's Zap layout and event catalogue:
  `http_request`, `grpc_request`, `auth: request rejected`, ...), enriched with `request_id`,
  `correlation_id`, `trace_id`, `tenant_id`, `user_id`
- The same OTel spans as Go services (names, attributes, resource, sampling)
- A `RequestContext`/`CurrentUser` available in every handler via one dependency
- The same RFC 9457 Problem Details error contract (`application/problem+json`) across all
  services — the same types, titles, error codes, trace ids and validation messages as the Go
  `gincommon` v2 library (see [docs/problem-details.md](docs/problem-details.md))

---

## Features

| Capability | HTTP | gRPC |
|------------|------|------|
| **Request/correlation ID** | `ContextMiddleware` — reads `x-request-id`/`x-correlation-id` or generates a UUID; echoes both back | `ContextServerInterceptor` — reads from metadata or generates a UUID |
| **Tracing** | `TracingMiddleware` — one server span per request, `METHOD route`, gincommon's attributes | `ContextServerInterceptor` — one server span per RPC; `instrument_grpc_client()` for outbound calls |
| **Metrics** | `PrometheusMiddleware` — the `platform_http_*` metrics of the [observability standard](#platform-observability-standard-metrics); requires `OBSERVABILITY_DOMAIN` | `ContextServerInterceptor` — the `platform_grpc_*` metrics |
| **Logging** | `LoggingMiddleware` — the `http_request` access log; `get_logger()` in gincommon's layout, enriched with context | Same `get_logger()`; `ContextServerInterceptor` logs `grpc_request` after each call |
| **Auth** | `Depends(require_authenticated_user)` — `401` if `x-user-id` unset (opt-in per route) | **Not provided** — no gRPC equivalent exists; check `get_request_context().user_id` yourself |
| **Tenant context** | `RequestContext{request_id, correlation_id, user: CurrentUser, trace_id, client_ip}` via `get_request_context()` | Same struct, same accessor, bound by `ContextServerInterceptor` |
| **RBAC** | `RequireRole` / `RequireAnyRole` / `RequireAllRoles` as FastAPI dependencies | **Not provided** — these are FastAPI `Depends`-based; there is no gRPC interceptor equivalent |
| **Timeout** | `TimeoutMiddleware(timeout_seconds=...)` — a `503` `service-unavailable` problem (`REQUEST_TIMEOUT`) if no response has started in time | **Not provided** — set a deadline on the gRPC channel/client call yourself |
| **Health** | `health_router` — `/health`, `/live`, `/ready` (200 unless custom readiness checks fail) | — |
| **Error type** | `PlatformException` hierarchy → RFC 9457 problems via `register_exception_handlers` | `ContextServerInterceptor` sanitizes every RPC (unary and streaming): a status the handler chose (`context.abort` / `set_code`) is kept; any other exception becomes `INTERNAL "internal server error"` and is logged as `unclassified error` (types and frames, never the message). Without it grpcio sends `UNKNOWN "Exception calling application: <message>"` and logs the message |
| **Outbound propagation** | `create_platform_http_client()` — `httpx.AsyncClient` that stamps identity/trace headers on every request | `ContextClientInterceptor` — stamps identity metadata on outbound unary-unary calls |
| **OpenTelemetry init** | Automatic in `create_platform_app` (`ensure_tracing` from the settings + `OTEL_*`) | `init_tracing_from_env()` (or `init_tracing(TracingConfig(...))`) once at startup; `ContextServerInterceptor` starts the spans |

**Not a 1:1 port of `platform-gincommon`.** The gRPC column above is intentionally
thinner than the Go sibling's `grpccommon` package — there is no panic-recovery
interceptor, no auth-rejection interceptor, and no RBAC interceptor for gRPC in this
library today. See [Out of scope](#out-of-scope).

---

## Install (consumer services)

```bash
pip install platform-fastapicommon
# or, for gRPC support:
pip install "platform-fastapicommon[grpc]"
```

The PyPI **distribution name** is `platform-fastapicommon` (matches the repo name), but the
**importable package** stays `fastapicommon` — no code changes needed if you're already
using it: `from fastapicommon.bootstrap import create_platform_app` still works exactly as
shown throughout this README. (`fastapicommon` alone was rejected by PyPI as too similar to
an existing unrelated package, `fastapi-common`.)

**Python version:** 3.11+ (see `pyproject.toml`). Pin a release in production — see
[Versioning & releases](#versioning--releases) below.

---

## Versioning & releases

This package follows **[Semantic Versioning](https://semver.org/)**. Git tags are the
source of truth for releases. Full policy: **[VERSIONING.md](./VERSIONING.md)**. History:
**[CHANGELOG.md](./CHANGELOG.md)**. Security policy and trust model:
**[SECURITY.md](./SECURITY.md)**.

### Release line

| Version | Status | `pip install` | Python | Notes |
|---------|--------|----------------|--------|-------|
| *Unreleased* | — | from source / `main` | 3.11+ | Not yet tagged; pre-1.0, see the caveat below |

### SemVer guarantees (public API: everything re-exported from a `fastapicommon.*` subpackage's `__all__`)

| Change type | Version bump |
|-------------|--------------|
| Breaking change in the public API (removed/renamed export, incompatible defaults) | **MAJOR** |
| New feature, optional `PlatformSettings` field, new middleware (backward compatible) | **MINOR** |
| Bug fix, security fix | **PATCH** |

**Not covered by SemVer:** any name prefixed with `_`, `examples/`, and `tests/` may change
in any release. **Pre-1.0 caveat:** while the package is `0.y.z`, a MINOR bump may still
include breaking changes per the SemVer spec — see [VERSIONING.md](./VERSIONING.md) for
details.

---

## HTTP — quick start

### Middleware order

```text
Incoming request
  → TracingMiddleware        (outermost — the OTel server span)
  → ContextMiddleware        (binds RequestContext for the whole request)
  → LoggingMiddleware        (the http_request access log)
  → UnhandledErrorMiddleware (unclassified exception → 500 problem; never re-raised to the server)
  → PrometheusMiddleware     (records the platform_http_* metrics)
  → TimeoutMiddleware        (innermost — 503 if no response has started in time)
  ╠═ always public (no auth) ══════════════════════════════════╗
  ║   GET  /health, /live, /ready  → health_router               ║
  ║   GET  /metrics                → metrics_router               ║
  ╚═══════════════════════════════════════════════════════════════╝
  ╠═ your routes ══════════════════════════════════════════════╗
  ║   → [optional per route] Depends(require_authenticated_user) ║
  ║   → [optional per route] Depends(RequireRole(...))            ║
  ║   → your handler                                              ║
  ╚═══════════════════════════════════════════════════════════════╝
```

`TracingMiddleware` is added *last* in `create_platform_app`, which makes it *outermost*
(see [ARCHITECTURE.md § Execution model](./ARCHITECTURE.md#execution-model) for why) — the
same order as platform-gincommon's `ObservabilityMiddlewares`. Unlike
`platform-gincommon`'s `RequireAuth`, **there is no middleware that globally rejects
unauthenticated requests** — every route is reachable anonymously unless it explicitly adds
an auth or RBAC dependency.

### Complete HTTP example

```python
from fastapi import APIRouter, Depends

from fastapicommon.auth import CurrentUser, get_current_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import NotFoundException
from fastapicommon.logging import get_logger
from fastapicommon.rbac import RequireRole

logger = get_logger(__name__)

# PlatformSettings() reads APP_NAME, APP_ENV, OBSERVABILITY_DOMAIN (required while metrics are
# on), OTEL_EXPORTER_OTLP_ENDPOINT, etc. from the environment — see "Environment variables" below.
app = create_platform_app(settings=PlatformSettings())

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get("/me")
async def whoami(user: CurrentUser = Depends(get_current_user)) -> dict:
    # user_id/tenant_id are None, roles is () if the caller sent no identity headers —
    # this route has no auth dependency, so it never rejects anonymous callers.
    logger.info("whoami_requested", user_id=user.user_id)
    return {"user_id": user.user_id, "tenant_id": user.tenant_id, "roles": user.roles}


@router.delete("/{widget_id}", dependencies=[Depends(RequireRole("tenant_admin"))])
async def delete_widget(widget_id: str) -> dict:
    if widget_id == "missing":
        raise NotFoundException(f"widget {widget_id} not found")
    return {"status": "deleted"}


app.include_router(router)
```

Run it: `uvicorn myservice:app --reload` (or see `examples/fastapi_service/main.py` and
`make run` in this repo for a runnable copy of this exact pattern).

### Outbound header propagation

When a handler calls another platform service, use `create_platform_http_client()` so the
receiving service can correlate the call:

```python
from fastapicommon.propagation import create_platform_http_client

async def list_items_handler() -> dict:
    async with create_platform_http_client() as client:
        # Injects x-user-id, x-tenant-id, x-tenant-roles, x-request-id, x-correlation-id (from
        # the bound RequestContext, only when set) and traceparent (via OTel propagation).
        resp = await client.get("http://inventory-svc/internal/stock")
    return resp.json()
```

### Error response shape

Every error — `PlatformException` subclasses, request validation, `HTTPException`, unmatched
routes, timeouts and the unhandled-exception fallback — is an RFC 9457 problem
(`application/problem+json`), identical to `platform-gincommon` v2's:

```json
{
  "type": "https://errors.company.com/platform/not-found",
  "title": "Not Found",
  "status": 404,
  "detail": "Widget 42 was not found.",
  "instance": "/widgets/42",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errorCode": "NOT_FOUND",
  "timestamp": "2026-10-15T10:45:22Z"
}
```

```python
from fastapicommon.exceptions import NotFoundException, PlatformException
from fastapicommon.problem import problem_type

TENDER_NOT_FOUND = problem_type("tender", "not-found", "Tender not found", 404, "TENDER_NOT_FOUND")

raise NotFoundException("Widget 42 was not found.")
raise PlatformException("Tender 123 was not found.", problem_type=TENDER_NOT_FOUND)
```

Validation failures are `400 platform/validation-failed` with `validationErrors`
(`[{"field": "items[0].sku", "message": "must be at most 8 characters"}]`), a 5xx never carries
a `detail`, and `tenantId` is present only for an authenticated identity (never on a 401).
Clients parse problems with `fastapicommon.problem.problem_from_response(resp)`. Full contract,
mapping and migration: [docs/problem-details.md](docs/problem-details.md).

---

## gRPC — quick start

Requires the `grpc` extra: `pip install "platform-fastapicommon[grpc]"`.

As the [Features](#features) table shows, gRPC support here is **context propagation +
metrics + logging only** — there's no auth-rejection or RBAC interceptor to add. A
protected gRPC service must check identity itself inside each handler.

### Complete gRPC example

```python
from concurrent import futures

import grpc

from fastapicommon.grpc import ContextServerInterceptor
from fastapicommon.logging import configure_logging, get_logger
from fastapicommon.telemetry import init_tracing_from_env

# your generated stubs:
import my_service_pb2
import my_service_pb2_grpc

logger = get_logger(__name__)


class ItemService(my_service_pb2_grpc.ItemServiceServicer):
    def GetItem(self, request, context):
        from fastapicommon.context import get_request_context

        rc = get_request_context()  # bound by ContextServerInterceptor
        if not rc.user_id:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing x-user-id")

        logger.info("get_item", user_id=rc.user_id, tenant_id=rc.tenant_id)
        return my_service_pb2.GetItemResponse(id=request.id)


def serve() -> None:
    configure_logging()
    init_tracing_from_env()  # OTEL_SERVICE_NAME / APP_NAME + OTEL_*; the interceptor starts the spans

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[ContextServerInterceptor()],
    )
    my_service_pb2_grpc.add_ItemServiceServicer_to_server(ItemService(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    server.wait_for_termination()
```

This repo's own `examples/grpc_service/server.py` uses `grpc`'s generic-handler API with
raw bytes instead of compiled `.proto` stubs, purely so the demo runs with no protoc build
step — see that file for a runnable variant of the pattern above (`make run` runs the
FastAPI example; run the gRPC one directly with
`python -m examples.grpc_service.server`).

To emit the `platform_grpc_*` metrics from a gRPC-only service (no `create_platform_app()`),
pass the settings to the interceptor — `ContextServerInterceptor(PlatformSettings())` — which
initializes metrics exactly like `create_platform_app()` does (and raises on the same missing
domain or invalid identities). See [Platform Observability Standard metrics](#platform-observability-standard-metrics).

### `grpc.aio` servers

An asyncio server uses `AsyncContextServerInterceptor`, which does the same: server span,
request context, `platform_grpc_*` metrics, the `grpc_request` log and exception sanitizing.

```python
from fastapicommon.grpc import AsyncContextServerInterceptor

server = grpc.aio.server(interceptors=[AsyncContextServerInterceptor(PlatformSettings())])
```

### Required gRPC metadata

| Metadata key | Enforced? | Notes |
|-------------|-----------|-------|
| `x-user-id` / `x-tenant-id` | **No** — `ContextServerInterceptor` never rejects a call for missing metadata | Check `get_request_context().user_id` yourself if the RPC must be authenticated |
| `x-tenant-roles` | No | Comma-separated (e.g. `admin,user`); parsed the same way as the HTTP header |
| `x-request-id` / `x-correlation-id` | No | Generated if absent |

### Outbound propagation

```python
channel = grpc.intercept_channel(
    grpc.insecure_channel("inventory-svc:50051"),
    ContextClientInterceptor(),
)
```

---

## RequestContext

Both HTTP and gRPC handlers read the same structure via
`fastapicommon.context.get_request_context()`:

```python
@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: str | None = None
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    correlation_id: str
    user: CurrentUser = field(default_factory=CurrentUser)
```

**Unlike `platform-gincommon`'s `RequestContext`, there is no `TraceID` or `ClientIP`
field.** If you need the active OTel trace ID in a log line, read it yourself from
`opentelemetry.trace.get_current_span()` — `fastapicommon.logging` does not enrich log
records with it automatically (see [Correlation ID vs Trace ID](#correlation-id-vs-trace-id)).

**HTTP:** `Depends(get_current_user)` for just the user, or
`fastapicommon.context.get_request_context()` for the full context (raises `LookupError`
if called outside a bound context — see
[ARCHITECTURE.md § Failure domains](./ARCHITECTURE.md#failure-domains)).
**gRPC:** same `get_request_context()`, valid inside a unary-unary handler wrapped by
`ContextServerInterceptor`.

---

## Platform Observability Standard metrics

This library implements the Enterprise Platform Observability Standard the same way the Go
original [`platform-gincommon`](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon)
does (Go is the source of truth). Metric names, help text, label names and order, label
vocabularies and histogram buckets come from the Platform Observability Registry, vendored
byte-for-byte at [`src/fastapicommon/metrics/registry.json`](src/fastapicommon/metrics/registry.json)
(a copy of gincommon's `internal/obsregistry/registry.json`; CI fails if the two drift). The
full guide — tiers, the metric constructors for a service's own metrics, CI enforcement,
SLOs, runbooks — is [`docs/observability/`](docs/observability/README.md), with the generated
[metric inventory](docs/observability/metric-registry.md).

### Configuration

The domain is **mandatory** while metrics are enabled (the default):

```python
settings = PlatformSettings(
    app_name="event-consumer",     # service label: ^[a-z][a-z0-9_-]{0,62}$
    observability_domain="iam",    # or OBSERVABILITY_DOMAIN
    environment="prod",            # or APP_ENV, then ENVIRONMENT: local|dev|test|staging|prod
    build_version="v1.2.3",        # or BUILD_VERSION; "unknown" when unset
)
app = create_platform_app(settings=settings)
```

- The `platform_*` metrics below — the only HTTP/gRPC metrics the library emits — carry the
  const labels `domain`, `service` and `environment` on every series.
- `create_platform_app()` (and `ContextServerInterceptor(settings)`) **raise `ValueError` at
  startup** — where Go panics — when no domain is configured (neither `observability_domain`
  nor `OBSERVABILITY_DOMAIN`; the message names `OBSERVABILITY_DOMAIN`), on a domain that is not
  in the registry, an `app_name` that is not a valid service label, or an environment that is
  missing or not one of
  `local, dev, test, staging, prod`. There are no aliases: `production` and `development` are
  rejected. `PlatformSettings.environment` defaults to `""` (unset); set
  `APP_ENV`/`ENVIRONMENT` or pass `environment=` explicitly.
- With `enable_metrics=False` (`ENABLE_METRICS=false`) no domain is needed: no metrics are
  initialized and `/metrics` is not mounted. A domain that is configured is still validated.
- Initialization is process-wide and the first app/interceptor created in the process sets the
  identity (like gincommon's `sync.Once`). In tests, call
  `fastapicommon.metrics.init_metrics(options, registry=CollectorRegistry())` first to get an
  isolated registry; `/metrics` then serves that registry.

### Emitted metrics

Const labels on every `platform_*` series: `domain`, `service`, `environment`.

| Metric | Type | Labels | Recorded by |
|---|---|---|---|
| `platform_build_info` | gauge (always 1) | `version` | initialization |
| `platform_http_requests_total` | counter | `method`, `route`, `status_class`, `error_class` | `PrometheusMiddleware` |
| `platform_http_request_duration_seconds` | histogram (`request_duration` buckets) | same | `PrometheusMiddleware` |
| `platform_http_request_size_bytes` | histogram (`payload_size` buckets) | same | `PrometheusMiddleware` (Content-Length, 0 when unknown) |
| `platform_http_response_size_bytes` | histogram (`payload_size` buckets) | same | `PrometheusMiddleware` (Content-Length, else bytes streamed) |
| `platform_http_active_requests` | gauge | `route` | `PrometheusMiddleware` |
| `platform_http_panics_total` | counter | `method`, `route` | `PrometheusMiddleware` — unhandled exceptions (also counted as 5xx above) |
| `platform_http_rate_limited_requests_total` | counter | `method`, `route` | `PrometheusMiddleware` — any 429 response |
| `platform_http_request_timeouts_total` | counter | `method`, `route` | `TimeoutMiddleware` — each 503 timeout problem it returns |
| `platform_grpc_server_handled_total` | counter | `grpc_method`, `grpc_code`, `status_class`, `error_class` | `ContextServerInterceptor` (unary-unary) |
| `platform_grpc_server_handling_seconds` | histogram (`request_duration` buckets) | `grpc_method`, `status_class`, `error_class` | same |
| `platform_grpc_server_request_size_bytes` / `_response_size_bytes` | histogram (`payload_size` buckets) | same | same (protobuf `ByteSize()`, 0 for non-protobuf messages) |
| `platform_grpc_server_inflight_requests` | gauge | `grpc_method` | same |
| `platform_grpc_rate_limited_requests_total` | counter | `grpc_method` | same — RPCs ending `ResourceExhausted` |
| `platform_grpc_panics_total` | counter | `grpc_method` | same — handler exceptions without an explicit status (client sees `INTERNAL "internal server error"`) |
| `platform_unclassified_errors_total` | counter | `transport` (`http` \| `grpc`) | every exception nothing classified (`UnhandledErrorMiddleware` / the exception handler, and the gRPC interceptor); both series start at 0. Alert on it (gincommon's `PlatformUnclassifiedErrors`); diagnose with the `unclassified error` log event |

Label values, all bounded:

- `method` — the standard HTTP methods; anything else (e.g. `PURGE`) is `OTHER`.
- `route` — the route template in the registry's (Gin-style) syntax, so Go and Python services
  report routes identically: `/items/{item_id}` → `/items/:item_id`, `{rest:path}` → `*rest`.
  Unmatched requests, 405s and slash redirects are `unknown` — never the raw URL path.
- `status_class` / `error_class` — `2xx`/`success` for HTTP 200-399, `4xx`/`client_error` for
  400-499, else `5xx`/`server_error`; for gRPC, `OK` is 2xx, client-side codes (`Canceled`,
  `InvalidArgument`, `NotFound`, `AlreadyExists`, `PermissionDenied`, `Unauthenticated`,
  `FailedPrecondition`, `OutOfRange`, `Aborted`, `DeadlineExceeded`, `ResourceExhausted`) are 4xx,
  the rest 5xx — identical to gincommon.
- `grpc_code` — Go's code names (`NotFound`, `Canceled`, ...); `grpc_method` — the full method
  (`/package.Service/Method`) or `unknown`.

The `platform_*` collectors do not expose prometheus_client's `*_created` samples (Go's client
has none, and Go's `metricslint check` would read them as unregistered `platform_*` families).

### Pre-standard metric names

The library's pre-standard metric names were removed outright, with no compatibility period:
neither this library nor gincommon was ever deployed, so nothing depended on them. See
[`docs/observability/migration.md`](docs/observability/migration.md) for the mapping;
`fastapicommon-metricslint check` / `refs` (and Go's `metricslint`) report them as errors.

### A service's own metrics

Build them through the registry-driven constructors (Go `gincommon.NewShared*` /
`NewCounterVec` ...), never `prometheus_client` directly, so they carry the identity labels and
follow the tier rules:

```python
from fastapicommon.metrics import new_counter, new_shared_counter

processed = new_shared_counter("platform_messages_processed_total")  # Tier 1/2: from the registry
processed.labels(queue="iam.events", event_type="user.created").inc()

hmac_failures = new_counter(  # Tier 3: <domain>_<service>_<metric>, checked at construction
    "iam_event_consumer_hmac_failures_total", "HMAC verification failures", ["reason"]
)
```

### Compliance checks

`fastapicommon.metrics.compliance.validate()` applies the standard's full rule set (Go
`obsregistry.Validate`) to what `/metrics` serves; add it to the service's tests:

```python
from fastapicommon.metrics import metrics_registry
from fastapicommon.metrics.compliance import validate

report = validate(metrics_registry())
assert report.ok(), "\n".join(map(str, report.errors()))
```

The `fastapicommon-metricslint` CLI (Go `metricslint`: `check`, `refs`, `registry`, `inventory`)
checks a live scrape (`check --url http://localhost:8080/metrics`) and scans dashboards, alert
rules and HPA manifests for removed or unregistered names (`refs deploy/ dashboards/`).
`tests/integration/test_metrics_compliance.py` drives every instrumented code path through
`create_platform_app()` and the gRPC interceptor and validates the scrape: 0 errors. In CI,
`make metrics-lint` checks the vendored registry, the reference Prometheus rules
(`docker/prometheus/rules/`, promtool) and the inventory, and `make gincommon-parity` that the
registry, rules and inventory are identical to platform-gincommon's.

---

## Logs and traces (parity with platform-gincommon)

Logs and traces follow platform-gincommon's contract (its LLD §7.2 and §7.3), so Go and Python
services read the same in Loki and Tempo.

**Log format.** One JSON object per line on stderr, Zap's production layout — `level`
(`debug` / `info` / `warn` / `error`), `ts` (Unix seconds), `caller` (`dir/file.py:line`),
`msg`, the entry's fields, and `stacktrace` on error entries. In `dev` / `local` the layout is
Zap's console format (`time  LEVEL  caller  msg  {fields}`), with the stack trace from warn up.
`LOG_LEVEL`, `LOG_SAMPLING` and `LOG_FILE` work as in Go.

**Log events** emitted by the library:

| Message | Level | Fields |
|---|---|---|
| `http_request` | info 1xx-3xx · warn 4xx · error 5xx (an escaped exception is 500) | `trace_id`, `request_id`, `method`, `path` (the route template, never the raw path), `status`, `duration_ms`, `tenant_id` / `user_id` — or `claimed_tenant_id` / `claimed_user_id` for identity headers that failed validation |
| `grpc_request` | info OK · warn client codes · error otherwise | `method` (bounded), `grpc_code` (Go names, e.g. `NotFound`), `duration_ms`, `request_id`, `user_id` / `tenant_id` |
| `auth: request rejected` | warn | `reason`, `method`, `route`, `status`, `client_ip`, `trace_id`, `request_id`, `claimed_user_id`, `claimed_tenant_id` (from `require_authenticated_user`) |
| `authz: access denied` | warn | `tenant_id`, `user_id`, `roles`, `action` (e.g. `require_role:admin`), `resource` (the route) |
| `problem response` | warn | `traceId`, `type`, `status`, `errorCode`, `service`, `tenantId` |
| `rate limited` | warn, ≤ 1 per route per second | `route`, `method`, `status`, `suppressed` |
| `metrics handler error` | error, ≤ 1 per distinct error per 5 min | `error` |
| `otel_tracing_initialized` | info, once per install | `service`, `version`, `domain`, `environment`, `endpoint`, `insecure`, `exporter`, `sample_ratio`, `ignore_remote_parent_sampled`, `baggage`, `provisional` |
| `otel_exporter_init_failed` / `otel_shutdown_flush_failed` / `otel_error` | warn | `error` (+ `endpoint`, `fallback`) |

The access, auth and gRPC entries redact sensitive keys, and header-derived values are stripped
of control characters and capped at 256 bytes (`fastapicommon.logging.sanitize`, gincommon's
`log_sanitizer.go`).

**Spans.**

| | HTTP (`TracingMiddleware`) | gRPC (`ContextServerInterceptor`) |
|---|---|---|
| Name | `METHOD route` (`GET /items/:item_id`; `HTTP unknown` for a non-standard method on an unmatched route) | the bounded `grpc_method` (`unknown` when malformed) |
| Attributes | `http.request.method` (`_OTHER` + `http.request.method_original` ≤ 32 bytes), `http.route`, `url.path` (≤ 512), `user_agent.original` (≤ 512), `server.address` (≤ 256), `client.address`, `http.response.status_code` | `rpc.system.name`, `rpc.service`, `rpc.method` (`_OTHER` + `rpc.method_original`), `rpc.response.status_code` (canonical, e.g. `NOT_FOUND`), `rpc.grpc.status_code` (int) |
| Error status | 5xx, or an exception (`panic`; recorded with its type only, never the message) | any non-OK code |
| Resource | `service.name`, `service.version`, `service.namespace` (the domain), `deployment.environment.name` | same |

Code outside the middleware uses `fastapicommon.telemetry`: `new_tracer(scope)`,
`span_trace_id()`, `record_span_error(exc, description)`, `inject_trace_context(headers)`.

---

## Required HTTP headers

| Header | Enforced? | Purpose |
|--------|-----------|---------|
| `x-user-id` | No | User identity — must be injected by a trusted API gateway; read verbatim, not cryptographically verified |
| `x-tenant-id` | No | Tenant identity — same trust assumption |
| `x-tenant-roles` | No | Comma-separated roles (e.g. `admin,user`); split and stripped, **not** format-validated or deduplicated |
| `x-request-id` | No | Any non-empty value is accepted and echoed back verbatim; a UUID is generated only if absent |
| `x-correlation-id` | No | Same as above |
| `traceparent` | No | W3C trace context — continues an upstream OTel trace if present |

**No header is required or format-validated.** This is a real difference from
`platform-gincommon`, which validates length/character-set on identity headers and
request IDs. See [SECURITY.md § Trust model](./SECURITY.md#trust-model-and-known-limitations)
for the full list of what this library does and doesn't guard against.

---

## Correlation ID vs Trace ID

`RequestContext` carries both, as in platform-gincommon: `request_id` / `correlation_id` from
the `x-request-id` / `x-correlation-id` headers (or generated), and `trace_id` from the active
server span (continuing an inbound `traceparent`). Every log entry written inside a request
carries all three, the `http_request` / `grpc_request` entries and problem responses
included, and the response echoes `X-Request-ID` and `X-Trace-ID` — so Loki and Tempo
correlate in both directions. `fastapicommon.telemetry.span_trace_id()` reads the trace id
outside a request context.

---

## Calling the API (local testing)

```bash
# Public route — no auth headers
curl -i http://localhost:8080/health

# Anonymous call — no auth enforced, no identity in the response
curl -i http://localhost:8080/widgets/me

# With identity headers
curl -i http://localhost:8080/widgets/me \
  -H "x-user-id: user-1" \
  -H "x-tenant-id: tenant-1" \
  -H "x-tenant-roles: tenant_admin" \
  -H "x-request-id: my-req-001"

# Admin delete (RequireRole("tenant_admin"))
curl -i -X DELETE http://localhost:8080/widgets/42 \
  -H "x-user-id: user-1" -H "x-tenant-id: tenant-1" -H "x-tenant-roles: tenant_admin"

# Propagate an upstream trace
curl -i http://localhost:8080/widgets/me \
  -H "x-user-id: user-1" -H "x-tenant-id: tenant-1" \
  -H "traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
```

Successful and error responses alike include `x-request-id` and `x-correlation-id`
headers.

---

## Security

| Topic | Guidance |
|--------|----------|
| **Trust boundary** | `x-user-id`, `x-tenant-id`, `x-tenant-roles` must be set by a **trusted API gateway**. `ContextMiddleware` reads them verbatim and does not verify or reject anything. |
| **No global auth enforcement** | Every route is anonymous-reachable by default. Add `Depends(require_authenticated_user)` or an RBAC dependency per route that needs it. |
| **Authorization** | `RequireRole`/`RequireAnyRole`/`RequireAllRoles` check roles already present on the bound `RequestContext` — they don't verify the roles weren't forged by the client. The gateway must not forward client-supplied role headers. |
| **`/metrics`** | Served without authentication whenever `enable_metrics` is true. Restrict via network policy, an auth proxy, or a dedicated admin port in production. |
| **OTLP** | Insecure (no TLS) by default only when `environment` is `dev` / `local` (as in gincommon; `development` is not a dev environment); TLS otherwise. `OTEL_EXPORTER_OTLP_INSECURE` overrides. See [ARCHITECTURE.md § Tracing initialization](./ARCHITECTURE.md#tracing-initialization). |
| **Logging** | The library's own entries (`http_request`, `grpc_request`, auth / authz, `unclassified error`) go through gincommon's sanitizer: sensitive keys redacted, control characters stripped, values capped. Fields a service logs itself are not sanitized. `x-request-id` is validated (`[A-Za-z0-9._-]`, ≤128 bytes) before it is echoed. See [SECURITY.md](./SECURITY.md). |

Full policy: **[SECURITY.md](./SECURITY.md)**.

---

## Important rules for handler authors

| Rule | Why |
|------|-----|
| **Never read identity headers directly** (`request.headers["x-user-id"]`) in a handler | Bypasses nothing today (there's no validation to bypass), but breaks the moment this library adds validation. Always use `Depends(get_current_user)` or `get_request_context()`. |
| **Never write identity headers in a service response** | Identity headers flow inbound from the gateway only. Use `create_platform_http_client()` for outbound calls instead of hand-copying headers. |
| **Add RBAC/auth dependencies explicitly on every route that needs them** | Unlike `platform-gincommon`, nothing is enforced by middleware — a route with no `Depends(...)` is reachable anonymously, full stop. |
| **Use `create_platform_http_client()` for all service-to-service calls** | Otherwise `traceparent`/`x-request-id`/`x-correlation-id` don't reach the next hop, and the distributed trace breaks at that boundary. |
| **Don't add an auth dependency to `/health`, `/live`, `/ready`** | They're unauthenticated by design (`health_router` has no `Depends`) — load balancer and k8s probes don't send identity headers. Wrapping them in an auth check breaks liveness/readiness probing. |

### Common mistakes

**Reading identity headers directly (fragile):**

```python
# ❌ Bypasses get_current_user / RequestContext entirely
user_id = request.headers.get("x-user-id")
```

**Using the dependency (correct):**

```python
# ✅ Consistent with everything else that reads identity
async def handler(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"user_id": user.user_id}
```

**Outbound call without propagation (breaks tracing):**

```python
# ❌ Downstream service gets no trace context or identity
async with httpx.AsyncClient() as client:
    await client.get("http://other-svc/data")
```

**Outbound call with propagation (correct):**

```python
# ✅ traceparent, x-request-id, x-correlation-id, x-user-id, x-tenant-id all forwarded
async with create_platform_http_client() as client:
    await client.get("http://other-svc/data")
```

---

## Public API

See **[ARCHITECTURE.md § Public API](./ARCHITECTURE.md#public-api)** for the complete
symbol table for every `fastapicommon.*` module — kept there rather than duplicated here
so the two docs can't drift out of sync.

| Module | Responsibility |
|---|---|
| `fastapicommon.auth` | Header constants, `CurrentUser`, `get_current_user`, `require_authenticated_user` |
| `fastapicommon.context` | contextvars-based `RequestContext` propagation |
| `fastapicommon.logging` | structlog JSON logging enriched with request context |
| `fastapicommon.telemetry` | OpenTelemetry tracing setup for HTTP/gRPC |
| `fastapicommon.metrics` | Prometheus HTTP/gRPC metrics + `/metrics` router; `init_metrics`, `MetricsOptions`, `metrics_registry`, `default_registry`; `new_counter` / `new_gauge` / `new_histogram`, `new_shared_*`, `register_labeled` (observability standard); `fastapicommon.metrics.compliance.validate` |
| `fastapicommon.middleware` | `ContextMiddleware`, `TimeoutMiddleware` |
| `fastapicommon.rbac` | `RequireRole` / `RequireAnyRole` / `RequireAllRoles` dependencies |
| `fastapicommon.exceptions` | `PlatformException` hierarchy + handlers |
| `fastapicommon.health` | `/health`, `/live`, `/ready` router |
| `fastapicommon.config` | `PlatformSettings` (pydantic-settings) |
| `fastapicommon.bootstrap` | `create_platform_app()` |
| `fastapicommon.propagation` | `create_platform_http_client()` |
| `fastapicommon.grpc` | Server/client interceptors for gRPC services (requires the `grpc` extra) |

---

## Environment variables (consumer service)

`PlatformSettings` (`fastapicommon.config`) reads these from the process environment /
`.env`. Field names are matched case-insensitively; `environment` additionally accepts
`APP_ENV` as an alias (see the field-by-field table):

| Variable | Example | Default | `PlatformSettings` field |
|----------|---------|---------|---------------------------|
| `APP_NAME` | `my-service` | `platform-service` | `app_name` — FastAPI title + OTel `service.name` |
| `APP_ENV` (or `ENVIRONMENT`) | `prod` | unset (`""`; production defaults) | `environment` — `dev` / `local` select the developer defaults (console logs, debug level, plaintext OTLP, 100% sampling), as in gincommon |
| `LOG_LEVEL` | `debug` | debug in dev/local, else info | `log_level` — `debug`, `info`, `warn`, `error` |
| `LOG_SAMPLING` | `true` | `false` | Zap-style sampling: per second and message+level, the first 100 entries, then every 100th |
| `LOG_FILE` | `logs/app.log` | unset | Also write JSON logs to this file |
| `REQUEST_TIMEOUT_SECONDS` | `10` | `30.0` | `request_timeout_seconds` — `TimeoutMiddleware` deadline |
| `ENABLE_TRACING` | `false` | `true` | `enable_tracing` — gates `setup_tracing()` entirely |
| `ENABLE_METRICS` | `false` | `true` | `enable_metrics` — gates `PrometheusMiddleware` + `/metrics` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `otel-collector:4317` | `localhost:4317` | OTLP/gRPC collector; `none` / `off` / `disabled` (or `OTEL_TRACES_EXPORTER=none`) keeps spans in-process |
| `OTLP_ENDPOINT` | `otel-collector:4317` | unset | `otlp_endpoint` — overrides `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | true only in dev/local | Plaintext OTLP |
| `OTEL_TRACES_SAMPLER_RATIO` (or `OTEL_TRACES_SAMPLER_ARG`) | `0.25` | 1.0 in dev/local, else 0.1 | Root sampling ratio; always parent-based |
| `OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED` | `true` | `false` | Re-sample a sampled remote parent, so callers cannot force sampling |
| `OTEL_PROPAGATOR_BAGGAGE` | `true` | `false` | Also propagate W3C baggage |
| `OBSERVABILITY_DOMAIN` | `iam` | unset — **required** while `ENABLE_METRICS` is true | `observability_domain` — registry domain of the [`platform_*` metrics](#platform-observability-standard-metrics); startup raises `ValueError` without one |
| `BUILD_VERSION` | `v1.2.3` | unset (`unknown`) | `build_version` — `version` label of `platform_build_info` |

While metrics are enabled, `APP_ENV` / `ENVIRONMENT` (or an explicit `environment=`) is also the
`environment` label and must be one of `local, dev, test, staging, prod`.

---

## Project layout

```
platform-fastapicommon/
├── src/fastapicommon/
│   ├── context/        # RequestContext, CurrentUser — no framework deps
│   ├── exceptions/      # PlatformException hierarchy — no framework deps
│   ├── config/          # PlatformSettings — no framework deps
│   ├── auth/            # header constants, get_current_user
│   ├── middleware/      # ContextMiddleware, TimeoutMiddleware
│   ├── logging/         # structlog JSON configuration
│   ├── metrics/         # Prometheus definitions + middleware + /metrics router; platform_* metrics + vendored registry.json
│   ├── telemetry/       # OpenTelemetry setup (HTTP + gRPC)
│   ├── rbac/            # RequireRole / RequireAnyRole / RequireAllRoles
│   ├── health/          # /health, /live, /ready router
│   ├── propagation/     # create_platform_http_client
│   ├── grpc/            # server/client interceptors (requires the grpc extra)
│   └── bootstrap/       # create_platform_app() — the single entry point
├── tests/
│   ├── unit/             # mirrors src/fastapicommon/ one-to-one
│   ├── integration/      # multi-module tests (create_platform_app() end-to-end)
│   └── e2e/              # builds + runs the real Docker image, real HTTP calls (requires Docker)
├── examples/
│   ├── fastapi_service/  # reference HTTP service (this README's HTTP example, runnable)
│   └── grpc_service/     # reference gRPC service (raw-bytes generic handler demo)
├── docs/adr/             # ADRs for cross-cutting standards
├── docs/architecture/mermaid/  # Mermaid diagram sources embedded in ARCHITECTURE.md
├── ARCHITECTURE.md        # Module graph, flow diagrams, invariants, public API tables
├── docker-compose.yml     # Local OTel Collector / Tempo / Prometheus / Grafana / Loki
└── .env-example           # Local dev environment template
```

---

## Local development (this repo)

Dependency management uses [`uv`](https://docs.astral.sh/uv/) (matching
`platform-pgcommon-py`'s convention) — `pyproject.toml`'s `[dependency-groups].dev`
plus the `grpc`/`examples` extras are locked into `uv.lock`, committed to the repo for
reproducible installs. Install `uv` yourself first (`brew install uv` or see uv's docs);
`make install` assumes it's already on `PATH`.

### Setup

```bash
make setup     # copies .env-example -> .env, creates logs/
make install   # uv sync (dev group + grpc extra, from uv.lock) + install pre-commit hooks
```

### Common commands

| Command | Description |
|---------|-------------|
| `make lint` | ruff check |
| `make typecheck` | mypy --strict |
| `make format` | ruff format + black (rewrites files) |
| `make format-check` | Same, but check-only — no changes; used in CI |
| `make vulncheck` | pip-audit against locked dependencies |
| `make test-unit` | `tests/unit` only — no Docker, no wired-up app |
| `make test-int` | `tests/integration` only — `create_platform_app()` end-to-end |
| `make test-e2e` | `tests/e2e` only — builds + runs the real Docker image, real HTTP calls (requires Docker) |
| `make test-all` | All three tiers together (coverage gate ≥90%, enforced by `pyproject.toml`; requires Docker) |
| `make cover` | `test-all`, then open an HTML coverage report |
| `make build` | Wheel + sdist into `dist/` (hash-locked build backend), `twine check --strict`, installed-wheel check |
| `make run` | Run the example FastAPI service (`uvicorn --reload`) |
| `make docker-build` | Build the example service Docker image locally |
| `make docker-up` / `make docker-down` | Start/stop the local observability stack |
| `make smoke-test` | Build the image, run `smoke-tests.sh` and `tests/e2e` against it (CI's "Smoke tests" job; requires Docker) |
| `make ci` | CI's validate-quality + validate-test: lock / pip check, format, lint, mypy, docs, CI script tests, pip-audit, build, `test-ci` + coverage gate |
| `make api-compat` | griffe public-API diff against the last release tag |
| `make metrics-lint` | Observability standard: registry self-check, rule references, promtool on the reference rules, inventory drift |
| `make metrics-inventory` | Regenerate `docs/observability/metric-registry.md` from the vendored registry |
| `make gincommon-parity` | Registry, reference rules and inventory identical to the sibling platform-gincommon checkout (`GINCOMMON_DIR`) |
| `make release-guard TAG=vX.Y.Z` | Package version == tag and a dated CHANGELOG section (run before tagging) |
| `make pin-base-images` | Re-pin the Dockerfile base images to their current `@sha256` digests |

### Example service (optional)

`examples/fastapi_service` is a **reference app** for local testing — the exact code shown
in [Complete HTTP example](#complete-http-example) above.

```bash
make run
curl -H "x-user-id: user-1" -H "x-tenant-id: tenant-1" http://localhost:8080/widgets/me
```

### Observability stack (optional)

```bash
make docker-up
```

Trace flow: **app** → OTLP/gRPC `localhost:4317` → **otel-collector** → **Tempo**.
Prometheus scrapes the app on `:8080/metrics`. Grafana is at `localhost:3000`
(Prometheus/Loki/Tempo datasources pre-provisioned, trace↔log correlation wired up).

**Logs:** the example service's structlog JSON output is duplicated to `logs/app.log`
(via `make run`'s `tee`, since this library itself has no built-in file sink). Promtail
ships that file into Loki. Run `make docker-up`, then `make run`, then generate traffic —
in Grafana → Explore → Loki, try `{job="platform-fastapicommon-example"}`.

This entire flow (metrics → Prometheus, traces → Tempo, logs → Loki) has been verified
end-to-end against this exact `docker-compose.yml`.

---

## Testing

- Tests are tiered under `tests/unit/` (mirrors `src/fastapicommon/` one-to-one),
  `tests/integration/` (multi-module, e.g. `create_platform_app()` end-to-end), and
  `tests/e2e/` (builds and runs the real Docker image, makes real HTTP calls against it
  over a real socket) — matching `platform-pgcommon-py`'s `unit`/`integration`/`e2e`
  tiering. `tests/integration`'s `TestClient` runs the ASGI app in-process and
  structurally cannot verify the packaged image actually works; `tests/e2e` is the one
  tier that does.
- **Coverage gate:** `pyproject.toml`'s `[tool.coverage.report].fail_under = 90` — enforced
  automatically on `make test-all`/`make cover`/`make ci` (no separate flag needed).
  Running `make test-unit` or `make test-int` alone will report below 90% since each only
  covers part of the codebase — expected, not a regression. `make test-e2e` alone passes
  `--no-cov`: it runs the app inside a separate Docker container process, so coverage.py
  (instrumenting the pytest process) has nothing to measure.
- `tests/e2e` requires Docker; it's skipped automatically if the `docker` CLI isn't on
  `PATH`. All other tests run without a live server or Docker.

---

## CI

The pipeline is platform-gincommon's (`.github/`), translated to the Python tooling as
platform-pgcommon-py and platform-eventcommon did. Third-party actions are pinned by commit
SHA. `ci.yml` runs on push/PR to `main`:

```
changes ─┬─► validate-test ──────┬──► interop (cross-language compatibility)
         ├─► build-image ────────┤──► trivy ──────┐
         │                       │   smoke ───────┤──► pr-summary (PR events only)
         │                       └────────────────┤──► push       (push events only)
         ├─► validate-quality ───────────────────┘
         └─► api-compat (informational)
```

- **Detect changes** — a documentation-only change (`*.md`, `docs/architecture/*.mmd`)
  skips the build/test jobs via `if:`, so the required checks still report (as skipped =
  success). `docs.yml` checks the diagrams on those.
- **Validate / Test / test** (required) — unit + integration with coverage on Python 3.12,
  then the coverage gate (≥ 90%, `pyproject.toml` `fail_under`). The same workflow also runs
  the suites on 3.11 / 3.13 / 3.14 and with every runtime dependency at its floor
  (`lowest-deps.sh`); all of them gate trivy, smoke, interop and push.
- **Validate / Quality / quality** (required) — lock check, `uv pip check`, HTML-entity
  check on workflows, ruff format + black, ruff, mypy --strict, diagram sync, CI script
  tests, pip-audit, digest-pinned `FROM` lines, and `make build` (hash-locked build backend,
  `twine check --strict`, installed-wheel check). Gates push.
- **API compatibility** — griffe against the last release tag; a warning here, blocking in
  `release.yml`.
- **Build image (cache)** (required) — Hadolint and a `.dockerignore` check first, then the
  example service image is built into the GHA + registry cache.
- **Trivy CVE scan** (required) — fails on CRITICAL / HIGH / UNKNOWN with a fix; SARIF to
  the Security tab; CycloneDX SBOM artifact.
- **Smoke tests** (required) — `.github/scripts/smoke-tests.sh` (size, fail-fast on an
  unknown `OBSERVABILITY_DOMAIN`, `/health`, `platform_*` metrics, RFC 9457 not-found,
  installed version, pip absent, numeric non-root UID), then `tests/e2e` against the same
  image. `make smoke-test` runs both locally.
- **PR summary** (required) — one status table, posted or updated on every PR run.
- **Push image → GHCR** (pushes to `main`) — rebuilds, and publishes only if the image's
  content fingerprint matches the one trivy and smoke judged (checked before the push and
  again on the pushed digest), then Cosign keyless-signs it.

`changelog-check.yml` fails a PR that changes `src/`, `pyproject.toml`, `uv.lock`,
`build-constraints.txt` or the `Dockerfile` without a CHANGELOG entry (Dependabot exempt).

Release (`v*` tags, `release.yml`): **verify** (dispatch ref, tag at HEAD and on `main`,
`pyproject.toml` version == tag, dated CHANGELOG section, which floating image tags may move)
→ the same gates as CI at the tag, plus blocking `api-compat` → **dist** (wheel + sdist built
once, reproducibly, with `SHA256SUMS`) → **push** (fingerprint-checked image `X.Y.Z`, and
`X.Y` / `X` only when newest of that line; never `latest`; SBOM, SLSA provenance, Cosign
signature) → **Publish to PyPI** (Trusted Publishing / OIDC, the same files) → **GitHub
Release** (CHANGELOG section, wheel, sdist, Cosign-signed `SHA256SUMS`, provenance, SBOM).
See [VERSIONING.md § Publishing to PyPI](./VERSIONING.md#publishing-to-pypi) for the
one-time account setup and [§ Verifying a release](./VERSIONING.md#verifying-a-release).

---

## Docker (example service only)

```bash
make docker-build
docker run --rm -p 8080:8080 -e APP_ENV=prod -e OBSERVABILITY_DOMAIN=iam platform-fastapicommon-example:local
```

The image runs the **example** service. Consumer services do not deploy this image.

---

## Out of scope

This library does **not** handle:

| Concern | Where it lives |
|---------|----------------|
| JWT validation, OAuth, API key auth | API gateway — identity is trusted and pre-validated before headers reach this library |
| Database access, connection pooling, retry logic | Your service's own data-access layer |
| Business validation logic | Your service's handlers |
| Rate limiting, circuit breaking | Infrastructure layer (API gateway, service mesh) |
| Request body parsing and binding | FastAPI's own `pydantic` models |
| gRPC panic recovery, auth rejection, or RBAC interceptors | Not implemented — see the [Features](#features) table |

This library only standardises **transport, observability, and identity context
propagation**. Keeping this scope narrow ensures it stays safe to upgrade across all
services without risk of business logic changes.

---

## License / ownership

BCBP Solutions FZC LLC — internal platform shared library.

---

## See also

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Module graph, flow diagrams, key invariants, full public API tables |
| [VERSIONING.md](./VERSIONING.md) | SemVer rules, supported versions, release process |
| [CHANGELOG.md](./CHANGELOG.md) | Per-version changes |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Development setup, adding capabilities, PR checklist |
| [SECURITY.md](./SECURITY.md) | Vulnerability reporting, trust model, supported versions |
