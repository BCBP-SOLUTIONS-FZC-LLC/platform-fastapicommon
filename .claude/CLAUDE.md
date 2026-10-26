# CLAUDE.md

This file provides guidance to Claude Code when working with **platform-fastapicommon**, the shared FastAPI and gRPC transport and observability library every Python platform service imports (Tender Management SaaS platform). It is the Python port of the Go `platform-gincommon`. Detailed reference lives in the sibling files listed under [See Also](#see-also). This file is what must always be in context.

## What This Repo Is

`platform-fastapicommon` is the **Python package `fastapicommon`** (`pyproject.toml`: version `0.4.1`, `requires-python >=3.11`, CI on 3.11 / 3.12 / 3.13 / 3.14 plus every runtime dependency at its floor; runtime dependencies `fastapi>=0.115.10`, `starlette>=0.46`, `pydantic-settings`, `structlog>=24.1`, `prometheus-client>=0.20`, `httpx`, OpenTelemetry `>=1.28`; extras `grpc` (`grpcio>=1.62`) and `examples` (plain `uvicorn`); managed with **uv**, `uv.lock` committed). It is published to PyPI as `platform-fastapicommon` (Apache-2.0; the repository is private) and is **never deployed**. The example service image (`examples/fastapi_service`) exists only as a CI smoke target. It tracks Go **platform-gincommon v2.1.0**: same `platform_*` metrics (registry vendored byte for byte), span shape, Zap log layout and RFC 9457 error bodies. Consumers: `platform-interop-tests`' `python-probe` (CI `interop` job); production services adopt it from `0.3.0`.

**Status:** **`0.4.1` released 2026-10-27** (CHANGELOG `[0.4.1]`; dependency and security patch). `0.4.0` (2026-10-22) was additive: RFC 9457 extension members, platform-gincommon `v2.1.0`. `0.3.0` (2026-10-17) broke against 0.2.0, allowed as a MINOR pre-1.0 — `VERSIONING.md` § Recorded exceptions. New work goes under `[Unreleased]`. Open design questions: LLD §16 (OQ-1, OQ-5 … OQ-19).

**Key responsibilities (the library is the sole implementation of):**
- **The request pipeline** — `create_platform_app()` installs, outermost first: `TracingMiddleware → ContextMiddleware → LoggingMiddleware → UnhandledErrorMiddleware → PrometheusMiddleware → TimeoutMiddleware → routing`, plus `/health`, `/live`, `/ready`, `/metrics` and the RFC 9457 exception handlers. All middleware is **pure ASGI**. Auth and roles are **per-route dependencies** (`require_authenticated_user`, `RequireRole` …), not middleware.
- **The gRPC server interceptors** — `ContextServerInterceptor` (threaded) and `AsyncContextServerInterceptor` (`grpc.aio`): span, validated context, `platform_grpc_*` metrics, `grpc_request` log and exception sanitizing in one interceptor. Streaming RPCs get sanitizing only (OQ-11).
- **The trust boundary** — gateway identity (`x-user-id`, `x-tenant-id`, `x-tenant-roles`) validated with gincommon's rules on both transports: a repeated id header is ambiguous, roles are capped (4096 bytes / 64 roles), ids are `[A-Za-z0-9._-]` ≤ 128 bytes, the client IP believes forwarding headers only from `TRUSTED_PROXIES`.
- **Telemetry** — one delegating global OTel `TracerProvider`, Zap-layout structlog on stderr, `platform_*` collectors built from the vendored registry, Tier 3 validation, `fastapicommon-metricslint`.
- **Errors** — every HTTP error is an `application/problem+json` problem identical to gincommon's; no exception message ever reaches a client, a log or a span (`unclassified error` carries types and frames only; `platform_unclassified_errors_total{transport}`).

**Does NOT own:** authentication (the API gateway); business authorization policy; request-body models; database access (`platform-pgcommon-py`); messaging (`platform-eventcommon`); rate limiting / retries (gateway, mesh); the collector / Tempo / Prometheus / Loki in production; the registry's content (platform-gincommon + observability governance).

## Common Commands

```bash
make setup             # uv sync (dev group + grpc extra) + .env from .env-example + logs/ + pre-commit hook (run once)
make help              # every target, one line each
make format / format-check   # ruff format + black + ruff --fix / check only (aliases fmt, fmt-check)
make lint / typecheck  # ruff check / mypy --strict
make lock-check / pip-check / vulncheck   # uv lock --check / uv pip check / pip-audit
make metrics-lint      # registry self-check, rule references, promtool, inventory drift
make metrics-inventory # regenerate docs/observability/metric-registry.md (alias metrics-doc)
make gincommon-parity  # registry, rules, inventory identical to ../../platform-gincommon (GINCOMMON_DIR)
make docs-check        # every docs/architecture/mermaid/*.mmd embedded byte-identically in ARCHITECTURE.md
make test              # unit + integration + e2e (Docker; alias test-all)
make test-unit / test-int / test-e2e      # no Docker / in-process app / the real image
make test-ci / cover-check / cover / cover-func   # CI test gate / + ≥ 90% gate / + HTML / per-file report
make build             # wheel + sdist, twine check --strict, installed-wheel check
make ci                # lock-check + pip-check + format-check + lint + typecheck + metrics-lint + docs-check + ci-scripts-test + vulncheck + build + test-ci → coverage gate
make api-compat / release-guard TAG=vX.Y.Z   # griffe vs the last tag / pre-tag version + CHANGELOG check
make docker-build / lint-docker / scan / smoke-test   # example image, hadolint, Trivy, smoke-tests.sh + tests/e2e
make run / show-config # example service on :8080 (--reload) / resolved settings + tracing config
make docker-up / docker-down / docker-reset / docker-logs / docker-ps   # local otel-collector, Tempo, Loki, Promtail, Prometheus, Grafana
```

Run a single test (always `--no-cov`: `pyproject.toml`'s pytest `addopts` collect coverage and enforce `fail_under = 90`, so a subset fails on coverage even when it passes):
```bash
uv run pytest tests/unit/middleware/test_context_middleware.py -k ambiguous -v --no-cov
uv run pytest tests/integration/test_observability_signals.py::test_cancelled_request_is_aborted_not_a_panic --no-cov
```

If the shell has another project's venv active, uv warns `VIRTUAL_ENV=… does not match the project environment`; prefix with `env -u VIRTUAL_ENV`.

**Coverage note:** line + branch coverage of `src/fastapicommon` over unit + integration. Gate **≥ 90%** (`[tool.coverage.report] fail_under`, `.github/scripts/coverage-gate.sh`). Current: **100%** line and branch, **425 tests** (388 unit, 31 integration, 6 e2e), no `pragma: no cover` in `src/`. Keep it there: test a reachable path; restructure an unreachable branch away rather than excluding it.

## Architecture

A framework-free core (`context`, `config`, `problem`, `health`, `_scope`) under FastAPI / Starlette / gRPC / OpenTelemetry adapters, and one composition function (`bootstrap.create_platform_app`). `fastapicommon/__init__.py` imports only `bootstrap`, so `import fastapicommon` never imports `grpc`. There is no import-linter contract; cycles are broken with function-local imports marked `# avoid an import cycle`. Layout and dependency rules: **[`.claude/architecture.md`](architecture.md)**; the narrative with 16 diagrams is [`ARCHITECTURE.md`](../ARCHITECTURE.md); the contract is the [LLD](../docs/lld/platform-fastapicommon-lld.md).

## Critical Rules (do not break)

- **gincommon is the behavioural source of truth.** Match platform-gincommon v2.0.0 for log keys and levels, span names and attributes, metric names / labels / buckets, problem types / titles / codes / messages, and validation rules. A deliberate divergence is an LLD §16 open question, never a silent change.
- **The registry belongs to platform-gincommon.** `src/fastapicommon/metrics/registry.json` is vendored **byte for byte** (`make gincommon-parity`, CI-required on `main`). Never edit it here; a new shared metric goes into gincommon's registry first.
- **No exception message reaches a client, a log or a span.** Unclassified errors log `error_type` / `error_chain` / `stack` frames only; spans record the type only (`record_span_error` too); 5xx problems carry no `detail`. Every test that touches errors asserts the secret never appears.
- **Unverified identity is never logged as verified.** `tenant_id` / `user_id` in log entries come only from the validated `RequestContext`; client values on anonymous or rejected requests are `claimed_*`.
- **Pure ASGI only.** Never use Starlette `BaseHTTPMiddleware` — it ends a failed stream as a clean 200 and unbinds the context before the body is sent.
- **Aborted and cancelled requests are never panics.** A failure after the response started, or a cancellation, is `aborted` (499 or the status sent, `client_error`), decided once in `_scope.request_outcome` and shared by tracing, logging and metrics.
- **One server span per request.** FastAPI ≥ 0.143 instruments itself; `create_platform_app` turns it off (`FASTAPI_NATIVE_TELEMETRY_OFF`). Any new framework-level telemetry must be switched off the same way — the signals are this library's.
- **Never replace a tracer provider the service installed itself**, and never shut the global provider down on an app's shutdown (`flush_tracing`, not `shutdown_tracing`).
- **Required check names:** `Validate / Test / test` and `Validate / Quality / quality` (reusable workflows skip their *inner* job on docs-only changes so the checks still report). Don't rename jobs.
- **CHANGELOG entry required** for any change to `src/`, `pyproject.toml`, `uv.lock`, `build-constraints.txt` or the `Dockerfile` (`changelog-check.yml`), under `[Unreleased]` in its Keep-a-Changelog category (Security, Changed (breaking), Added, Fixed, Changed, Removed (breaking)).
- **Diagrams:** edit a `docs/architecture/mermaid/*.mmd` and its embedded block in `ARCHITECTURE.md` together (`make docs-check`). A `;` in a sequence-diagram message breaks GitHub rendering; check with `ghcr.io/mermaid-js/mermaid-cli/mermaid-cli:11.4.2`.

## Conventions

- `mypy --strict` over `src/fastapicommon`; ruff + black, line length 100. Every public symbol typed, documented and exported through its subpackage's `__all__` — that `__all__` is the SemVer surface; underscore modules (`_errors`, `_scope`) are not.
- Tests mirror `src/`: `tests/unit/<subpackage>/`; integration in `tests/integration/` (`pytest.mark.integration`, no Docker); e2e in `tests/e2e/` (`pytest.mark.e2e`, the real image). `tests/conftest.py`'s autouse fixtures isolate metrics state (collectors a test left on the default `REGISTRY` are unregistered) and tracing (`shutdown_tracing()` after each test); logging is not reset, so a test that asserts log output calls `configure_logging(...)` itself. Use an isolated `CollectorRegistry` and an `InMemorySpanExporter`; assert Zap entries by parsing stderr (`capsys`).
- Function-local imports are not allowed in annotations under `from __future__ import annotations` on the old-FastAPI floor — import at module level.
- A regression test names the bug in its docstring (`Regression: …`).

## Wire Contracts

What crosses the process boundary — headers, problem types, log events, metric labels, span attributes: **[`.claude/wire-contracts.md`](wire-contracts.md)**.

## Public API

Fourteen subpackages (`bootstrap`, `config`, `context`, `auth`, `rbac`, `exceptions`, `problem`, `grpc`, `telemetry`, `logging`, `propagation`, `metrics`, `health`, `middleware`) and the `fastapicommon-metricslint` CLI. Catalogue, settings and env vars: **[`.claude/api-reference.md`](api-reference.md)**.

## Runtime Flows

HTTP request, identity rejection, unclassified error, abort / cancellation, timeout, gRPC (threaded / aio), tracing initialisation, shutdown: **[`.claude/request-flows.md`](request-flows.md)**.

## Operations

Local stack, CI workflows, release process, troubleshooting: **[`.claude/operations.md`](operations.md)**.

## Extending the Library

- **New middleware:** pure ASGI in `middleware/`, exported in `__all__`, placed in `bootstrap/app.py` (`add_middleware` runs innermost first); update ARCHITECTURE § Execution model, `request-flow.mmd` / `bootstrap-flow.mmd` and the README chain.
- **New metric:** platform-gincommon registry first (`proposed`), re-vendor, `make gincommon-parity` + `make metrics-inventory`; build it in `PlatformMetrics.__init__` (`metrics/platform.py`) from its entry, add a `record_*` method; callers guard on `platform_metrics()` being `None`.
- **New setting / env var:** `config/settings.py`, README environment table, LLD §12, `.env-example`.
- **New problem type:** mirror gincommon's `registry.md` (15 platform types today); `tests/unit/problem` checks the set against it.

## See Also

Detailed reference in `.claude/`:
- [`architecture.md`](architecture.md) — package layout, dependency rules, middleware order, process state
- [`wire-contracts.md`](wire-contracts.md) — headers, problem types, log events, metric and span contracts (this library has no database; this replaces the siblings' `database-schema.md`)
- [`api-reference.md`](api-reference.md) — public API per subpackage, `PlatformSettings` + env vars + defaults, metricslint
- [`request-flows.md`](request-flows.md) — step-by-step flows and failure scenarios
- [`operations.md`](operations.md) — local dev, testing, CI, release, troubleshooting

Repository docs:
- **`README.md`** — consumer guide, integration, local development, testing observability locally
- **`ARCHITECTURE.md`** — narrative with 16 diagrams, invariants, threat model, conformance checklist
- **`docs/lld/platform-fastapicommon-lld.md`** — low-level design (rev 1.2; §16 open-questions register). Keep it in sync when behaviour changes
- **`docs/problem-details.md`**, **`docs/observability/`**, **`docs/adr/`**, **`docs/MigrationGuide.md`**, **`CHANGELOG.md`**, **`VERSIONING.md`**, **`CONTRIBUTING.md`**, **`SECURITY.md`**
