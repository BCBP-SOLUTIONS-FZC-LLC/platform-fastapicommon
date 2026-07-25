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
| **Context** | Builds `RequestContext{request_id, correlation_id, user: CurrentUser}` and binds it via `contextvars` | Read it via `fastapicommon.context.get_request_context()` / `fastapicommon.auth.get_current_user`; propagate outbound via `create_platform_http_client()` |

Your service only implements **business logic** on top of these layers. Log format, span
setup, error response shape, and metric names are standardised here.

> **Tracing guarantee:** when `settings.enable_tracing` is true (the default), every
> incoming request creates a root OTel span, or continues an existing distributed trace if
> a `traceparent` header is provided, via `FastAPIInstrumentor`. Spans are only **exported**
> when `PlatformSettings.otlp_endpoint` (env var `OTLP_ENDPOINT`) is set — otherwise they're
> created (valid trace IDs) but never leave the process. See
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

- A consistent structured log line with the same enrichment fields
  (`request_id`, `correlation_id`, `tenant_id`, `user_id`)
- OTel spans with the same instrumentation (`FastAPIInstrumentor`)
- A `RequestContext`/`CurrentUser` available in every handler via one dependency
- The same error JSON shape (`{"code": ..., "message": ...}`) across all services

---

## Features

| Capability | HTTP | gRPC |
|------------|------|------|
| **Request/correlation ID** | `ContextMiddleware` — reads `x-request-id`/`x-correlation-id` or generates a UUID; echoes both back | `ContextServerInterceptor` — reads from metadata or generates a UUID |
| **Tracing** | `FastAPIInstrumentor` via `setup_tracing()`; OTel HTTP server spans | `instrument_grpc_server()` / `instrument_grpc_client()` (separate opt-in call) |
| **Metrics** | `PrometheusMiddleware` — `http_requests_total`, `http_request_duration_seconds` | `record_grpc_request()` via `ContextServerInterceptor` — `grpc_requests_total`, `grpc_request_duration_seconds` |
| **Logging** | `fastapicommon.logging.get_logger()` — structlog JSON, enriched with context | Same `get_logger()`; `ContextServerInterceptor` logs `grpc_request_completed` after each call |
| **Auth** | `Depends(require_authenticated_user)` — `401` if `x-user-id` unset (opt-in per route) | **Not provided** — no gRPC equivalent exists; check `get_request_context().user_id` yourself |
| **Tenant context** | `RequestContext{request_id, correlation_id, user: CurrentUser}` via `get_request_context()` | Same struct, same accessor, bound by `ContextServerInterceptor` |
| **RBAC** | `RequireRole` / `RequireAnyRole` / `RequireAllRoles` as FastAPI dependencies | **Not provided** — these are FastAPI `Depends`-based; there is no gRPC interceptor equivalent |
| **Timeout** | `TimeoutMiddleware(timeout_seconds=...)` — `asyncio.wait_for` around the handler, `504` JSON on expiry | **Not provided** — set a deadline on the gRPC channel/client call yourself |
| **Health** | `health_router` — `/health`, `/live`, `/ready` (200 unless custom readiness checks fail) | — |
| **Error type** | `PlatformException` hierarchy → `{"code": ..., "message": ...}` JSON via `register_exception_handlers` | Exceptions raised in a unary-unary handler propagate to the client as-is (gRPC has no equivalent exception-handler registry) |
| **Outbound propagation** | `create_platform_http_client()` — `httpx.AsyncClient` that stamps identity/trace headers on every request | `ContextClientInterceptor` — stamps identity metadata on outbound unary-unary calls |
| **OpenTelemetry init** | `setup_tracing(app, service_name, exporter=None)` (called automatically by `create_platform_app`) | `instrument_grpc_server()` / `instrument_grpc_client()` (call explicitly) |

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
  → ContextMiddleware        (outermost — binds RequestContext for the whole request)
  → PrometheusMiddleware     (records http_requests_total / http_request_duration_seconds)
  → TimeoutMiddleware        (innermost — asyncio.wait_for around the handler)
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

`ContextMiddleware` is added *last* in `create_platform_app`, which makes it *outermost*
(see [ARCHITECTURE.md § Execution model](./ARCHITECTURE.md#execution-model) for why). Unlike
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

# PlatformSettings() reads APP_NAME, APP_ENV, OTLP_ENDPOINT, etc. from the environment —
# see "Environment variables" below.
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
        # Injects x-user-id, x-tenant-id, x-request-id, x-correlation-id (from the bound
        # RequestContext, only when set) and traceparent (via OTel propagation).
        resp = await client.get("http://inventory-svc/internal/stock")
    return resp.json()
```

### Error response shape

All `PlatformException` subclasses (and the generic-exception fallback) produce the same
JSON body:

```json
{"code": "not_found", "message": "widget 42 not found"}
```

**Unlike `platform-gincommon`'s `ErrorResponse`, this body does not include
`request_id`/`trace_id`.** Correlation IDs live in **response headers**
(`x-request-id`, `x-correlation-id`), set by `ContextMiddleware` on every response,
success or error:

```python
import httpx

resp = httpx.get("http://localhost:8080/widgets/nonexistent")
print(resp.json())                       # {"code": "not_found", "message": "..."}
print(resp.headers["x-request-id"])      # correlation ID for this call
```

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
from fastapicommon.telemetry import instrument_grpc_server

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
    instrument_grpc_server()  # opt-in; not automatic like the HTTP side

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

`platform-gincommon` tracks a request ID and an OTel trace ID as two separate,
purpose-built fields (`RequestContext.TraceID` plus a request ID helper). **This library
only has one:** `request_id` / `correlation_id` on `RequestContext`, sourced from the
`x-request-id` / `x-correlation-id` headers (or generated). OTel spans are created
independently by `FastAPIInstrumentor` and have their own trace IDs, but **those trace IDs
are not copied into `RequestContext` or into structlog's enrichment fields.**

Practically: `request_id`/`correlation_id` is what you search logs by; the OTel trace ID
(visible in Tempo/Jaeger, or via `opentelemetry.trace.get_current_span().get_span_context().trace_id`)
is what you use to find the full distributed trace. The two are **not** correlated to each
other out of the box in this library — if you need that, add a structlog processor that
reads the active span's trace ID (see `fastapicommon/logging/config.py::_add_request_context`
for the pattern to extend).

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
| **OTLP** | Insecure (no TLS) by default when `environment` is `dev`/`development`/`local`; TLS required otherwise. See [ARCHITECTURE.md § Tracing initialization](./ARCHITECTURE.md#tracing-initialization). |
| **Logging** | No sanitization — header values are copied into log fields and response headers verbatim. See [SECURITY.md](./SECURITY.md) for the full trust model. |

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
| `fastapicommon.metrics` | Prometheus HTTP/gRPC metrics + `/metrics` router |
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
| `APP_ENV` (or `ENVIRONMENT`) | `prod` | `development` | `environment` — controls OTLP insecure-mode default (`dev`/`development`/`local` → insecure) |
| `LOG_LEVEL` | `DEBUG` | `INFO` | `log_level` — passed to `configure_logging()` |
| `REQUEST_TIMEOUT_SECONDS` | `10` | `30.0` | `request_timeout_seconds` — `TimeoutMiddleware` deadline |
| `ENABLE_TRACING` | `false` | `true` | `enable_tracing` — gates `setup_tracing()` entirely |
| `ENABLE_METRICS` | `false` | `true` | `enable_metrics` — gates `PrometheusMiddleware` + `/metrics` |
| `OTLP_ENDPOINT` | `otel-collector:4317` | unset | `otlp_endpoint` — when set, spans are exported via OTLP/gRPC; when unset, spans are recorded but never exported |

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
│   ├── metrics/         # Prometheus definitions + middleware + /metrics router
│   ├── telemetry/       # OpenTelemetry setup (HTTP + gRPC)
│   ├── rbac/            # RequireRole / RequireAnyRole / RequireAllRoles
│   ├── health/          # /health, /live, /ready router
│   ├── propagation/     # create_platform_http_client
│   ├── grpc/            # server/client interceptors (requires the grpc extra)
│   └── bootstrap/       # create_platform_app() — the single entry point
├── tests/                # mirrors src/fastapicommon/ one-to-one
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

### Setup

```bash
make setup     # copies .env-example -> .env, creates logs/
make install   # create venv, install dev + grpc + examples extras, install pre-commit hooks
```

### Common commands

| Command | Description |
|---------|-------------|
| `make lint` | ruff check + mypy --strict |
| `make format` | ruff format + black (rewrites files) |
| `make format-check` | Same, but check-only — no changes; used in CI |
| `make vulncheck` | pip-audit against installed dependencies |
| `make test` | All tests (coverage gate ≥90%, enforced by `pyproject.toml`) |
| `make coverage` | Run tests, then open an HTML coverage report |
| `make build` | Build the wheel + sdist into `dist/` |
| `make run` | Run the example FastAPI service (`uvicorn --reload`) |
| `make docker-build` | Build the example service Docker image locally |
| `make docker-up` / `make docker-down` | Start/stop the local observability stack |
| `make ci` | `format-check` + `lint` + `test` + `build` — the full local pipeline |

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

- Tests live under `tests/`, mirroring `src/fastapicommon/` one-to-one, plus
  `tests/bootstrap/` for end-to-end `create_platform_app()` checks.
- **Coverage gate:** `pyproject.toml`'s `[tool.coverage.report].fail_under = 90` — enforced
  automatically by `pytest` (no separate flag needed).
- All tests run without a live server or Docker — the observability stack is for manual
  verification only.

---

## CI

GitHub Actions runs on push/PR to `main`/`develop`. Job names below match the org's
required-status-checks ruleset on `main` exactly:

1. **Validate** — a reusable workflow fanning out into two required checks: **Quality**
   (format check, ruff lint, mypy --strict, `pip check`, pip-audit) and **Test** (pytest,
   coverage gate) — shown as `Validate / Quality / quality` and `Validate / Test / test`.
2. **Coverage** — the same test suite again across the full Python version matrix (3.11,
   3.12, 3.13, 3.14) — informational, not part of the required-checks ruleset.
3. **Build** — wheel + sdist.
4. **Lint Dockerfile** — hadolint against `Dockerfile`.
5. **Build image (cache)** — builds the example service image (no push), using GHA cache.
6. **Trivy CVE scan** — scans the built image; fails on HIGH/CRITICAL vulnerabilities that
   have an available fix (`--ignore-unfixed`, since unfixed base-image CVEs aren't
   actionable here — see `make scan` to run the same check locally).
7. **Smoke tests** — runs the built image and curls `/health`, `/ready`, `/metrics`,
   `/widgets/me` — see `make smoke-test` to run the same check locally.
8. **Docker image (example service)** — builds and pushes to GHCR (push events only).
9. **PR summary** — posts/updates a single PR comment summarizing the checks above (PR
   events only).

Release (`v*` tags): validate → coverage → build (+ `twine check`) → **publish to PyPI**
(Trusted Publishing / OIDC, no stored token) *and* docker (semver tags) run in parallel →
GitHub Release (CHANGELOG excerpt), once both finish. See
[VERSIONING.md § Publishing to PyPI](./VERSIONING.md#publishing-to-pypi) for the one-time
account setup.

---

## Docker (example service only)

```bash
make docker-build
docker run --rm -p 8080:8080 -e APP_ENV=prod platform-fastapicommon-example:local
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
