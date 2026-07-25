# Contributing

This is an internal shared library for BCBP Solutions platform services. This guide
covers how to set up a development environment, add new capabilities, write tests, and
cut a release.

## Prerequisites

- Python 3.11+ (matches `requires-python` in `pyproject.toml`)
- [`uv`](https://docs.astral.sh/uv/) — manages the venv, dependency resolution, and
  `uv.lock` (matches `platform-pgcommon-py`'s convention). Install via `brew install uv`
  or see uv's install docs; `make install` does not install `uv` itself.
- Docker (for the local observability stack — see the README's "Local testing with the
  observability stack")
- `pre-commit` is installed as part of `make install` — no separate install needed

## Development setup

```bash
git clone https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon
cd platform-fastapicommon
make setup     # copies .env-example -> .env, creates logs/
make install   # uv sync (dev group + grpc extra, from uv.lock) + install pre-commit hooks
make lint      # verify ruff + mypy pass
make test      # run all tests with the coverage gate
```

## Project layout

```
src/fastapicommon/
  context/      <- CurrentUser, RequestContext, contextvars accessors — no framework deps
  exceptions/   <- PlatformException hierarchy + FastAPI exception handlers — no framework deps
  config/       <- PlatformSettings (pydantic-settings) — no framework deps
  auth/         <- header constants, get_current_user, require_authenticated_user
  middleware/   <- ContextMiddleware, TimeoutMiddleware
  logging/      <- structlog JSON configuration, get_logger
  metrics/      <- Prometheus definitions, PrometheusMiddleware, /metrics router
  telemetry/    <- OpenTelemetry tracing setup (HTTP + gRPC)
  rbac/         <- RequireRole / RequireAnyRole / RequireAllRoles
  health/       <- /health, /live, /ready router
  propagation/  <- create_platform_http_client (outbound header propagation)
  grpc/         <- server/client interceptors (requires the grpc extra)
  bootstrap/    <- create_platform_app() — the single entry point
tests/          <- mirrors src/fastapicommon/ one-to-one, plus a top-level test_package.py
examples/       <- fastapi_service and grpc_service demo services (not part of the library)
docs/adr/       <- ADRs for cross-cutting standards (logging, tracing, metrics, context)
docs/architecture/mermaid/ <- Mermaid diagram sources embedded in ARCHITECTURE.md
```

**Dependency rule:** `context`, `exceptions`, and `config` import nothing from the rest of
this library. Everything else may depend on them, never the reverse — see
[`ARCHITECTURE.md`](./ARCHITECTURE.md#module-dependency-graph) for the full graph.

## Adding a new middleware

1. Implement in `src/fastapicommon/middleware/your_middleware.py`.
2. Export it from `src/fastapicommon/middleware/__init__.py`'s `__all__`.
3. Add unit tests in `tests/middleware/`.
4. If it changes the middleware add-order in `create_platform_app`, update the
   [Execution model](./ARCHITECTURE.md#execution-model) section of `ARCHITECTURE.md` and
   `docs/architecture/mermaid/request-flow.mmd` / `bootstrap-flow.mmd` to match.

## Adding a new metric

1. Declare the `Counter`/`Histogram` in `src/fastapicommon/metrics/definitions.py`.
2. Record it from the relevant middleware/interceptor (follow the pattern in
   `PrometheusMiddleware` or `record_grpc_request`).
3. Add it to the [Registered Prometheus metrics](./ARCHITECTURE.md#registered-prometheus-metrics)
   table in `ARCHITECTURE.md`.
4. Add a test asserting the metric is recorded (see `tests/metrics/test_metrics.py`).

## Extending RBAC or logging

Unlike `platform-gincommon`, this library does not define a pluggable `Authorizer` or
`Logger` interface to implement — `fastapicommon.rbac` reads roles directly off the bound
`RequestContext`, and `fastapicommon.logging.get_logger` always returns a structlog logger
configured by `configure_logging`. If a service needs custom authorization logic beyond
role membership, build it on top of `fastapicommon.context.get_roles()` /
`get_current_user()` rather than expecting an injectable interface — there isn't one.

## Testing requirements

| Layer | Location | Notes |
|-------|----------|-------|
| Unit | `tests/<module>/` | Mirrors `src/fastapicommon/<module>/`; no live server or Docker required |
| Bootstrap / integration | `tests/bootstrap/` | Exercises `create_platform_app()` end-to-end via `fastapi.testclient.TestClient` |

All tests must pass without a running server or Docker stack — the observability stack in
`docker-compose.yml` is for manual/local verification (see the README), not part of the
automated test suite.

Coverage must stay at or above 90% (`[tool.coverage.report].fail_under` in
`pyproject.toml`) — `pytest` enforces this automatically, no separate flag needed.

## Linting

```bash
make lint
```

Ruff (`[tool.ruff]` in `pyproject.toml`) enforces formatting/import/lint rules; mypy runs
in `--strict` mode over `src/fastapicommon`. New public functions and classes must be
fully typed — this is enforced, not optional.

## Branching strategy (GitFlow)

- `main` — always releasable; tagged releases only.
- `develop` — integration branch for the next release.
- `feature/*` — branched from `develop`, merged back into `develop` via PR.
- `release/*` — branched from `develop` when preparing a release; only bug fixes and
  release metadata (version bump, changelog) land here before merging into both `main`
  and `develop`.
- `hotfix/*` — branched from `main` for urgent production fixes; merged back into both
  `main` and `develop`.

## Pull request checklist

- [ ] `make lint` passes (ruff + mypy)
- [ ] `make test` passes (coverage gate included)
- [ ] New/changed public API is fully typed and documented in `ARCHITECTURE.md`'s
      [Public API](./ARCHITECTURE.md#public-api) tables
- [ ] `CHANGELOG.md` `[Unreleased]` section updated
- [ ] If a cross-cutting standard changed (new metric family, new propagated header, new
      log field): an ADR added under `docs/adr/`
- [ ] If the public API changed: `VERSIONING.md` impact assessed (MAJOR vs MINOR vs PATCH)

## Commit style

Use a short imperative subject line (≤ 72 chars). Reference the area:

```
fix(rbac): return 403 instead of 500 when roles header is empty
feat(telemetry): support OTLP/HTTP exporter in addition to gRPC
docs: update sampling ratio default in ARCHITECTURE.md
```

## Cutting a release

See **[VERSIONING.md § Maintainer release process](./VERSIONING.md#maintainer-release-process)**
for the full steps. Short version:

1. Move `[Unreleased]` items to a new `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`.
2. Bump the version in `pyproject.toml` and `src/fastapicommon/__init__.py`.
3. Run `make ci` locally.
4. Create and push an annotated tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
5. The release workflow creates the GitHub Release automatically from the tag.
6. Notify consuming teams via Slack with upgrade notes if MINOR or MAJOR.
