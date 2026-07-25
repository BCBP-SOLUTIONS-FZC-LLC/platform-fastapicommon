# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/) — see [VERSIONING.md](./VERSIONING.md) for the
pre-1.0 caveat on what SemVer guarantees currently apply.

## [Unreleased]

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

[Unreleased]: https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/commits/main
