# platform-fastapicommon

The platform's shared **FastAPI and gRPC middleware library** for Python services: the transport-layer system of record for how every Python service in the platform correlates requests, traces, logs, emits metrics, trusts gateway identity, builds tenant context, answers errors and checks roles. It is the Python port of [`platform-gincommon`](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon) and implements the same contracts — the same `platform_*` metrics, OpenTelemetry spans, Zap-layout log lines and RFC 9457 error bodies — so Go and Python services read identically in Prometheus, Tempo, Loki and to API clients. It also implements the **Enterprise Platform Observability Standard** for Python: gincommon's metric registry, vendored byte for byte, the collectors built from it, and `fastapicommon-metricslint`.

**Repository:** [github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon)
**Package:** `pip install platform-fastapicommon` (import `fastapicommon`) · Python ≥ 3.11 (tested on 3.11, 3.13, 3.14) · **never deployed** — consumers import the package; `examples/` is a reference service whose image exists only for CI smoke tests
**Design:** a framework-free core (`context`, `auth.validation`, `config`, `problem`) under FastAPI / gRPC / OpenTelemetry adapters and one composition function, `create_platform_app()` ([ARCHITECTURE.md](ARCHITECTURE.md), [LLD](docs/lld/platform-fastapicommon-lld.md)). The observability contract is [`docs/observability/`](docs/observability/README.md) with the generated [metric inventory](docs/observability/metric-registry.md); the registry is **gincommon's** (`src/fastapicommon/metrics/registry.json`, parity-checked in CI). Every HTTP error is an [RFC 9457](docs/problem-details.md) problem (`application/problem+json`) identical to gincommon `v2.0.0`'s.

---

## Mental model

| This library owns | It does NOT own |
|---|---|
| The middleware **order** and what each layer records — `Tracing → Context → Logging → UnhandledError → Prometheus → Timeout → routing`, plus the gRPC server interceptors | Authentication itself — JWT/OAuth/API keys are validated by the API gateway before headers reach a service |
| Gateway-identity **validation**, on HTTP and gRPC: `x-user-id` / `x-tenant-id` (non-empty, ≤ 256 bytes, no control characters, **exactly one value**), `x-tenant-roles` (`[a-zA-Z0-9_-]+`, deduplicated, ≤ 4096 bytes, ≤ 64 roles) | Cryptographic identity verification — headers are trusted at the network layer |
| `RequestContext{request_id, correlation_id, user, trace_id, client_ip, identity_rejection}` bound for the whole request | Business authorization policy — `RequireRole` & co. are coarse role checks; tenant / resource policy lives in the service |
| The RFC 9457 error contract and `X-Trace-ID` / `X-Request-ID` response headers | Request-body models, business validation, domain error mapping |
| `platform_http_*`, `platform_grpc_*`, `platform_build_info`, `platform_unclassified_errors_total` with `{domain, service, environment}` injected centrally | Service-specific (Tier 3) metric *definitions* — services declare them, this library validates them |
| The vendored Platform Observability Registry and its Python enforcement (`metrics.compliance`, `fastapicommon-metricslint`) | The registry's content and ratification — platform-gincommon and observability governance |
| OpenTelemetry setup: one delegating global `TracerProvider`, `ParentBased` sampling, OTLP/gRPC export, W3C propagation | The collector / Tempo / Prometheus / Loki infrastructure (the `docker-compose.yml` stack is dev-only) |
| Outbound propagation: `create_platform_http_client()` (httpx) and `ContextClientInterceptor` (gRPC) | Retries, circuit breaking, rate limiting (gateway / mesh) |
| Structured logging in gincommon's Zap layout, with sanitisation and redaction | Database access, pooling, RLS (platform-pgcommon-py); messaging (platform-eventcommon) |

Consuming services keep their own app factory, routers and business logic and import only the public names of each subpackage (`__all__`) — never underscore modules.

**Tracing guarantee:** with tracing enabled (the default), every incoming request starts one server span named `METHOD route` (e.g. `GET /items/:item_id`), or continues the caller's trace when a valid `traceparent` is present. End-to-end traces hold as long as outbound calls use `create_platform_http_client()` (HTTP) or `instrument_grpc_client()` (gRPC).

---

## Why this library exists

Every backend service needs the same cross-cutting transport concerns — request correlation, distributed tracing, structured logs, Prometheus metrics, gateway-identity extraction, error bodies and access control. Centralising them in one library, and keeping it in parity with the Go one, means:

- **One log schema, across languages.** Every Python service emits the same `http_request` / `grpc_request` entries as Go services (same keys — `msg`, `level`, `ts`, `caller` — same fields, `duration_ms` as float milliseconds, info / warn / error by outcome), so one Loki query covers the platform.
- **One metric vocabulary.** `platform_http_requests_total{method, route, status_class, error_class}` means the same thing in a FastAPI service as in a Gin one, so dashboards and SLO burn-rate alerts aggregate across the whole platform. Label cardinality is bounded once (route templates, `OTHER` methods, bounded gRPC methods).
- **One trust boundary.** Header validation, repeated-header ambiguity, role caps, trusted proxies and the `claimed_*` vs verified-identity split in logs are implemented and tested once, with gincommon's rules.
- **One error contract.** Every error is the same RFC 9457 problem a Go service would send — same types, titles, codes, `traceId` order and validation messages — so clients branch on one shape (platform-interop-tests checks it on every PR).
- **One governance gate.** The registry is the contract: a service cannot emit an unregistered shared metric, a prohibited label (`tenant_id`, `user_id`, ...) or a legacy name without the compliance check failing its CI.

This is enforced structurally: the collectors are *built from* registry entries, the vendored registry cannot drift from gincommon's (`gincommon-parity.sh` in CI), and `make ci` (lint, mypy `--strict`, the observability gate, ≥ 90% coverage) runs on every PR.

---

## API overview

Fourteen subpackages and one CLI. Source of truth: the docstrings (`make docs-serve`) and each subpackage's `__all__`, which is the stable surface covered by SemVer ([VERSIONING.md](VERSIONING.md)); underscore names, `examples/` and `tests/` are not. Full signatures: [LLD §5.3](docs/lld/platform-fastapicommon-lld.md#53-symbol-catalogue).

**Default HTTP stack** (`create_platform_app`): `TracingMiddleware → ContextMiddleware → LoggingMiddleware → UnhandledErrorMiddleware → PrometheusMiddleware → TimeoutMiddleware → routing`, plus `/health`, `/live`, `/ready`, `/metrics` and the RFC 9457 exception handlers. Authentication and roles are **per-route dependencies**, not middleware. **gRPC:** `ContextServerInterceptor` (threaded server) / `AsyncContextServerInterceptor` (`grpc.aio`) do the span, context, metrics, `grpc_request` log and exception sanitizing in one interceptor.

### `fastapicommon.bootstrap` / `fastapicommon.config` — the app

| Group | Symbols | Purpose |
|---|---|---|
| App factory | `create_platform_app(app_name=None, settings=None, **fastapi_kwargs)` | Initialises metrics, logging and tracing; installs the stack, health router and exception handlers; your `lifespan`, `on_startup` / `on_shutdown` and `@app.on_event` handlers still run |
| Configuration | `PlatformSettings` (`app_name`, `environment`, `observability_domain`, `build_version`, `log_level`, `request_timeout_seconds`, `trusted_proxies`, `enable_tracing`, `enable_metrics`, `otlp_endpoint`, `problem_type_base`) | Read from the environment / `.env`; a missing or invalid identity raises `ValueError` at startup |

### `fastapicommon.context` / `fastapicommon.auth` / `fastapicommon.rbac` — identity

| Group | Symbols | Purpose |
|---|---|---|
| Request context | `RequestContext`, `CurrentUser`, `get_request_context()`, `get_request_context_or_none()`, `get_request_id`, `get_correlation_id`, `get_tenant_id`, `get_user_id`, `get_roles`, `get_trace_id`, `get_client_ip`, `request_context_scope`, `bind_request_context`, `reset_request_context` | Validated identity and correlation ids anywhere in the request (contextvars) |
| Authentication | `require_authenticated_user` (401 + audit entry), `get_current_user` | Per-route dependencies |
| Authorization | `RequireRole(role)`, `RequireAnyRole(*roles)`, `RequireAllRoles(*roles)` | Per-route role checks (403 + `authz: access denied`) |
| Validation | `normalize_header_value`, `normalize_request_id`, `parse_roles`, header constants and `MAX_*` limits; `fastapicommon.auth.validation` also has `resolve_identity`, `parse_roles_strict`, `InvalidRolesError`, `MAX_ROLES_HEADER_LEN`, `MAX_ROLES` (not re-exported from `fastapicommon.auth`) | gincommon's validation rules, for both transports |

### `fastapicommon.exceptions` / `fastapicommon.problem` — RFC 9457 Problem Details

| Group | Symbols | Purpose |
|---|---|---|
| Errors to raise | `PlatformException` (`BusinessException`), `ValidationException`, `UnauthorizedException`, `ForbiddenException`, `NotFoundException`, `ConflictException`, `BusinessRuleViolationException`, `RateLimitException`, `UnsupportedOperationException`, `ServiceUnavailableException` | What handlers raise; rendered as problems |
| Domain types | `problem_type(domain, name, title, status, code)`, `ProblemType`, the 15 `TYPE_*` platform types, `type_for_status`, `type_uri` | `{base}/{domain}/{name}` URIs — same registry as gincommon's |
| Rendering | `register_exception_handlers`, `problem_response`, `UnhandledErrorMiddleware`, `build_problem`, `Problem`, `FieldError` | Every error is `application/problem+json`; a 5xx never carries detail |
| Correlation | `resolve_trace_id`, `trace_id_from_traceparent`, `sanitized_request_id`, `new_trace_id` | `traceparent` → `x-request-id` → span → generated (platform-pgcommon-py's order) |
| Clients | `problem_from_response(resp)`, `parse_problem(body)`, `is_problem_content_type` | Parse problems; branch on `status` / `type` / `errorCode`, never on text |

### `fastapicommon.grpc` — gRPC services (`grpc` extra)

| Group | Symbols | Purpose |
|---|---|---|
| Servers | `ContextServerInterceptor(settings=None)`, `AsyncContextServerInterceptor(settings=None)` | Span, validated context, `platform_grpc_*` metrics, `grpc_request`, exception sanitizing; `settings` initialises metrics for a gRPC-only service |
| Client | `ContextClientInterceptor()` | Outbound identity and ids on unary calls |

### `fastapicommon.telemetry` / `fastapicommon.logging` / `fastapicommon.propagation`

| Group | Symbols | Purpose |
|---|---|---|
| Tracing setup | `TracingConfig`, `ensure_tracing`, `init_tracing`, `init_tracing_from_env`, `flush_tracing`, `shutdown_tracing`, `setup_tracing` | `create_platform_app` does it; use these for gRPC-only services or apps built by hand |
| Tracing outside handlers | `new_tracer(scope)`, `noop_tracer()`, `span_trace_id()`, `record_span_error(exc, description)`, `inject_trace_context(headers)`, `extract_trace_context(carrier)` | Child spans, trace ids for logs, propagation without a request |
| Logging | `configure_logging(level=None, environment=None)`, `get_logger(name)` | gincommon's Zap layout; `LOG_LEVEL`, `LOG_SAMPLING`, `LOG_FILE` |
| Outbound HTTP | `create_platform_http_client(**httpx_kwargs)` | `httpx.AsyncClient` stamping identity, ids and `traceparent` |

### `fastapicommon.metrics` and `fastapicommon-metricslint` — observability governance

| Symbol / command | Purpose |
|---|---|
| `PrometheusMiddleware`, `metrics_router`, `init_metrics`, `init_metrics_from_settings`, `metrics_registry()`, `PlatformMetrics` | The library's own `platform_*` metrics and `/metrics` |
| `new_shared_counter / gauge / histogram(name)` | Tier 1/2 metrics from their registry entry |
| `new_counter / gauge / histogram(name, documentation, labelnames)` | Tier 3 (`<domain>_<service>_*`), validated at construction |
| `register_labeled(collector)` | Identity labels on a third-party collector |
| `metrics.compliance.validate(registry)` | The standard's full rule set over a scrape (Go `obsregistry.Validate`) |
| `fastapicommon-metricslint check [--url U \| files] [--strict]` · `refs <paths>` · `registry` · `inventory` | Validate a scrape; find legacy / unregistered names in dashboards and rules; self-check; render the inventory |

Exit codes: `0` clean, `1` findings, `2` usage or I/O error.

---

## Input validation

Every rejection the library produces is an RFC 9457 problem (`application/problem+json`), with `X-Trace-ID` / `X-Request-ID` response headers set. Internal error detail is never sent: a 5xx `internal-error` has no `detail`, and the exception's message goes nowhere — not to the client, a log or a span. Full contract: [docs/problem-details.md](docs/problem-details.md).

```json
{
  "type": "https://errors.company.com/platform/unauthorized",
  "title": "Unauthorized",
  "status": 401,
  "detail": "Ambiguous x-user-id header.",
  "instance": "/tenders/7",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errorCode": "UNAUTHORIZED",
  "timestamp": "2026-10-16T10:45:22.000Z"
}
```

### Notable validation rules

| Rule | Enforcement |
|---|---|
| `x-user-id` / `x-tenant-id` | Non-empty after trim, ≤ 256 bytes, no ASCII control characters / DEL / U+2028 / U+2029 → else the field is absent; a route with `Depends(require_authenticated_user)` answers `401` `Missing or invalid authentication headers.` with a warn `auth: request rejected` |
| Repeated identity headers are ambiguous | More than one `x-user-id` or `x-tenant-id` value (HTTP header or gRPC metadata) → the **whole** identity is absent; authenticated routes answer `401` `Ambiguous x-user-id header.` / `Ambiguous x-tenant-id header.` — a gateway that appends rather than replaces can never let a client value win |
| Roles are bounded and validated | Repeated `x-tenant-roles` values are joined; tokens must match `[a-zA-Z0-9_-]+` (≤ 64 bytes; invalid tokens dropped), deduplicated; a header over 4096 bytes or with more than 64 roles → identity absent, `401` `Invalid tenant roles.` |
| `x-request-id` / `x-correlation-id` | `[A-Za-z0-9._-]`, ≤ 128 bytes; else a new UUID (request id) or the request id (correlation id) is used consistently for logs, spans, problems and propagation |
| The client IP cannot be spoofed | `X-Forwarded-For` / `X-Real-IP` are believed only when the socket peer is in `TRUSTED_PROXIES` (Gin's `SetTrustedProxies` algorithm); otherwise the client IP is the peer |
| Unverified identity is never logged as verified | `tenant_id` / `user_id` in `http_request` come only from the validated context; client-supplied values on anonymous or rejected requests appear as `claimed_tenant_id` / `claimed_user_id` |
| Metric labels cannot explode | HTTP `route` is the Gin-syntax template or `unknown` (never the raw path); non-standard methods → `OTHER`; gRPC methods outside `/package.Service/Method` or over 256 bytes → `unknown` |
| Observability identity fails fast | Missing domain, or a domain / service / environment the registry rejects (`production` and `development` included) → `ValueError` at startup |
| Tier 3 metric names are validated at construction | `<domain>_<service>_` prefix, `_total` counters, `_seconds` histograms, no prohibited or centrally injected labels |
| Legacy exception subclasses are rejected | A `PlatformException` subclass that sets `status_code` / `code` / `error_code` class attributes raises `TypeError` at definition (only `problem_type` selects the response) |

---

## Architecture

A framework-free core under FastAPI / Starlette / gRPC / OpenTelemetry adapters and one composition function; dependencies point inward. Full flow diagrams, invariant tables and the threat model are in **[ARCHITECTURE.md](ARCHITECTURE.md)**; Mermaid sources live in **[`docs/architecture/`](docs/architecture/README.md)**; the low-level contract is the **[LLD](docs/lld/platform-fastapicommon-lld.md)**.

```
platform-fastapicommon/
├── src/fastapicommon/                  # the package — public API is each subpackage's __all__
│   ├── bootstrap/                      # create_platform_app(): the one-call composition
│   ├── config/                         # PlatformSettings (pydantic-settings)
│   ├── context/                        # CurrentUser, RequestContext, contextvars accessors — no framework deps
│   ├── auth/                           # header constants, validation (resolve_identity, role caps), require_authenticated_user
│   ├── rbac/                           # RequireRole / RequireAnyRole / RequireAllRoles
│   ├── middleware/                     # Tracing, Context (trusted proxies), Logging, Timeout — pure ASGI
│   ├── exceptions/                     # PlatformException hierarchy, RFC 9457 handlers, UnhandledErrorMiddleware
│   ├── problem/                        # RFC 9457 model, type registry, builder, trace-id resolution, client parsing
│   ├── logging/                        # Zap-layout structlog config, sanitisation, audit entries
│   ├── metrics/                        # registry.json (VENDORED from gincommon), platform_* collectors, Prometheus middleware,
│   │                                   #   Tier 1/2/3 constructors, compliance validator, metricslint CLI
│   ├── telemetry/                      # delegating TracerProvider, TracingConfig, tracer helpers
│   ├── grpc/                           # Context(Async)ServerInterceptor, ContextClientInterceptor (grpc extra)
│   ├── health/                         # /health, /live, /ready
│   ├── propagation/                    # create_platform_http_client (httpx)
│   └── _errors.py · _scope.py          # internal: exception types / frames, per-request outcome flags
├── examples/
│   ├── fastapi_service/                # reference HTTP service (the image CI smoke-tests)
│   └── grpc_service/                   # reference gRPC service (raw-bytes generic handler)
├── docs/
│   ├── lld/                            # low-level design
│   ├── architecture/                   # Mermaid sources embedded in ARCHITECTURE.md
│   ├── observability/                  # the standard as implemented, metric-registry.md (generated), migration, runbooks
│   ├── adr/                            # ADR-001 logging · 002 tracing · 003 metrics · 004 context propagation
│   └── problem-details.md · MigrationGuide.md
├── docker/                             # local otel-collector, Tempo, Prometheus (+ vendored rules), Loki, Promtail, Grafana config
├── .github/                            # workflows (ported from gincommon) + scripts
├── .claude/                            # Claude Code context: CLAUDE.md + reference set, settings.json
├── .pre-commit-config.yaml             # ruff, black, mypy, pytest, whitespace
└── tests/                              # unit/ (mirrors src/), integration/, e2e/
```

### Dependency rules

| Component | May depend on |
|---|---|
| `context`, `config`, `problem`, `health`, `_scope` | Nothing internal |
| `auth` | `context`, `exceptions`, `logging` |
| `metrics` | `_scope`, `config`, `logging` |
| `middleware` | `auth`, `context`, `exceptions`, `logging`, `metrics`, `problem`, `_errors`, `_scope` |
| `exceptions` | `context`, `logging`, `problem`, `_errors`, `_scope` (+ `metrics`, `middleware` lazily) |
| `grpc` | `auth`, `config`, `context`, `logging`, `metrics`, `_errors` |
| `bootstrap` | The composition layer; the only module `fastapicommon/__init__.py` imports, so `import fastapicommon` never imports `grpc` |

Computed from the imports; the full graph is in [ARCHITECTURE.md](ARCHITECTURE.md#module-dependency-graph).

### Runtime components

| Concern | Technology | Notes |
|---|---|---|
| **HTTP framework** | FastAPI `≥ 0.115.10` / Starlette `≥ 0.46` (tested 0.140 / 1.3) | Pure-ASGI middleware; `TRUSTED_PROXIES` for services behind a proxy |
| **gRPC** | `grpcio ≥ 1.62` (`grpc` extra; tested 1.83) | Threaded and `grpc.aio` interceptors of the library's own (no OpenTelemetry gRPC instrumentation on the server) |
| **Metrics** | `prometheus-client ≥ 0.20` | `REGISTRY` unless an isolated registry is passed; `_created` timestamp samples dropped (Go emits none) |
| **Tracing** | OpenTelemetry `≥ 1.28` (tested 1.44), OTLP/gRPC exporter | One delegating global provider (inner SDK provider swapped); in-process fallback when the exporter cannot be created |
| **Logging** | `structlog ≥ 24.1` through the stdlib root logger | Zap's JSON layout on stderr, console layout in dev / local; sampling off unless `LOG_SAMPLING=true` |
| **Registry** | Vendored `registry.json` | 115 entries: 6 `approved`, 52 `proposed`, 57 `removed` (legacy names kept so the lint can name their replacement) |

### Sibling platform libraries

| Library | Relationship |
|---|---|
| `platform-gincommon` (Go) | Source of truth for the registry, reference rules, problem-type registry and the log / trace contracts; this library vendors and mirrors them |
| `platform-pgcommon-py` | Same RFC 9457 `traceId` order and vectors; traces through the global provider this library installs |
| `platform-eventcommon` | Python messaging; its `platform_messages_*` metrics are entries of the same registry |
| `platform-interop-tests` | Cross-language suite (its `python-probe` depends on this library), run by CI's `interop` job |

---

## Integrating into a consuming service

### 1. Prerequisites

```bash
pip install "platform-fastapicommon==0.2.0"          # or [grpc] for the gRPC interceptors
# uv: uv add "platform-fastapicommon[grpc]==0.2.0"
```

Python 3.11+. Pin an exact version — never unpinned in production, especially while `0.y.z` ([VERSIONING.md](VERSIONING.md#consume-a-release)). The PyPI distribution name is `platform-fastapicommon`; the import name is `fastapicommon` (`fastapicommon` alone was rejected by PyPI as too similar to an unrelated package). Requests must arrive through the trusted API gateway, which authenticates the caller and injects `x-user-id` / `x-tenant-id` / `x-tenant-roles`; the service must not be reachable around the gateway.

### 2. HTTP service

```text
Incoming request
  → TracingMiddleware            (outermost — the OTel server span, route label)
  → ContextMiddleware            (validated ids + identity → RequestContext; X-Trace-ID / X-Request-ID)
  → LoggingMiddleware            (http_request after the response)
  → UnhandledErrorMiddleware     (unclassified exception → 500 problem; never re-raised to the server)
  → PrometheusMiddleware         (platform_http_* at the end of the request)
  → TimeoutMiddleware            (innermost — 503 if no response has started in time)
  ╠═ public routes (no auth) ═══════════════════════════════════╗
  ║   GET /health, /live, /ready → health_router                 ║
  ║   GET /metrics               → metrics_router                ║
  ╚══════════════════════════════════════════════════════════════╝
  ╠═ your routes ═══════════════════════════════════════════════╗
  ║   → [per route] Depends(require_authenticated_user)  (401)    ║
  ║   → [per route] Depends(RequireRole(...))            (403)    ║
  ║   → your handler                                              ║
  ╚══════════════════════════════════════════════════════════════╝
```

```python
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from fastapicommon.auth import CurrentUser, require_authenticated_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import NotFoundException
from fastapicommon.logging import get_logger
from fastapicommon.rbac import RequireRole

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # open pools / clients here; create_platform_app flushes spans after this exits
    yield


# PlatformSettings() reads APP_NAME, APP_ENV, OBSERVABILITY_DOMAIN (required while metrics are on),
# BUILD_VERSION, OTEL_*, LOG_*, TRUSTED_PROXIES ... — see "Environment variables".
# A missing or invalid identity raises ValueError here, before the service serves traffic.
app = create_platform_app(settings=PlatformSettings(), lifespan=lifespan)

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get("/me")
async def whoami(user: CurrentUser = Depends(require_authenticated_user)) -> dict:
    # 401 problem (and an "auth: request rejected" audit entry) before this runs
    # when the identity is missing, invalid, repeated or over the role caps.
    logger.info("whoami_requested", user_id=user.user_id)
    return {"user_id": user.user_id, "tenant_id": user.tenant_id, "roles": user.roles}


@router.delete("/{widget_id}", dependencies=[Depends(RequireRole("tenant_admin"))])
async def delete_widget(widget_id: str) -> dict:
    if widget_id == "missing":
        raise NotFoundException(f"Widget {widget_id} was not found.")  # 404 problem
    return {"status": "deleted"}


app.include_router(router)
# run: uvicorn myservice.main:app --host 0.0.0.0 --port 8080
```

**Nothing is enforced globally**: unlike gincommon's `ProtectedMiddlewares`, a route without an auth or role dependency is reachable anonymously (its handler still sees the validated, possibly empty, identity). `examples/fastapi_service/main.py` is a runnable copy (`make run`).

### 3. gRPC service

```python
from concurrent import futures

import grpc

from fastapicommon.config import PlatformSettings
from fastapicommon.context import get_request_context
from fastapicommon.grpc import ContextServerInterceptor
from fastapicommon.logging import configure_logging, get_logger
from fastapicommon.telemetry import init_tracing_from_env

import my_service_pb2, my_service_pb2_grpc  # your generated stubs

logger = get_logger(__name__)


class ItemService(my_service_pb2_grpc.ItemServiceServicer):
    def GetItem(self, request, context):
        rc = get_request_context()  # bound and validated by the interceptor
        if not rc.user_id:  # no gRPC auth interceptor: check identity yourself
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing x-user-id")
        logger.info("get_item", user_id=rc.user_id, tenant_id=rc.tenant_id)
        return my_service_pb2.GetItemResponse(id=request.id)


def serve() -> None:
    settings = PlatformSettings()
    configure_logging(settings.log_level or None, settings.environment or None)
    init_tracing_from_env()  # OTEL_SERVICE_NAME → APP_NAME + OTEL_*; the interceptor starts the spans
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[ContextServerInterceptor(settings)],  # settings: initialises platform_grpc_* metrics
    )
    my_service_pb2_grpc.add_ItemServiceServicer_to_server(ItemService(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    server.wait_for_termination()
```

An **asyncio** server uses `AsyncContextServerInterceptor` — the same behaviour for `async def` handlers; sync handlers keep running in the server's migration thread pool:

```python
server = grpc.aio.server(interceptors=[AsyncContextServerInterceptor(PlatformSettings())])
```

What the interceptors do on every RPC: a server span named after the bounded method (continuing `traceparent`, with baggage), a `RequestContext` validated exactly like HTTP, `platform_grpc_*` metrics, a `grpc_request` log, and exception sanitizing — a status the handler chose (`context.abort`, `set_code`) is kept; any other exception becomes `INTERNAL "internal server error"` and an `unclassified error` entry (types and frames, never the message), where grpcio alone would send `UNKNOWN "Exception calling application: <message>"`. On `grpc.aio`, a deadline or client cancel is recorded as `DeadlineExceeded` / `Canceled`. **Not provided on gRPC:** auth / role interceptors, request timeouts (clients set deadlines), and context / spans / metrics for streaming RPCs (they get sanitizing only; bind context with `request_context_scope`). `examples/grpc_service/server.py` is a runnable variant.

### 4. Authorization

```python
from fastapi import Depends
from fastapicommon.rbac import RequireAllRoles, RequireAnyRole, RequireRole

@router.post("/tenders", dependencies=[Depends(RequireAnyRole("buyer", "admin"))])
async def create_tender(...): ...
```

The role dependencies check the **validated** roles (empty when the identity was rejected) and log `authz: access denied` before the 403. Tenant- or resource-scoped policy belongs in the service: decide with `get_request_context()` and raise `ForbiddenException("The identity is not allowed to perform this operation.")` (or a `PlatformException` with your own type).

### 5. Outbound calls

| Destination | HTTP | gRPC | Sends |
|---|---|---|---|
| Trusted internal platform service | `create_platform_http_client()` | `grpc.intercept_channel(channel, ContextClientInterceptor())` + `instrument_grpc_client()` | HTTP: `traceparent` (+ baggage), `x-request-id`, `x-correlation-id`, `x-user-id`, `x-tenant-id`, `x-tenant-roles` · gRPC: the ids and identity (no roles); `traceparent` via `instrument_grpc_client()` |
| Third parties (outside the trust boundary) | a plain `httpx` client + `inject_trace_context(headers)` if you want the trace | plain channel | only what you add |
| No request (jobs, consumers) | `inject_trace_context(headers)` | — | `traceparent` (+ baggage when enabled) |

`create_platform_http_client()` forwards identity to **any** host it is given (LLD OQ-6) — use it only for internal platform services.

```python
from fastapicommon.propagation import create_platform_http_client

async with create_platform_http_client() as client:
    resp = await client.get("http://inventory-svc/internal/stock")
```

### 6. Custom metrics

```python
from fastapicommon.metrics import new_counter, new_shared_counter

# after create_platform_app (before it they raise MetricsNotInitializedError)
processed = new_shared_counter("platform_messages_processed_total")   # Tier 1/2: name, help, labels, buckets from the registry
processed.labels(queue="iam.events", event_type="user.created").inc()

hmac_failures = new_counter(                                          # Tier 3: <domain>_<service>_*, checked at construction
    "iam_event_consumer_hmac_failures_total", "HMAC verification failures", ["reason"]
)
```

A new shared (`platform_*` or `<domain>_*`) metric is a **registry change in platform-gincommon first** (status `proposed`), then a re-vendor here (`make gincommon-parity`, `make metrics-inventory`), then `new_shared_*`. Add `fastapicommon.metrics.compliance.validate(metrics_registry())` to the service's tests:

```python
from fastapicommon.metrics import metrics_registry
from fastapicommon.metrics.compliance import validate

report = validate(metrics_registry())
assert report.ok(), "\n".join(map(str, report.errors()))
```

### 7. Tracing and logging outside handlers

| API | Use |
|---|---|
| `new_tracer(scope)` / `noop_tracer()` | Child spans in services, repositories and consumers — tracers (and `start_as_current_span` decorators) obtained before tracing is installed follow the active provider |
| `span_trace_id()` | Trace id of the current span, for log correlation |
| `record_span_error(exc, description)` | Mark the current span failed (type only — never the message) |
| `inject_trace_context(headers)` / `extract_trace_context(carrier)` | Propagate W3C context without a request |
| `get_logger(__name__)` | Entries carry `request_id`, `correlation_id`, `trace_id`, `tenant_id`, `user_id` automatically inside a request |

### 8. Returning and handling errors

Raise; the registered handlers render problems. Define domain types once:

```python
from fastapicommon.exceptions import NotFoundException, PlatformException
from fastapicommon.problem import problem_type

TENDER_NOT_FOUND = problem_type("tender", "not-found", "Tender not found", 404, "TENDER_NOT_FOUND")

raise NotFoundException("Widget 42 was not found.")                                   # platform/not-found
raise PlatformException("Tender 123 was not found.", problem_type=TENDER_NOT_FOUND)    # tender/not-found
```

Request-model validation failures are `400 platform/validation-failed` with `validationErrors` (`[{"field": "items[0].sku", "message": "..."}]`, never the rejected value); an unexpected exception is a `500 internal-error` with no detail and an `unclassified error` entry (alert on `platform_unclassified_errors_total`). Every platform 4xx/5xx from another service is a problem:

```python
from fastapicommon.problem import problem_from_response

if (p := problem_from_response(resp)) is not None:
    logger.warning("upstream_problem", status=p.status, type=p.type, error_code=p.error_code, trace_id=p.trace_id)
```

Branch on `status`, `type` and `errorCode`, never on `title` / `detail` text. Mapping table, validation and migration: [docs/problem-details.md](docs/problem-details.md).

### 9. Rate limits

The library implements no rate limiter — throttling belongs to the gateway / mesh. `PrometheusMiddleware` *observes* `429` responses (`platform_http_rate_limited_requests_total`) and logs at most one `rate limited` warn per route per second, with a `suppressed` count; gRPC counts `ResourceExhausted`.

### 10. Upgrading to 0.3.0

`0.3.0` (CHANGELOG `[Unreleased]`) aligns with platform-gincommon `v2.0.0` and is breaking. Before upgrading a service:

- [ ] **Set the observability identity.** `OBSERVABILITY_DOMAIN` (a registry domain such as `iam`) and `APP_ENV` (`local|dev|test|staging|prod`; `PlatformSettings.environment` no longer defaults to `development`).
- [ ] **Point traces at a collector.** The default endpoint is now `localhost:4317` (TLS outside dev/local, 10% sampling): set `OTEL_EXPORTER_OTLP_ENDPOINT`, or `OTEL_TRACES_EXPORTER=none`.
- [ ] **Update log queries.** Logs are on **stderr** in Zap's layout: `event` → `msg`, `timestamp` → `ts`; `grpc_request_completed` → `grpc_request` (`grpc_code`, `duration_ms`).
- [ ] **Set `TRUSTED_PROXIES`** behind a proxy / load balancer, or the recorded client IP is the proxy's.
- [ ] **Make the gateway replace identity headers**, never append them: repeated identity headers and oversized role headers now make the request unauthenticated.
- [ ] **Replace `status_code` / `code` class attributes** on `PlatformException` subclasses with `problem_type = ...` (else `TypeError` at import).
- [ ] **Use `AsyncContextServerInterceptor`** on `grpc.aio` servers.
- [ ] **Update dashboards and alerts** to the `platform_*` names (`fastapicommon-metricslint refs dashboards/ rules/`); HTTP latency now includes the body, cancelled requests count as 499 / `client_error`, and span `exception.type` is qualified (`module.Class`).

Full list: CHANGELOG `[Unreleased]`; concept mapping from Go: [docs/MigrationGuide.md](docs/MigrationGuide.md).

---

## Local development

### Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) (`brew install uv`) — `uv.lock` pins every dependency
- Docker — only for the image tests (`make test-e2e`, `make smoke-test`), the optional observability stack (`make docker-up`) and, without a local `promtool`, `make metrics-lint`'s rule tests
- A platform-gincommon checkout next to this repo for `make gincommon-parity` (`GINCOMMON_DIR`, default `../../platform-gincommon`)

### Setup

```bash
git clone https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon
cd platform-fastapicommon
make setup       # uv sync (dev group + grpc extra), .env from .env-example, logs/, pre-commit hook
make test-unit   # no Docker needed
make ci          # every gate CI runs (lint, mypy, metrics-lint, docs, build, tests + coverage)
make run         # example service on :8080
```

### Common commands

| Command | Description |
|---|---|
| `make setup` / `make install` / `make install-hooks` | Everything for a fresh clone / `uv sync` only / the pre-commit hook |
| `make format` (`fmt`) / `make format-check` (`fmt-check`) | ruff format + black + ruff `--fix` / the check-only variant CI runs |
| `make lint` / `make typecheck` | ruff / mypy `--strict` |
| `make lock-check` / `make pip-check` / `make vulncheck` | `uv.lock` in sync / consistent venv / pip-audit |
| `make metrics-lint` / `make metrics-inventory` / `make gincommon-parity` | Registry self-check, rule references, promtool, inventory drift / regenerate the inventory / parity with gincommon |
| `make test` (`test-all`) | unit + integration + e2e (requires Docker) |
| `make test-unit` / `make test-int` / `make test-e2e` | One suite |
| `make test-ci` / `make cover-check` / `make cover` / `make cover-func` | CI's test gate / + coverage gate (≥ 90%) / HTML report / per-file report |
| `make smoke-test` | Build the image, `smoke-tests.sh` + `tests/e2e` against it (CI's "Smoke tests") |
| `make scan` / `make lint-docker` | Trivy / hadolint (CI thresholds) |
| `make build` | Wheel + sdist (hash-locked backend), `twine check --strict`, installed-wheel check |
| `make api-compat` / `make release-guard TAG=vX.Y.Z` | griffe public-API diff / pre-tag version + CHANGELOG check |
| `make run` / `make show-config` | Example service (`--reload`) / print the resolved settings and tracing config |
| `make docker-up` / `make docker-down` / `make docker-reset` / `make docker-logs` / `make docker-ps` | The local observability stack |
| `make docs-check` / `make docs-serve` | Diagram sync / pydoc on :8081 |
| `make pin-base-images` | Re-pin the Dockerfile `FROM` digests |
| `make ci` | `lock-check` + `pip-check` + `format-check` + `lint` + `typecheck` + `metrics-lint` + `docs-check` + `ci-scripts-test` + `vulncheck` + `build` + `test-ci` + coverage gate |
| `make clean` / `make help` | Remove venv, caches, build output and logs / list every target |

### Running a single test

```bash
uv run pytest tests/unit/middleware/test_context_middleware.py -k ambiguous --no-cov -v
uv run pytest tests/integration/test_observability_signals.py::test_cancelled_request_is_aborted_not_a_panic --no-cov
```

`--no-cov`: a subset alone is below the 90% gate (`pyproject.toml`), which is meant for the whole suite.

### Calling the example service locally

```bash
make run
curl -i http://localhost:8080/health                       # 200 {"status":"ok"}, X-Request-ID + X-Trace-ID
curl -s http://localhost:8080/metrics | grep '^platform_build_info'

curl -i http://localhost:8080/widgets/me \
  -H "x-user-id: user-1" -H "x-tenant-id: tenant-1" -H "x-tenant-roles: tenant_admin" \
  -H "x-request-id: my-req-001"

# RequireRole("tenant_admin"): 403 problem without the role
curl -i -X DELETE http://localhost:8080/widgets/42 -H "x-user-id: user-1" -H "x-tenant-id: tenant-1"

# Repeated identity → identity absent; on an authenticated route: 401 "Ambiguous x-user-id header."
curl -i http://localhost:8080/widgets/me -H "x-user-id: alice" -H "x-user-id: mallory"

# Continue an upstream trace — X-Trace-ID echoes 4bf92f35...
curl -i http://localhost:8080/health -H "traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
```

(The example's `/widgets/me` uses `get_current_user`, so it answers anonymously; the 401 needs a route with `require_authenticated_user`, as in [§2](#2-http-service).)

### Developer tools

```bash
uv run fastapicommon-metricslint check --url http://localhost:8080/metrics   # validate a live scrape
uv run fastapicommon-metricslint refs docker/prometheus/rules                # legacy / unregistered names
make show-config                                                             # what the library resolves from the environment
make docs-serve                                                              # browse the public API (pydoc :8081)
```

---

## Testing observability locally

Every request through the stack produces a span, metric observations and a log line; the local stack shows all three.

### How the pipeline works

```
example service / your service (make run)
    │
    ├── spans ──OTLP/gRPC :4317──▶ otel-collector ──▶ Tempo (:3200)
    │
    ├── /metrics ◀──scrape── Prometheus (:9090) ── vendored recording rules + SLO burn-rate alerts
    │
    └── JSON logs ──LOG_FILE──▶ Promtail ──▶ Loki (:3100)
                                                   │
                                 Grafana (:3000) ◀─┴─ Tempo · Prometheus · Loki datasources
```

### Step 1 — Start the stack

```bash
make docker-up
```

| Container | Image | Host port(s) | Purpose |
|---|---|---|---|
| `otel-collector` | `otel/opentelemetry-collector-contrib` | `4317`, `4318` | OTLP gRPC/HTTP receiver → Tempo |
| `tempo` | `grafana/tempo:2.7.2` | `3200` | Trace store |
| `prometheus` | `prom/prometheus` | `9090` | Scrapes `host.docker.internal:8080/metrics`; loads the reference rules |
| `loki` | `grafana/loki` | `3100` | Log store |
| `promtail` | `grafana/promtail` | — | Ships `logs/app.log` (`LOG_FILE`) into Loki |
| `grafana` | `grafana/grafana` | `3000` | Dashboards / Explore (trace ↔ log correlation provisioned) |

Ports are overridable (`OTEL_COLLECTOR_GRPC_PORT`, `TEMPO_HTTP_PORT`, `PROMETHEUS_HTTP_PORT`, `LOKI_HTTP_PORT`, `GRAFANA_HTTP_PORT`); the scrape target is `PROMETHEUS_SCRAPE_TARGET`.

### Step 2 — Run the app and generate traffic

```bash
# make run sets APP_ENV=dev, OBSERVABILITY_DOMAIN=iam, LOG_FILE=logs/app.log, OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
make run
for i in $(seq 1 20); do curl -s localhost:8080/health >/dev/null; done
curl -s -X DELETE localhost:8080/widgets/missing -H "x-tenant-roles: tenant_admin" >/dev/null   # 404 problem
```

`APP_ENV=dev` gives plaintext OTLP, 100% sampling and the console log layout on stderr (`LOG_FILE` stays JSON).

### Step 3 — Inspect each signal

| Signal | Where | Query |
|---|---|---|
| Metrics | http://localhost:9090 | `sum by (route, status_class) (rate(platform_http_requests_total[1m]))`, `platform_unclassified_errors_total` |
| Traces | Grafana → Explore → Tempo | Search by service name, or paste an `X-Trace-ID` from a response |
| Logs | Grafana → Explore → Loki | `{job="platform-fastapicommon-example"} \|= "http_request"` — `trace_id` / `request_id` link a log line to its trace |
| Rules | `make metrics-lint` | `promtool check rules` + `promtool test rules` over `docker/prometheus/rules` |

### Troubleshooting observability

| Symptom | Likely cause | Fix |
|---|---|---|
| `ValueError` naming `OBSERVABILITY_DOMAIN` at startup | No `observability_domain` and no `OBSERVABILITY_DOMAIN` | Set one — a registry domain such as `iam` |
| `ValueError`: environment not in the vocabulary | `APP_ENV` / `ENVIRONMENT` unset, or `production` / `development` | Use `local\|dev\|test\|staging\|prod` |
| Exporter retry warnings / no traces in Tempo | Collector unreachable, or TLS against a plaintext collector | `make docker-up`; outside dev set `OTEL_EXPORTER_OTLP_INSECURE=true` only for plaintext collectors; `OTEL_TRACES_EXPORTER=none` to switch export off |
| Only ~10% of requests traced | Non-dev default ratio `0.1` | `OTEL_TRACES_SAMPLER_RATIO=1.0` locally |
| Traces as `service.name=unknown_service` | `init_tracing_from_env()` without `OTEL_SERVICE_NAME` / `APP_NAME` | Set one, or use `create_platform_app` / `init_tracing(TracingConfig(...))` |
| `client_ip` is the proxy's address | `TRUSTED_PROXIES` unset behind a proxy | List the proxy CIDRs |
| Authenticated requests answer 401 `Ambiguous ... header.` | The gateway appends identity headers | Make it replace them |
| No logs in Loki | `LOG_FILE` unset | `make run` sets it; otherwise `LOG_FILE=logs/app.log` |
| `metricslint check --strict` fails on `proposed` | The `platform_*` entries await governance ratification | Run without `--strict` until ratification (LLD OQ-1) |

---

## Testing

### Canonical tests (do not break)

- **`tests/unit/metrics/test_compliance.py`**, **`tests/integration/test_metrics_compliance.py`** — the library's own scrape validates against the registry with 0 errors; **`tests/unit/metrics/test_metrics_registry.py`** (`test_vendored_registry_is_identical_to_gincommon`) pins the vendored copy.
- **`tests/unit/exceptions/test_unclassified.py`**, **`tests/unit/grpc/test_grpc_errors.py`**, **`tests/unit/grpc/test_grpc_aio.py`** — no exception message reaches a client or a log, on HTTP, threaded gRPC and `grpc.aio` (unary and every streaming kind, and a context that cannot abort).
- **`tests/integration/test_observability_signals.py`** — spans, logs and metrics for success, 404, 500, a stream that fails mid-body (aborted) and a cancelled request (499, never a panic).
- **`tests/unit/middleware/test_context_middleware.py`**, **`tests/unit/grpc/test_grpc.py`** — identity validation on both transports: ambiguity, role caps, ids, trusted-proxy client IP (untrusted, non-IP and IPv6 peers).
- **`tests/unit/middleware/test_tracing_middleware.py`** — the server span on its own: forwarding headers never believed without `ContextMiddleware`'s trusted proxies, span status per response class, an escaped exception as a `panic` span without its message.
- **`tests/integration/test_bootstrap.py`** — `create_platform_app` wiring, lifespans and `on_event` handlers, rejected identities as 401 problems with their audit entries.
- **`tests/unit/telemetry/test_tracing_contract.py`** — the delegating provider: captured tracers and decorators follow swaps, flush keeps the provider, a service-installed provider is never replaced.
- **`tests/unit/middleware/test_timeout_cancellation.py`** — the server's own cancellation propagates through a timed-out request.
- **`tests/unit/problem`** — the RFC 9457 model and the platform types against gincommon's `registry.md`.

Full list with the invariants each pins: [LLD §14.5](docs/lld/platform-fastapicommon-lld.md#145-canonical-test-cases-do-not-break).

### Coverage

`make cover-check` (CI: `coverage-gate.sh`, `COVERAGE_THRESHOLD`) fails below **90%** (`pyproject.toml` `fail_under`) over unit + integration; the current total is **100%** (lines and branches) across 423 tests (388 unit, 29 integration, 6 e2e), with no `pragma: no cover` in `src/`. Keep new code at 100%: test a reachable path, restructure an unreachable one away. `tests/e2e` runs against the built image (`--no-cov`: it measures a container, not this process). The same suites also run on Python 3.11 / 3.13 / 3.14 and with every runtime dependency at its floor (`lowest-deps.sh`).

---

## Environment variables

Read by `PlatformSettings` / the library at startup in a consuming service (`.env-example` documents the example's set):

| Variable | Default | Purpose |
|---|---|---|
| `APP_NAME` | `platform-service` | FastAPI title, `service` label, OTel `service.name` |
| `APP_ENV` / `ENVIRONMENT` | unset | `environment` label (`local\|dev\|test\|staging\|prod`, trimmed + lowercased; `APP_ENV` first; required while metrics are on). `dev` / `local` select the console log layout, debug level, plaintext OTLP and 100% sampling |
| `OBSERVABILITY_DOMAIN` | — (**required** while `ENABLE_METRICS` is true) | `domain` label; OTel `service.namespace` |
| `BUILD_VERSION` | `unknown` | `platform_build_info{version}`, OTel `service.version` |
| `LOG_LEVEL` | `debug` dev/local, else `info` | `debug\|info\|warn\|error\|critical`; invalid → default + one warning |
| `LOG_SAMPLING` | `false` | Zap sampling (100/s per message+level, then 1 in 100) |
| `LOG_FILE` | — | Also write JSON logs to this file (same level and sampling) |
| `REQUEST_TIMEOUT_SECONDS` | `30.0` | `TimeoutMiddleware`: 503 if no response has started in time |
| `TRUSTED_PROXIES` | `""` | Comma-separated IPs / CIDRs whose `X-Forwarded-For` / `X-Real-IP` are believed (empty: the socket peer) |
| `ENABLE_TRACING` / `ENABLE_METRICS` | `true` | Stack switches |
| `PROBLEM_TYPE_BASE` | `https://errors.company.com` | Problem `type` URI base; the same for every service of an organisation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `localhost:4317` | OTLP/gRPC collector; `none` / `off` / `disabled` turns export off (spans stay in-process) |
| `OTLP_ENDPOINT` | — | Overrides `OTEL_EXPORTER_OTLP_ENDPOINT` (same `none` values) |
| `OTEL_TRACES_EXPORTER` | — | Only `none` is honoured: export off (also with an explicit endpoint) |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` in dev/local, else `false` | Plaintext OTLP |
| `OTEL_TRACES_SAMPLER_RATIO` / `OTEL_TRACES_SAMPLER_ARG` | `1.0` dev/local, `0.1` otherwise | Sampling ratio `0.0`–`1.0` (`_RATIO` first; invalid or NaN → default + warning) |
| `OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED` | `false` | Re-sample remote-sampled parents at the ratio — enable on edge services |
| `OTEL_PROPAGATOR_BAGGAGE` | `false` | W3C baggage propagation |
| `OTEL_SERVICE_NAME` | — | `init_tracing_from_env()` only (then `APP_NAME`); unset → provisional `unknown_service` |
| `APP_PORT` | `8080` | Example service only |

Boolean values are case-insensitive (`true`/`1`/`yes`, `false`/`0`/`no`); anything else warns once and uses the default.

---

## Security

| Topic | Guidance |
|---|---|
| **Gateway trust boundary** | `x-user-id`, `x-tenant-id`, `x-tenant-roles` are trusted, not verified. The gateway must authenticate, strip client-supplied identity headers, and **replace** (not append) them; the service must be unreachable around the gateway |
| **Ambiguous identity** | Repeated `x-user-id` / `x-tenant-id` make the identity absent on HTTP and gRPC; roles are joined then capped (4096 bytes, 64 roles) |
| **No global auth** | Every route is anonymous unless it declares `Depends(require_authenticated_user)` or a role dependency; health and `/metrics` routes never should |
| **Authorization** | The role dependencies check only the validated roles; tenant-scoped policy is the service's |
| **Client IP** | The socket peer unless `TRUSTED_PROXIES` lists the proxy in front of the service |
| **Outbound identity** | `create_platform_http_client()` forwards identity to any host — internal platform services only (LLD OQ-6) |
| **Log injection and redaction** | The library's own entries strip control characters, cap values at 256 bytes and redact sensitive keys (`*_key`, `password`, `token`, ...); fields a service logs itself are not sanitized |
| **Error leakage** | No exception message reaches a client, a log or a span — types and code locations only; 5xx problems carry no detail; `tenantId` only from verified identity, never on a 401 |
| **Trace sampling abuse** | `ParentBased`; on edge services set `OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED=true` |
| **OTLP** | TLS unless the environment is `dev` / `local` (`OTEL_EXPORTER_OTLP_INSECURE` overrides) |
| **`/metrics`** | Served unauthenticated — restrict via network policy, an auth proxy or a dedicated port |
| **Supply chain** | Actions pinned to commit SHAs, digest-pinned base images, hash-locked dependencies and build backend, pip-audit and Trivy gates, Cosign keyless signing, SBOM + SLSA provenance, PyPI Trusted Publishing. Vulnerability reporting: [SECURITY.md](SECURITY.md) |

---

## Observability

**SLOs.** The library defines no SLO of its own; it vendors gincommon's reference recording rules and multi-window burn-rate alerts over `platform_http_*` / `platform_grpc_*` (`docker/prometheus/rules/`, with `promtool` tests), including `PlatformUnclassifiedErrors`, which consuming services adopt.

### Platform Observability Standard metrics

Per the [Enterprise Platform Observability Standard](docs/observability/README.md)'s three tiers, with `domain` / `service` / `environment` injected centrally as const labels — never declared at a call site — and `version` only on `platform_build_info`:

- **Tier 1 — `platform_*`** (emitted by this library): `platform_http_requests_total`, `platform_http_request_duration_seconds`, `platform_http_request_size_bytes`, `platform_http_response_size_bytes`, `platform_http_active_requests`, `platform_http_panics_total`, `platform_http_rate_limited_requests_total`, `platform_http_request_timeouts_total`; `platform_grpc_server_handled_total`, `platform_grpc_server_handling_seconds`, `platform_grpc_server_request_size_bytes`, `platform_grpc_server_response_size_bytes`, `platform_grpc_server_inflight_requests`, `platform_grpc_panics_total`, `platform_grpc_rate_limited_requests_total`; `platform_unclassified_errors_total{transport}` (both series start at 0); `platform_build_info`. Messaging and DB metrics come from the sibling libraries under the same registry.
- **Tier 2 — `<domain>_*`** (shared within a domain, e.g. the eight `iam_*` entries): registry entries, built with `new_shared_*`.
- **Tier 3 — `<domain>_<service>_*`** (one service only): free to add, validated at construction and by the compliance check.

Label values are bounded: `method` (standard methods, else `OTHER`), `route` (Gin-syntax template — `/items/{item_id}` → `/items/:item_id` — or `unknown`), `status_class` / `error_class` (2xx / success, 4xx / client_error, 5xx / server_error, as in gincommon), `grpc_code` (Go names, e.g. `NotFound`), `grpc_method` (`/package.Service/Method` or `unknown`). HTTP latency covers the whole request (body included) and the response size is the bytes sent; an exception after the response started, or a cancelled request (499), is never a panic. Full table: [ARCHITECTURE.md](ARCHITECTURE.md#registered-prometheus-metrics); inventory: [metric-registry.md](docs/observability/metric-registry.md); runbooks: [runbooks.md](docs/observability/runbooks.md).

### Tracing and logs

Server spans per request / RPC with W3C propagation: HTTP spans named `METHOD route` with gincommon's attributes, gRPC spans named after the bounded method with `rpc.response.status_code`, exceptions recorded by **type only**, and resources carrying `service.name`, `service.version`, `service.namespace` (domain) and `deployment.environment.name`. Logs are gincommon's Zap layout on stderr — `http_request` / `grpc_request` (info 2xx/OK, warn 4xx/client codes, error 5xx/server codes, debug when aborted), `auth: request rejected`, `authz: access denied`, `problem response`, `unclassified error`, `rate limited`, `otel_tracing_initialized` — and every entry written inside a request carries `request_id`, `correlation_id` and `trace_id`; identity fields are verified-only, with `claimed_*` for unverified values. Full catalogue: [LLD §7.3](docs/lld/platform-fastapicommon-lld.md#73-log-event-catalogue).

---

## Versioning & releases

### Nothing is deployed

This is a Python package. A release is a **git tag** whose wheel and sdist are published to PyPI; consumers pin it. There is no deployment or deploy gate. The example image exists to smoke-test the stack in CI and is never run in production.

| Artefact | Where | Purpose |
|---|---|---|
| Wheel + sdist (built once, reproducibly) | PyPI (Trusted Publishing, PEP 740 attestations) and the GitHub Release | What consuming services pin |
| `SHA256SUMS` + `SHA256SUMS.sigstore.json` (Cosign-signed) | GitHub Release | Verify the files |
| Example image (`X.Y.Z`, forward-only `X.Y` / `X`, no `latest`) | GHCR, Cosign-signed, SBOM + SLSA provenance | CI smoke target only |

### Versioning

SemVer over each subpackage's `__all__` ([VERSIONING.md](VERSIONING.md)); while `0.y.z`, a MINOR may break. History: [CHANGELOG.md](CHANGELOG.md).

| Version | Status | `pip install` | Notes |
|---|---|---|---|
| **0.3.0** | Next (`[Unreleased]`) | — | platform-gincommon `v2.0.0` alignment; breaking — follow [Upgrading to 0.3.0](#10-upgrading-to-030) |
| **v0.2.0** | Current | `platform-fastapicommon==0.2.0` | Predates release signing and `SHA256SUMS` |
| v0.1.0 – v0.1.1 | Superseded | `==0.1.1` | No further releases |

### Cutting a release

1. Bump `version` in `pyproject.toml` and rename `## [Unreleased]` to `## [X.Y.Z] - <date>` in `CHANGELOG.md` (`make release-guard TAG=vX.Y.Z` checks both).
2. Merge to `main` — the tag must be reachable from `origin/main`.
3. Tag `vX.Y.Z` (or `vX.Y.Z-rc.N`) and push; `release.yml` does the rest.

`release.yml` runs the same job graph as platform-gincommon's (via platform-eventcommon's Python port):

1. **Verify.** Tag, `pyproject.toml` version, dated CHANGELOG section, dispatch ref, which floating image tags may move.
2. **Gates.** The `ci.yml` gates at the tag — tests (incl. lowest-deps and the Python matrix), quality, Hadolint, Trivy, smoke, interop — plus a **blocking** `api-compat`.
3. **Dist.** Wheel + sdist built once, reproducibly, with `SHA256SUMS`.
4. **Push.** The example image, only when its content fingerprint matches the image Trivy and smoke judged (checked before and after the push); Cosign signature, SBOM, SLSA provenance.
5. **PyPI, then GitHub Release.** The same files go to PyPI (Trusted Publishing) and the GitHub Release (CHANGELOG notes, Cosign-signed `SHA256SUMS`). PyPI comes after the image push because a PyPI version can never be re-uploaded.

**Verifying a release:** [VERSIONING.md § Verifying a release](VERSIONING.md#verifying-a-release) (cosign ≥ 2.4: the signed `SHA256SUMS`, the files against it, and the image). One-time PyPI setup: [§ Publishing to PyPI](VERSIONING.md#publishing-to-pypi).

---

## CI

Six workflow files, ported from platform-gincommon; shell logic lives in `.github/scripts/` (shellcheck-clean); every third-party action is pinned to a full commit SHA.

- **`ci.yml`** — orchestrator (push / PR to `main`): `detect changes` (documentation-only changes skip the build and test jobs, which still report) → `validate-test` ∥ `validate-quality` ∥ `build-image` (Hadolint, `.dockerignore` check, cached amd64 build) ∥ `api-compat` (informational) → `trivy` (CRITICAL / HIGH / UNKNOWN with a fix fails; SARIF; CycloneDX SBOM) and `smoke` (image ≤ 170 MB uncompressed, startup failure on an invalid identity, `/health`, `platform_*` metrics, RFC 9457 not-found, non-root, pip absent, then `tests/e2e` against the image) → `interop` (platform-interop-tests) → `pr-summary` (one updated PR comment) → `push` (main only: image to GHCR, Cosign sign + verify).
- **`validate-test.yml`** (reusable) — unit + integration with coverage on 3.12, the coverage gate (**≥ 90%**), the same suites on 3.11 / 3.13 / 3.14, and `lowest direct dependencies` (every runtime dependency at its floor).
- **`validate-quality.yml`** (reusable) — lock check, `uv pip check`, HTML-entity guard on workflows, ruff format + black, ruff, mypy `--strict`, `make metrics-lint`, **gincommon parity** (at `GINCOMMON_REF`), diagram sync, CI script tests, pip-audit, digest-pinned `FROM` lines, `make build`.
- **`docs.yml`** — diagram sync on documentation-only changes.
- **`changelog-check.yml`** — PRs touching `src/`, `pyproject.toml`, `uv.lock`, `build-constraints.txt` or the `Dockerfile` must update `CHANGELOG.md` (Dependabot exempt).
- **`release.yml`** (`v*` tags or manual) — see [Cutting a release](#cutting-a-release).

**Required repository secret:** `CI_REPO_READ_TOKEN` — read access to platform-gincommon for the parity check (required on `main` and for releases).

---

## Docker

The only image is the **example service** (`examples/fastapi_service`).

### Building the example image

```bash
make docker-build
docker run --rm -p 8080:8080 -e APP_ENV=prod -e OBSERVABILITY_DOMAIN=iam -e OTEL_TRACES_EXPORTER=none \
  platform-fastapicommon-example:local
```

Two-stage `Dockerfile`: digest-pinned `ghcr.io/astral-sh/uv:0.13.1` + `python:3.14-slim` builder installing exactly the locked, hash-checked requirements and the wheel; digest-pinned `python:3.14-slim` runtime without pip, ensurepip, IDLE, Tk or pydoc data, running as numeric `USER 10001:10001`, `EXPOSE 8080`, a Python `HEALTHCHECK` on `/health`, reproducible layers (`SOURCE_DATE_EPOCH`). Re-pin with `make pin-base-images`.

### Health

| Endpoint | Returns | Checks |
|---|---|---|
| `GET /health`, `GET /live` | `200 {"status":"ok"}` | Pure liveness — never inspects a dependency; no auth |
| `GET /ready` | `200 {"status":"ok"}` or `503 {"status":"not_ready"}` | The service's readiness checks (`create_health_router(readiness_checks=[...])`) |

### Minimum required environment variables

```bash
OBSERVABILITY_DOMAIN=iam
APP_ENV=prod                                         # local|dev|test|staging|prod
BUILD_VERSION=v0.3.0
OTEL_EXPORTER_OTLP_ENDPOINT=otel-collector.observability:4317
TRUSTED_PROXIES=10.0.0.0/8                           # behind a proxy / load balancer
```

---

## Platform dependencies

### 1. Consuming services (services → this library)

No production Python service depends on the library yet in this workspace. The only consumer is `platform-interop-tests/python-probe` (editable path dependency), which exercises the HTTP surface against gincommon's in the cross-language suite. Services adopting it start at `0.3.0` ([§10](#10-upgrading-to-030)).

### 2. Sibling libraries

| Library | Direction | Contract |
|---|---|---|
| `platform-gincommon` | registry ←, error contract ← | Registry and reference rules vendored byte for byte (CI parity); same problem types, codes and messages; same log and trace contract |
| `platform-pgcommon-py` | contract ↔ | Same RFC 9457 `traceId` order and vectors; uses the global tracer provider |
| `platform-eventcommon` | registry ↔ | Messaging metrics under the same registry |
| `platform-interop-tests` | CI → | Cross-language suite run by the `interop` job (`error_shape`: a Go and a Python 401 are the same problem) |

### 3. Infrastructure dependencies

| System | Role | On failure |
|---|---|---|
| **OTel collector** (OTLP/gRPC) | Span export | Exporter creation failure → warning + in-process provider (valid trace ids, nothing exported); runtime export errors are logged; requests unaffected; shutdown flush ≤ 5 s off the event loop |
| **Prometheus** | Scrapes `/metrics` | No effect on request handling; a failing collector costs only its own series |
| **Log pipeline** | Ships stderr / `LOG_FILE` | No effect on request handling |
| **API gateway** | Authenticates and replaces identity headers | Without it, authenticated routes return `401` — never trust a request that bypassed it |

---

## Out of scope

| Concern | Where it lives |
|---|---|
| JWT validation, OAuth, API keys, MFA | API gateway / Keycloak |
| Business authorization policy | Each service (the role dependencies are coarse checks) |
| Database access, pooling, RLS, migrations | `platform-pgcommon-py` |
| Messaging, outbox, schema registry | `platform-eventcommon`, `platform-schemagov` |
| Rate limiting, circuit breaking, retries | API gateway / service mesh |
| Request body models and business validation | Your service (FastAPI / pydantic) |
| gRPC auth / role interceptors, gRPC request timeouts | Not implemented — handlers check identity; clients set deadlines (LLD OQ-12) |
| Collector, Tempo, Prometheus, Loki, Grafana in production | Platform observability infrastructure |
| Metric governance decisions | platform-gincommon + observability governance |

Keeping scope to **transport, observability and identity context** keeps the library safe to upgrade across every service without business-logic risk.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, extending the library (new middleware, new metrics, new problem types) and the PR checklist.

| Document | Description |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layer model, module graph, public API, every request / error / tracing / metrics flow (16 diagrams), concurrency, failure domains, invariants, threat model, conformance checklist |
| [`docs/lld/platform-fastapicommon-lld.md`](docs/lld/platform-fastapicommon-lld.md) | Low-level design — API contract, limits, algorithms, error taxonomy, open questions |
| [`docs/architecture/`](docs/architecture/README.md) | Mermaid sources |
| [`docs/problem-details.md`](docs/problem-details.md) | The RFC 9457 contract as implemented |
| [`docs/observability/README.md`](docs/observability/README.md) | The observability standard as implemented, CI enforcement |
| [`docs/observability/metric-registry.md`](docs/observability/metric-registry.md) | Generated registry inventory (`make metrics-inventory`) |
| [`docs/observability/migration.md`](docs/observability/migration.md) · [`runbooks.md`](docs/observability/runbooks.md) | Legacy → `platform_*` mapping · alert runbooks |
| [`docs/adr/`](docs/adr/) · [`docs/MigrationGuide.md`](docs/MigrationGuide.md) | Decision records · Go → Python concept mapping |
| [VERSIONING.md](VERSIONING.md) | SemVer rules, supported releases, release process, verifying a release |
| [CHANGELOG.md](CHANGELOG.md) | Per-version changes and migration steps |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting and trust model |
| [`.claude/CLAUDE.md`](.claude/CLAUDE.md) | Context for Claude Code sessions: critical rules, commands, and the `.claude/` reference set |

---

## License / ownership

Owned by BCBP Solutions FZC LLC (platform team). Licensed under Apache-2.0 (`pyproject.toml`) and published publicly on PyPI; the repository itself is private ([VERSIONING.md § Publishing to PyPI](VERSIONING.md#publishing-to-pypi)).
