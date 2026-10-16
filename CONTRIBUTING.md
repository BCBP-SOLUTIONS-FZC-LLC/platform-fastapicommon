# Contributing

This is an internal shared library for BCBP Solutions platform services. This guide covers how to set up a development environment, add new middleware and metrics, write tests, and cut a release.

## Prerequisites

- Python 3.11+ (matches `requires-python` in `pyproject.toml`)
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`) — manages the venv and `uv.lock`; `make` does not install it
- Docker — for `tests/e2e`, `make smoke-test`, the local observability stack and, without a local `promtool`, the rule tests in `make metrics-lint`
- A platform-gincommon checkout next to this repository for `make gincommon-parity` (`GINCOMMON_DIR`, default `../../platform-gincommon`)
- `ruff`, `black`, `mypy`, `pytest` and `pre-commit` come from the locked dev group — no separate install needed

## Development setup

```bash
git clone https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon
cd platform-fastapicommon
make setup       # uv sync (dev group + grpc extra), .env from .env-example, logs/, pre-commit hook
make lint        # verify ruff passes
make typecheck   # verify mypy --strict passes
make test-unit   # unit tests, no Docker
make ci          # every gate CI runs, before pushing
```

## Project layout

```
src/fastapicommon/     ← Public API: each subpackage's __all__ (SemVer applies)
  context/ config/ problem/ health/   ← Core: no framework or internal deps
  auth/ rbac/                         ← Gateway-identity validation, per-route auth and role dependencies
  exceptions/                         ← PlatformException hierarchy, RFC 9457 handlers, UnhandledErrorMiddleware
  middleware/                         ← Tracing, Context, Logging, Timeout (pure ASGI)
  logging/                            ← Zap-layout structlog config, sanitisation, audit entries
  metrics/                            ← registry.json (vendored), platform_* collectors, compliance, metricslint
  telemetry/                          ← Delegating TracerProvider, TracingConfig, tracer helpers
  grpc/                               ← Server and client interceptors (grpc extra)
  propagation/                        ← create_platform_http_client
  bootstrap/                          ← create_platform_app(): the composition layer
  _errors.py _scope.py                ← Internal (underscore: no SemVer)
examples/              ← fastapi_service, grpc_service (not for consumers)
tests/                 ← All tests live here: unit/ mirrors src/, integration/, e2e/
docs/                  ← lld/, architecture/ (Mermaid), observability/, adr/, problem-details.md
docker/                ← Local observability stack config, vendored Prometheus rules
```

**Dependency rule:** the core (`context`, `config`, `problem`, `health`, `_scope`) imports nothing internal; adapters depend inward; only `bootstrap` composes, and `import fastapicommon` never imports `grpc`. Full table and graph: [ARCHITECTURE.md § Module dependency graph](./ARCHITECTURE.md#module-dependency-graph).

## Adding a new HTTP middleware

1. Implement it in `src/fastapicommon/middleware/your_middleware.py` as **pure ASGI** (`__call__(scope, receive, send)`), not Starlette `BaseHTTPMiddleware`, which ends a failed stream as a clean response and unbinds the request context before the body is sent.
2. Export it from `src/fastapicommon/middleware/__init__.py`'s `__all__`.
3. Add unit tests in `tests/unit/middleware/`, and an integration test in `tests/integration/` if it changes the signals (`test_observability_signals.py`).
4. If it records metrics, add them as described below; if it logs, use the existing sanitiser (`fastapicommon.logging.sanitize`) and add the entry to the [LLD log catalogue](./docs/lld/platform-fastapicommon-lld.md#73-log-event-catalogue).
5. If it changes the stack order in `create_platform_app` (`bootstrap/app.py` — `add_middleware` runs innermost first), update the [Execution model](./ARCHITECTURE.md#execution-model), `docs/architecture/mermaid/request-flow.mmd` / `bootstrap-flow.mmd` (then re-embed them; `make docs-check` fails on drift), and the chain in `README.md`.

## Adding a new metric

The registry is the contract, and it belongs to platform-gincommon:

- **Shared (`platform_*`, `<domain>_*`):** add the entry to platform-gincommon's registry first (status `proposed`). Re-vendor `src/fastapicommon/metrics/registry.json` byte for byte, then run `make gincommon-parity` and `make metrics-inventory`. If the library emits the metric itself, build the collector in `PlatformMetrics.__init__` (`metrics/platform.py`) with `_counter` / `_gauge` / `_histogram(name)`, which take the name, help, labels and buckets from the entry. Then add a `record_*` method. Callers guard on `platform_metrics()` being `None`, so the stack is a pass-through until metrics are initialised.
- **Service-only (Tier 3):** not in this library — services use `new_counter` / `new_gauge` / `new_histogram`.

Then add it to the [Registered Prometheus metrics](./ARCHITECTURE.md#registered-prometheus-metrics) table, test it in `tests/unit/metrics/`, and keep `tests/integration/test_metrics_compliance.py` at 0 errors.

## Replacing logger or RBAC

Unlike platform-gincommon, there is no `port.Logger` / `port.Authorizer` to implement:

- **Logger:** `fastapicommon.logging` configures structlog through the stdlib root logger; services add stdlib handlers or structlog processors rather than replacing the logger. The library's own entries keep their fields and layout.
- **RBAC:** `RequireRole` / `RequireAnyRole` / `RequireAllRoles` check the validated roles. Tenant- or resource-scoped policy is a FastAPI dependency in the service, built on `get_request_context()`, raising `ForbiddenException`.

## Testing requirements

| Layer | Location | Notes |
|-------|----------|-------|
| Unit | `tests/unit/<module>/` | Mirrors `src/fastapicommon/<module>/`; no network, no Docker |
| Integration | `tests/integration/` | Modules wired together (`create_platform_app` through `TestClient`, real gRPC servers on loopback); `pytest.mark.integration`; no Docker |
| E2E | `tests/e2e/` | Builds and runs the real image over a socket; `pytest.mark.e2e`; skipped without the `docker` CLI |
| Smoke | `.github/scripts/smoke-tests.sh` | Against the built image (`make smoke-test`, CI's "Smoke tests") |

All tests under `tests/unit/` and `tests/integration/` must pass without a running server or Docker stack.

Coverage over unit + integration must stay at or above **90%** (`fail_under` in `pyproject.toml`; currently 100%, lines and branches). Run the gate before submitting a PR:

```bash
make cover-check   # test-ci + the coverage gate
```

A subset alone (`make test-int`, `pytest -k ...`) is below the gate by design — pass `--no-cov`. The canonical tests that must not break are listed in the [README](./README.md#canonical-tests-do-not-break) and [LLD §14.5](./docs/lld/platform-fastapicommon-lld.md#145-canonical-test-cases-do-not-break).

## Linting

```bash
make format-check   # ruff format + black, check only (make format to apply)
make lint           # ruff ([tool.ruff] in pyproject.toml)
make typecheck      # mypy --strict over src/fastapicommon
```

Every public function and class is fully typed (enforced by `mypy --strict`) and has a docstring — the docstrings are the API reference (`make docs-serve`). The pre-commit hook runs the same checks on commit.

## Pull request checklist

- [ ] `make ci` passes locally (lock + format + lint + typecheck + metrics-lint + docs-check + vulncheck + build + tests + coverage gate)
- [ ] New public symbols are typed, documented and exported through `__all__`
- [ ] New behaviour is covered by tests in `tests/unit/` or `tests/integration/`
- [ ] `CHANGELOG.md` `[Unreleased]` section updated (`changelog-check.yml` enforces it for `src/`, `pyproject.toml`, `uv.lock`, `build-constraints.txt` and the `Dockerfile`)
- [ ] If the registry changed: platform-gincommon first, then `make gincommon-parity` and `make metrics-inventory`
- [ ] If the public API changed: README, [ARCHITECTURE.md § Public API](./ARCHITECTURE.md#public-api) and `VERSIONING.md` impact assessed (`make api-compat`)
- [ ] If a cross-cutting standard changed (metric family, propagated header, log field): an ADR under `docs/adr/`

## Commit style

Use a short imperative subject line (≤ 72 chars). Reference the area:

```
fix(security): ambiguous identity, role caps, trusted proxies
feat(grpc): add AsyncContextServerInterceptor for grpc.aio servers
docs: update sampling ratio default in README
```

Branch from `main` (`feat/*`, `fix/*`, `docs/*`, `chore/*`) and merge back through a PR; releases are tagged on `main`.

## Cutting a release

See **[VERSIONING.md § Maintainer release process](./VERSIONING.md#maintainer-release-process)** for the full steps. Short version:

1. Bump the version in `pyproject.toml`, `src/fastapicommon/__init__.py` (`__version__`) and `tests/unit/test_package.py`.
2. Move `[Unreleased]` items to a new `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`.
3. Run `make ci` and `make release-guard TAG=vX.Y.Z` locally, and merge to `main`.
4. Create and push an annotated tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
5. `release.yml` publishes to PyPI and creates the GitHub Release from the CHANGELOG section.
6. Notify consuming teams via Slack with upgrade notes if MINOR or MAJOR.
