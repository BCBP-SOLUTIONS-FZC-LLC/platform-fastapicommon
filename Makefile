# -----------------------------
# CONFIG
# -----------------------------
-include .env

APP_NAME ?= platform-fastapicommon-example
APP_ENV ?= dev
APP_PORT ?= 8080
OBSERVABILITY_DOMAIN ?= iam
BUILD_VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LOG_FILE ?= logs/app.log
OTEL_EXPORTER_OTLP_ENDPOINT ?= localhost:4317
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
# APP_NAME/OTEL_EXPORTER_OTLP_ENDPOINT into `make test`'s pytest subprocess and silently override
# PlatformSettings' defaults in tests that assert on them. Only `run` actually needs these
# as real environment variables (its PlatformSettings() call reads os.environ), so they're
# set inline on that recipe only.

LINT_PATHS := src tests examples

.PHONY: install lint typecheck format format-check vulncheck build clean \
        lock-check pip-check docs-check ci-scripts-test api-compat release-guard \
        build-constraints pin-base-images \
        metrics-lint metrics-registry metrics-refs metrics-rules metrics-inventory gincommon-parity \
        test-unit test-int test-e2e test-all test-ci cover \
        setup help run \
        prometheus-config docker-build docker-up docker-down docker-logs \
        lint-docker scan smoke-test \
        ci

CI_IMAGE ?= platform-fastapicommon-ci-test

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
	@echo " make lock-check    - uv.lock up to date with pyproject.toml (uv lock --check)"
	@echo " make pip-check     - installed .venv has consistent dependencies (uv pip check)"
	@echo " make docs-check    - every docs/architecture/mermaid/*.mmd embedded verbatim in ARCHITECTURE.md"
	@echo " make ci-scripts-test - regression tests for the CI shell scripts (detect-changes.sh)"
	@echo " make metrics-lint  - registry self-check, rule refs, promtool, inventory drift (CI gate)"
	@echo " make metrics-inventory - regenerate docs/observability/metric-registry.md"
	@echo " make gincommon-parity GINCOMMON_DIR=path - registry, rules, inventory identical to gincommon's"
	@echo " make api-compat    - griffe vs the last release tag (API_NEW_VERSION=vX.Y.Z: fail unless allowed to break)"
	@echo " make release-guard TAG=vX.Y.Z - package version == TAG and CHANGELOG has a dated section"
	@echo " make test-unit     - unit tests only (tests/unit — no Docker, no wired-up app)"
	@echo " make test-int      - integration tests (tests/integration — create_platform_app() end-to-end)"
	@echo " make test-e2e      - e2e tests (tests/e2e — builds + runs the real Docker image, requires Docker)"
	@echo " make test-all      - unit + integration + e2e in one pytest run (requires Docker)"
	@echo " make test-ci       - unit + integration with coverage data, no threshold (CI's 'Validate / Test')"
	@echo " make cover         - test-all + open an HTML coverage report (>=90% gate enforced by pyproject.toml)"
	@echo " make build         - wheel + sdist (hash-locked backend), twine check, installed-wheel check"
	@echo " make build-constraints - re-lock the build backend (pyproject [build-system]) with hashes"
	@echo " make pin-base-images - pin the Dockerfile base images to their current @sha256 digests"
	@echo " make run           - run the example FastAPI service locally (uvicorn --reload)"
	@echo " make docker-build  - build the example service Docker image locally"
	@echo " make docker-up     - start the local observability stack (otel-collector, tempo, loki, prometheus, grafana)"
	@echo " make docker-down   - stop the local observability stack"
	@echo " make docker-logs   - tail logs from the observability stack containers"
	@echo " make lint-docker   - hadolint against Dockerfile (matches CI's 'Lint Dockerfile' check)"
	@echo " make scan          - build + Trivy CVE scan (matches CI's 'Trivy CVE scan' check)"
	@echo " make smoke-test    - build the image, smoke-tests.sh + tests/e2e against it (CI's 'Smoke tests')"
	@echo " make ci            - local validate-quality + validate-test (no Docker image jobs)"
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

# uv.lock matches pyproject.toml (validate-quality's "Lock check"; Go's tidy drift check).
lock-check:
	uv lock --check

# The installed .venv has no missing or conflicting requirements.
pip-check:
	uv pip check

# Every docs/architecture/mermaid/*.mmd is embedded byte-identically in ARCHITECTURE.md
# (validate-quality's "Architecture diagram sync"; docs.yml runs it on docs-only changes).
docs-check:
	bash .github/scripts/docs-mermaid-sync.sh

# Regression tests for the CI shell scripts (scratch git repositories, no Docker):
# detect-changes.sh's docs-only decision and its log sanitising.
ci-scripts-test:
	bash .github/scripts/detect-changes_test.sh

# -----------------------------
# OBSERVABILITY STANDARD (mirrors platform-gincommon's `make metrics-lint`)
# -----------------------------

METRICSLINT := uv run --no-sync fastapicommon-metricslint
# promtool from PATH, else this digest-pinned Prometheus image (gincommon's version).
PROMTOOL ?= $(shell command -v promtool 2>/dev/null)
PROMETHEUS_IMAGE ?= prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996
# The platform-gincommon checkout to compare with (gincommon-parity); CI checks out GINCOMMON_REF.
GINCOMMON_DIR ?= ../../platform-gincommon

# The vendored registry is self-consistent (gincommon's `metricslint registry`).
metrics-registry:
	$(METRICSLINT) registry

# The reference rules use only registered platform_* names, and no legacy one.
metrics-refs:
	$(METRICSLINT) refs --deny-deprecated docker/prometheus/rules

# promtool check + unit tests of the reference recording rules and SLO burn-rate alerts.
metrics-rules:
ifneq ($(PROMTOOL),)
	cd docker/prometheus/rules && $(PROMTOOL) check rules platform-*.yml && $(PROMTOOL) test rules promtool-tests.yml
else
	docker run --rm --entrypoint /bin/sh -v $(CURDIR)/docker/prometheus/rules:/rules:ro -w /rules $(PROMETHEUS_IMAGE) \
		-c 'promtool check rules platform-*.yml && promtool test rules promtool-tests.yml'
endif

# Regenerate docs/observability/metric-registry.md from the vendored registry.
metrics-inventory:
	$(METRICSLINT) inventory -o docs/observability/metric-registry.md

# validate-quality's "Observability standard": registry, refs, rules, and inventory drift.
metrics-lint: metrics-registry metrics-refs metrics-rules
	@$(METRICSLINT) inventory | diff -u docs/observability/metric-registry.md - \
		|| (echo "docs/observability/metric-registry.md is stale: run make metrics-inventory"; exit 1)

# Go is the source of truth: registry.json and the reference rules are byte-identical to
# gincommon's, and the generated inventory matches gincommon's metric-registry.md.
gincommon-parity:
	bash .github/scripts/gincommon-parity.sh "$(GINCOMMON_DIR)"

# Public-API diff (griffe) against the previous stable release tag. Without API_NEW_VERSION
# it only warns (ci.yml); with API_NEW_VERSION=vX.Y.Z it fails on a breaking change unless
# vX.Y.Z may break (a MAJOR bump, or a MINOR bump while 0.y.z) — release.yml.
api-compat:
	bash .github/scripts/api-compat.sh $(API_NEW_VERSION)

# Run before tagging: the package version equals TAG and CHANGELOG.md has a dated section for
# it. release.yml's verify job runs both, plus the git checks of verify-release-tag.sh.
release-guard:
	@test -n "$(TAG)" || { echo "usage: make release-guard TAG=vX.Y.Z" >&2; exit 1; }
	uv run --no-project --with 'packaging==26.2' python .github/scripts/release_guard.py $(TAG)
	RELEASE_TAG=$(TAG) bash .github/scripts/verify-changelog-entry.sh

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

# CI's "Validate / Test / test": one pass with coverage data (.coverage + coverage.xml) and no
# threshold here — .github/scripts/coverage-gate.sh enforces fail_under as a separate step.
test-ci:
	uv run pytest tests/unit tests/integration --cov-fail-under=0

# Coverage gate (>=90%) is enforced by pyproject.toml's [tool.coverage.report] fail_under,
# applied automatically via pytest's default addopts — this target just also opens an
# HTML report.
cover: test-all
	uv run coverage html
	@echo "HTML coverage report: htmlcov/index.html"

# -----------------------------
# BUILD
# -----------------------------

# The build backend is installed from build-constraints.txt (exact versions + hashes), so a
# wheel never builds with a backend resolved at build time. Then twine check --strict, and the
# wheel installs into a throwaway venv and reports pyproject.toml's version
# (validate-quality's "Build check").
build:
	@echo "Building wheel + sdist..."
	@rm -rf dist
	uv build --build-constraints build-constraints.txt --require-hashes
	@ls dist/*.whl dist/*.tar.gz
	uv run --no-project --with 'twine==7.0.0' twine check --strict dist/*
	@echo "Verifying the wheel installs..."
	@venv=$$(mktemp -d) && trap 'rm -rf "$$venv"' EXIT && \
		uv venv --quiet --python 3.12 "$$venv/venv" && \
		VIRTUAL_ENV="$$venv/venv" uv pip install --quiet dist/*.whl && \
		cd "$$venv" && "$$venv/venv/bin/python" -c "import importlib.metadata, fastapicommon; \
			import fastapicommon.bootstrap, fastapicommon.problem, fastapicommon.metrics; \
			assert fastapicommon.__version__ == importlib.metadata.version('platform-fastapicommon'), fastapicommon.__version__; \
			print('fastapicommon', fastapicommon.__version__)"
	@echo "OK"

# Re-lock build-constraints.txt after changing [build-system].requires in pyproject.toml.
build-constraints:
	uv run --no-project python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['build-system']['requires'], sep='\n')" \
		| uv pip compile - --universal --python-version 3.12 --generate-hashes \
			--custom-compile-command 'make build-constraints' -o build-constraints.txt

# -----------------------------
# RUN (example usage)
# -----------------------------

# Runs the example FastAPI service on the host so OTLP export reaches the
# otel-collector (localhost:4317) and Prometheus can scrape host.docker.internal.
# The library writes JSON logs to LOG_FILE as well (as gincommon does) for Promtail to tail.
# These are set as real environment variables (not just Make variables) because
# PlatformSettings() and the logging / tracing setup read them from the process environment —
# scoped to this target only, see the note in the CONFIG section above.
run: export APP_NAME := $(APP_NAME)
run: export APP_ENV := $(APP_ENV)
run: export OBSERVABILITY_DOMAIN := $(OBSERVABILITY_DOMAIN)
run: export OTEL_EXPORTER_OTLP_ENDPOINT := $(OTEL_EXPORTER_OTLP_ENDPOINT)
run: export LOG_FILE := $(LOG_FILE)
run: setup
	@echo "Running example FastAPI service on :$(APP_PORT) (logs -> stderr and $(LOG_FILE))..."
	uv run uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port $(APP_PORT) --reload

# -----------------------------
# DOCKER (EXAMPLE SERVICE IMAGE)
# -----------------------------

docker-build:
	@echo "Building Docker image (version: $(BUILD_VERSION))..."
	docker build -t $(APP_NAME):local --build-arg APP_NAME=$(APP_NAME) --build-arg BUILD_VERSION=$(BUILD_VERSION) \
		--build-arg SOURCE_DATE_EPOCH=$$(git log -1 --format=%ct) .

# Requires hadolint (brew install hadolint). Matches the "Hadolint" step of CI's
# "Build image (cache)" job (hadolint-action's default threshold, info).
lint-docker:
	hadolint --failure-threshold info Dockerfile

# Requires trivy (brew install trivy). Matches CI's "Trivy CVE scan" gate:
# fails on CRITICAL/HIGH/UNKNOWN vulnerabilities that have an available fix.
scan:
	docker build -t $(APP_NAME):scan .
	trivy image --severity CRITICAL,HIGH,UNKNOWN --ignore-unfixed --exit-code 1 --scanners vuln $(APP_NAME):scan

# CI's "Smoke tests" job: build the image, run .github/scripts/smoke-tests.sh (size, startup
# gate, /health, platform_* metrics, Problem Details, non-root, pip absent), then tests/e2e
# against the same image.
smoke-test:
	docker build -q --build-arg BUILD_VERSION=$(BUILD_VERSION) -t $(CI_IMAGE) . >/dev/null
	IMAGE=$(CI_IMAGE) bash .github/scripts/smoke-tests.sh
	E2E_IMAGE_TAG=$(CI_IMAGE) uv run pytest tests/e2e -v -m e2e --no-cov

# Re-resolve every tagged Dockerfile base image (FROM <image>:<tag>) to its current digest and
# rewrite the FROM lines as <image>:<tag>@sha256:<digest> — the format validate-quality's
# "Dockerfile base image digest check" requires. Review with `git diff Dockerfile`, rebuild
# (make smoke-test scan) and commit.
pin-base-images:
	@echo "Resolving Dockerfile base-image digests (docker buildx imagetools inspect)..."
	@refs=$$(sed -nE 's/^FROM ([^ @]+:[^ @]+)(@sha256:[0-9a-f]+)?([[:space:]].*)?$$/\1/p' Dockerfile | sort -u); \
	[ -n "$$refs" ] || { echo "FAIL: no tagged FROM lines found in Dockerfile" >&2; exit 1; }; \
	for ref in $$refs; do \
	  digest=$$(docker buildx imagetools inspect "$$ref" --format '{{.Manifest.Digest}}') || exit 1; \
	  case "$$digest" in sha256:*) ;; *) echo "FAIL: no digest resolved for $$ref" >&2; exit 1 ;; esac; \
	  re=$$(printf '%s' "$$ref" | sed 's/[.]/\\./g'); \
	  sed -i.bak -E "s#^FROM $$re(@sha256:[0-9a-f]+)?([[:space:]]|$$)#FROM $$ref@$$digest\\2#" Dockerfile && rm -f Dockerfile.bak; \
	  echo "  pinned  $$ref@$$digest"; \
	done; \
	if grep -E '^FROM [^ @]+:[^ @]+( AS .+)?$$' Dockerfile; then echo "FAIL: unpinned FROM line(s) above" >&2; exit 1; fi

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
# CI (mirrors .github/workflows/validate-quality.yml + validate-test.yml; the image jobs —
# build-image, trivy, smoke — are `make lint-docker scan smoke-test`)
# -----------------------------

ci: lock-check pip-check format-check lint typecheck metrics-lint docs-check ci-scripts-test vulncheck build
	$(MAKE) test-ci
	bash .github/scripts/coverage-gate.sh

# -----------------------------
# CLEAN
# -----------------------------

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build *.egg-info
	rm -rf logs/*.log
