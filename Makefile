# -----------------------------
# CONFIG
# -----------------------------
-include .env

APP_NAME ?= platform-fastapicommon-example
APP_ENV ?= dev
APP_PORT ?= 8080
BUILD_VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LOG_FILE ?= logs/app.log
OTLP_ENDPOINT ?= localhost:4317
OTEL_COLLECTOR_GRPC_PORT ?= 4317
OTEL_COLLECTOR_HTTP_PORT ?= 4318
TEMPO_HTTP_PORT ?= 3200
LOKI_HTTP_PORT ?= 3100
PROMETHEUS_HTTP_PORT ?= 9090
GRAFANA_HTTP_PORT ?= 3000
PROMETHEUS_SCRAPE_TARGET ?= host.docker.internal:$(APP_PORT)

# Deliberately NOT exported file-wide: `docker compose --env-file .env` reads these
# directly from .env, and every other recipe below interpolates $(VAR) at Make-expansion
# time rather than reading the shell environment. A blanket `export` here would leak
# APP_NAME/OTLP_ENDPOINT into `make test`'s pytest subprocess and silently override
# PlatformSettings' defaults in tests that assert on them. Only `run` actually needs these
# as real environment variables (its PlatformSettings() call reads os.environ), so they're
# set inline on that recipe only.

LINT_PATHS := src tests examples

.PHONY: install lint typecheck format format-check vulncheck build clean \
        test-unit test-int test-e2e test-all cover \
        setup help run \
        prometheus-config docker-build docker-up docker-down docker-logs \
        lint-docker scan smoke-test \
        ci

# -----------------------------
# SETUP
# -----------------------------

setup:
	@test -f .env || cp .env-example .env
	@mkdir -p logs
	@echo "Environment ready (.env, logs/)"

help:
	@echo "Available commands:"
	@echo " make setup         - copy .env-example to .env if missing, create logs/"
	@echo " make install       - uv sync (dev group + grpc extra) + install pre-commit hooks"
	@echo " make lint          - ruff check"
	@echo " make typecheck     - mypy --strict"
	@echo " make format        - ruff format + black (rewrites files)"
	@echo " make format-check  - ruff format --check + black --check (no changes; used in CI)"
	@echo " make vulncheck     - pip-audit against locked dependencies"
	@echo " make test-unit     - unit tests only (tests/unit — no Docker, no wired-up app)"
	@echo " make test-int      - integration tests (tests/integration — create_platform_app() end-to-end)"
	@echo " make test-e2e      - e2e tests (tests/e2e — builds + runs the real Docker image, requires Docker)"
	@echo " make test-all      - unit + integration + e2e in one pytest run (requires Docker)"
	@echo " make cover         - test-all + open an HTML coverage report (>=90% gate enforced by pyproject.toml)"
	@echo " make build         - build the wheel + sdist into dist/ (uv build)"
	@echo " make run           - run the example FastAPI service locally (uvicorn --reload)"
	@echo " make docker-build  - build the example service Docker image locally"
	@echo " make docker-up     - start the local observability stack (otel-collector, tempo, loki, prometheus, grafana)"
	@echo " make docker-down   - stop the local observability stack"
	@echo " make docker-logs   - tail logs from the observability stack containers"
	@echo " make lint-docker   - hadolint against Dockerfile (matches CI's 'Lint Dockerfile' check)"
	@echo " make scan          - build + Trivy CVE scan (matches CI's 'Trivy CVE scan' check)"
	@echo " make smoke-test    - alias for test-e2e (matches CI's 'Smoke tests' check)"
	@echo " make ci            - full local pipeline (format-check + lint + typecheck + test-all + build)"
	@echo " make clean         - remove venv, caches, build artifacts, and local logs"

# -----------------------------
# PYTHON BASICS (uv-managed: .venv + uv.lock)
# -----------------------------

install:
	uv sync --extra grpc
	uv run pre-commit install

lint:
	uv run ruff check $(LINT_PATHS)

typecheck:
	uv run mypy

format:
	uv run ruff format $(LINT_PATHS)
	uv run black $(LINT_PATHS)

format-check:
	uv run ruff format --check $(LINT_PATHS)
	uv run black --check $(LINT_PATHS)

vulncheck:
	uv run pip-audit

# -----------------------------
# TESTS
# -----------------------------

test-unit:
	uv run pytest tests/unit -v

test-int:
	uv run pytest tests/integration -v -m integration

# --no-cov: this tier runs the app inside a real Docker container, not in-process, so
# coverage.py (instrumenting this pytest process) has nothing to measure — running it
# alone would otherwise fail pyproject.toml's 90% coverage gate at 0%.
test-e2e:
	uv run pytest tests/e2e -v -m e2e --no-cov

test-all:
	uv run pytest tests/unit tests/integration tests/e2e -v

# Coverage gate (>=90%) is enforced by pyproject.toml's [tool.coverage.report] fail_under,
# applied automatically via pytest's default addopts — this target just also opens an
# HTML report.
cover: test-all
	uv run coverage html
	@echo "HTML coverage report: htmlcov/index.html"

# -----------------------------
# BUILD
# -----------------------------

build:
	@echo "Building wheel + sdist..."
	uv build

# -----------------------------
# RUN (example usage)
# -----------------------------

# Runs the example FastAPI service on the host so OTLP export reaches the
# otel-collector (localhost:4317) and Prometheus can scrape host.docker.internal.
# JSON logs are duplicated to LOG_FILE for Promtail to tail. These are set as real
# environment variables (not just Make variables) because PlatformSettings() inside
# examples/fastapi_service/main.py reads them from the process environment — scoped to
# this target only, see the note in the CONFIG section above.
run: export APP_NAME := $(APP_NAME)
run: export APP_ENV := $(APP_ENV)
run: export OTLP_ENDPOINT := $(OTLP_ENDPOINT)
run: setup
	@echo "Running example FastAPI service on :$(APP_PORT) (logs -> $(LOG_FILE))..."
	uv run uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port $(APP_PORT) --reload 2>&1 | tee -a $(LOG_FILE)

# -----------------------------
# DOCKER (EXAMPLE SERVICE IMAGE)
# -----------------------------

docker-build:
	@echo "Building Docker image (version: $(BUILD_VERSION))..."
	docker build -t $(APP_NAME):local --build-arg APP_NAME=$(APP_NAME) --build-arg BUILD_VERSION=$(BUILD_VERSION) .

# Requires hadolint (brew install hadolint). Matches CI's "Lint Dockerfile" check exactly.
lint-docker:
	hadolint --failure-threshold warning Dockerfile

# Requires trivy (brew install trivy). Matches CI's "Trivy CVE scan" check exactly:
# fails only on HIGH/CRITICAL vulnerabilities that have an available fix.
scan:
	docker build -t $(APP_NAME):scan .
	trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --scanners vuln $(APP_NAME):scan

# Alias — tests/e2e/test_smoke.py builds + runs the real image and asserts against it
# over real HTTP, superseding what used to be a separate raw docker run/curl recipe here.
smoke-test: test-e2e

# -----------------------------
# DOCKER (LOCAL OBSERVABILITY STACK)
# -----------------------------

prometheus-config:
	@sed 's|__PROMETHEUS_SCRAPE_TARGET__|$(PROMETHEUS_SCRAPE_TARGET)|g' docker/prometheus/prometheus.yml.tpl > docker/prometheus/prometheus.yml

docker-up: setup prometheus-config
	@echo "Starting observability stack (otel-collector, tempo, loki, promtail, prometheus, grafana)..."
	docker compose --env-file .env up -d
	@echo "Grafana:    http://localhost:$(GRAFANA_HTTP_PORT) (admin/admin by default)"
	@echo "Prometheus: http://localhost:$(PROMETHEUS_HTTP_PORT)"
	@echo "Now run 'make run' in another terminal to start the example service."

docker-down:
	@echo "Stopping observability stack..."
	docker compose --env-file .env down

docker-logs:
	docker compose --env-file .env logs -f

# -----------------------------
# CI (mirrors .github/workflows/validate.yml + the build job; docker image build
# is a separate CI job, not part of the default local loop)
# -----------------------------

ci: format-check lint typecheck test-all build

# -----------------------------
# CLEAN
# -----------------------------

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build *.egg-info
	rm -rf logs/*.log
