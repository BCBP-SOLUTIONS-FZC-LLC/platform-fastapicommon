# Operations

## 1. Local Development

**Prerequisites:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Docker (e2e, smoke, local stack, `promtool` fallback). A platform-gincommon checkout at `../../platform-gincommon` (`GINCOMMON_DIR`) for `make gincommon-parity`; without it the parity tests skip. Optional: `hadolint`, `trivy`, `gh`, `cosign`.

```bash
make setup        # uv sync (dev group + grpc extra) + .env from .env-example + logs/ + pre-commit hook
make ci           # every validate-* gate locally (no Docker needed)
make run          # example service on :8080: APP_ENV=dev, OBSERVABILITY_DOMAIN=iam, LOG_FILE=logs/app.log, OTLP to localhost:4317
```

- **Pre-commit hook** (`.pre-commit-config.yaml`): ruff, black, mypy, pytest, whitespace. Ruff may auto-fix on commit — re-stage and commit again.
- **Observability stack** (`docker-compose.yml`): `otel-collector` (4317/4318) → `tempo` (3200); `prometheus` (9090, scrapes `host.docker.internal:8080/metrics`, loads the vendored rules); `promtail` → `loki` (3100) from `logs/app.log`; `grafana` (3000). Ports overridable (`OTEL_COLLECTOR_GRPC_PORT`, `TEMPO_HTTP_PORT`, `PROMETHEUS_HTTP_PORT`, `LOKI_HTTP_PORT`, `GRAFANA_HTTP_PORT`). Targets `docker-up`, `docker-down`, `docker-reset`, `docker-logs`, `docker-ps`. README § Testing observability locally walks it.
- **Debug helpers:** `make show-config` (resolved settings + tracing config), `make docs-serve` (pydoc on 8081), `uv run fastapicommon-metricslint check --url http://localhost:8080/metrics`.
- **Diagrams:** render a changed `.mmd` with `docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/docs/architecture/mermaid:/data" ghcr.io/mermaid-js/mermaid-cli/mermaid-cli:11.4.2 -i /data/<name>.mmd -o /tmp/out.svg` — `make docs-check` only checks the embedding, not that GitHub can render it.

## 2. Testing Strategy

| Suite | Where | Covers |
|---|---|---|
| Unit | `tests/unit/<subpackage>/` (388 tests, no Docker) | every module; real threaded and `grpc.aio` servers on loopback; Zap output parsed from stderr; isolated `CollectorRegistry` / `InMemorySpanExporter` |
| Integration | `tests/integration/` (31, in-process) | `create_platform_app()` end to end: wiring and lifespans, signals for success / 404 / 500 / aborted / cancelled, 401 problems with audit entries, mounted routes, the library's own scrape against the registry |
| E2E | `tests/e2e/` (6, Docker) | the built image over HTTP |
| Smoke | `smoke-tests.sh` (CI `Smoke tests`, `make smoke-test`) | image ≤ 170 MB uncompressed, startup failure on invalid identity, `/health`, `platform_*` metrics, RFC 9457 not-found, non-root, no pip |
| Compatibility | CI | Python 3.11 / 3.13 / 3.14 matrix; `lowest-deps.sh` (every runtime dependency at its floor: FastAPI 0.115.10, Starlette 0.46); platform-interop-tests (`error_shape`: a Go and a Python 401 are the same problem) |

**Status (2026-10-17):** 425 tests (all dependencies at their latest versions); coverage **100%** line and branch over unit + integration (gate ≥ 90%); no `pragma: no cover` in `src/`. Running `lowest-deps.sh` locally replaces `.venv` — run it in a copy of the repository.

**Canonical tests (do not break):** LLD §14.5 (T-1 … T-16) and README § Canonical tests — unclassified errors on HTTP / threaded / aio, aborted and cancelled requests, timeout cancellation, the tracing provider contract, context isolation, identity validation on both transports, trusted-proxy client IP, and `test_vendored_registry_is_identical_to_gincommon`.

## 3. CI Workflows and Required Checks

Ported from platform-gincommon; job logic in `.github/scripts/*` (shellcheck-clean); every third-party action pinned by SHA.

- **`ci.yml`** (PRs and pushes to `main`): `detect changes` (docs-only → gated jobs skip but report) → `validate-test` ∥ `validate-quality` ∥ `build-image` (Hadolint, `.dockerignore`, cached amd64 build) ∥ `api-compat` (informational) → `trivy` (CRITICAL / HIGH / UNKNOWN with a fix; SARIF; SBOM) and `smoke` → `interop` → `pr-summary` → `push` (main only; fingerprint-checked; Cosign sign + verify).
- **`validate-test.yml`**: unit + integration with coverage on 3.12 and the ≥ 90% gate (`Validate / Test / test`); 3.11 / 3.13 / 3.14; `lowest direct dependencies`.
- **`validate-quality.yml`** (`Validate / Quality / quality`): lock check, `uv pip check`, HTML-entity guard, ruff format + black, ruff, mypy `--strict`, `make metrics-lint`, **gincommon parity** at `GINCOMMON_REF` (needs `CI_REPO_READ_TOKEN`), diagram sync, CI-script tests, pip-audit, digest-pinned `FROM`, `make build`.
- **`docs.yml`** (diagram sync on docs-only changes), **`changelog-check.yml`** (CHANGELOG required for `src/`, `pyproject.toml`, `uv.lock`, `build-constraints.txt`, `Dockerfile`; Dependabot exempt), **`release.yml`**.

## 4. Release State and Process

- Tags: `v0.1.0`, `v0.1.1`, `v0.2.0` (these predate signing and `SHA256SUMS`), `v0.3.0` (2026-10-17; the first signed release), **`v0.4.0`** (current, 2026-10-22). New work goes under `[Unreleased]`.
- To release: bump `pyproject.toml` `version`, `src/fastapicommon/__init__.py` `__version__` **and** `tests/unit/test_package.py` (`release_guard.py` checks only `pyproject.toml`); rename `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and update the compare links; `make ci` + `make release-guard TAG=vX.Y.Z`; merge to `main`; push an annotated tag. `release.yml`: verify → the CI gates + blocking `api-compat` → dist once (reproducible, `SHA256SUMS`) → image push (fingerprint-matched, signed, SBOM, provenance) → PyPI (Trusted Publishing) → GitHub Release. Verification commands: `VERSIONING.md` § Verifying a release.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError` naming `OBSERVABILITY_DOMAIN` / environment vocabulary | identity unset or `production` / `development` | set a registry domain; `APP_ENV=local\|dev\|test\|staging\|prod` |
| exporter retry warnings / no traces | no collector at `localhost:4317`, or TLS against plaintext | `make docker-up`, `OTEL_EXPORTER_OTLP_INSECURE=true` for plaintext, or `OTEL_TRACES_EXPORTER=none` |
| a subset of tests "fails" on coverage | `fail_under = 90` in `addopts` | pass `--no-cov` |
| `VIRTUAL_ENV … does not match the project environment` | another project's venv is active | `env -u VIRTUAL_ENV uv run …` |
| `make gincommon-parity` / parity tests skip | no gincommon checkout | clone it next to this repo or set `GINCOMMON_DIR` |
| smoke size gate passes locally, fails in CI | Docker Desktop reports the compressed size | trust CI's uncompressed number |
| Mermaid diagram blank on GitHub | `;` in a sequence-diagram message | replace with `,`; render with mermaid-cli |
| 401 `Ambiguous x-user-id header.` behind the gateway | the gateway appends identity headers | make it replace them |
