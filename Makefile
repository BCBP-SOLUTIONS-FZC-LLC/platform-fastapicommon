# -----------------------------
# CONFIG
# -----------------------------
-include .env

APP_NAME      ?= platform-fastapicommon-example
APP_ENV       ?= dev
APP_PORT      ?= 8080
BUILD_VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

# Example service + local observability stack (docker-compose.yml). The ports are read by
# `docker compose --env-file .env`; the Make variables are only for the messages below.
OBSERVABILITY_DOMAIN        ?= iam
LOG_FILE                    ?= logs/app.log
OTEL_EXPORTER_OTLP_ENDPOINT ?= localhost:4317
OTEL_COLLECTOR_GRPC_PORT    ?= 4317
OTEL_COLLECTOR_HTTP_PORT    ?= 4318
TEMPO_HTTP_PORT             ?= 3200
LOKI_HTTP_PORT              ?= 3100
PROMETHEUS_HTTP_PORT        ?= 9090
GRAFANA_HTTP_PORT           ?= 3000
PROMETHEUS_SCRAPE_TARGET    ?= host.docker.internal:$(APP_PORT)

# Deliberately NOT exported file-wide: `docker compose --env-file .env` reads these
# directly from .env, and every other recipe below interpolates $(VAR) at Make-expansion
# time rather than reading the shell environment. A blanket `export` here would leak
# APP_NAME/OTEL_EXPORTER_OTLP_ENDPOINT into `make test`'s pytest subprocess and silently override
# PlatformSettings' defaults in tests that assert on them. Only `run` (and `show-config`) need
# these as real environment variables, so they're set inline on those recipes only.

# uv manages everything: .venv from uv.lock (the dev group carries ruff, black, mypy, pytest,
# pip-audit, griffe, pre-commit), so every tool runs as `uv run <tool>` — no global installs.
UV ?= uv

# Everything ruff and black check in CI (validate-quality.yml's lint and format steps).
LINT_PATHS := src tests examples

# Test suites (pytest markers `integration` / `e2e` are applied by each suite's conftest).
# unit runs without an app; integration wires create_platform_app() in-process; e2e builds and
# runs the real Docker image (requires Docker), so CI runs it in the smoke job, not test-ci.
TEST_UNIT_DIR := tests/unit
TEST_INT_DIR  := tests/integration
TEST_E2E_DIR  := tests/e2e
TEST_CI_DIRS  := $(TEST_UNIT_DIR) $(TEST_INT_DIR)
TEST_ALL_DIRS := $(TEST_UNIT_DIR) $(TEST_INT_DIR) $(TEST_E2E_DIR)

# Image names: IMAGE_NAME is the local tag of the example service image (CI sets its own
# ghcr.io name in the environment for its jobs, none of which builds through this Makefile);
# CI_IMAGE is the tag CI's smoke job builds and smoke-tests.sh / tests/e2e run against.
IMAGE_NAME ?= $(APP_NAME)
CI_IMAGE   ?= platform-fastapicommon-ci-test

# Release inputs passed on the command line (release.yml / release checklist):
#   make api-compat API_NEW_VERSION=vX.Y.Z   make release-guard TAG=vX.Y.Z
# COVERAGE_THRESHOLD (optional) overrides pyproject.toml's fail_under in coverage-gate.sh; a
# command-line or environment value reaches the script's environment unchanged.
API_NEW_VERSION ?=
TAG             ?=

# -----------------------------
# SETUP
# -----------------------------

# setup: everything a fresh clone needs — the venv from uv.lock, a .env copied from
# .env-example (never overwrites an existing .env), logs/ for LOG_FILE, and the git hooks.
.PHONY: setup
setup: install
	@test -f .env || cp .env-example .env
	@mkdir -p logs
	@$(MAKE) --no-print-directory install-hooks
	@echo "Environment ready (.venv, .env, logs/, git hooks)"

# install: uv sync only (creates/updates .venv from uv.lock; dev group included by default, plus
# the grpc extra the gRPC interceptors and tests need).
.PHONY: install
install:
	$(UV) sync --extra grpc

# install-hooks: install the pre-commit framework's git hook (.pre-commit-config.yaml: ruff,
# black, mypy, pytest, whitespace). Bypass a single commit with `git commit --no-verify`; undo
# with `uv run pre-commit uninstall`.
.PHONY: install-hooks
install-hooks:
	$(UV) run pre-commit install

.PHONY: help
help:
	@echo "Available commands:"
	@echo "  make setup             - uv sync + copy .env-example to .env if missing + logs/ + install git hooks"
	@echo "  make install           - uv sync only (dev group + grpc extra)"
	@echo "  make install-hooks     - install the pre-commit git hook (ruff, black, mypy, pytest)"
	@echo "  make pin-base-images   - resolve + pin the Dockerfile base images to their current @sha256 digests"
	@echo "  make docs-serve        - browse the package API docs with pydoc (http://localhost:$(PYDOC_PORT))"
	@echo "  make format            - ruff format + black + ruff check --fix (rewrites files; alias: fmt)"
	@echo "  make format-check      - ruff format --check + black --check (no changes; used in CI; alias: fmt-check)"
	@echo "  make typecheck         - mypy --strict"
	@echo "  make lock-check        - uv.lock up to date with pyproject.toml (uv lock --check; Go's tidy drift check)"
	@echo "  make pip-check         - installed .venv has consistent dependencies (uv pip check; Go's mod-verify)"
	@echo "  make vulncheck         - pip-audit against locked dependencies (alias: vuln-check)"
	@echo "  make api-compat        - griffe vs the last release tag (API_NEW_VERSION=vX.Y.Z: fail unless compatible or allowed to break)"
	@echo "  make release-guard     - pre-tag check, TAG=vX.Y.Z: pyproject version == tag + CHANGELOG section exists"
	@echo "  make lint              - ruff check"
	@echo "  make metrics-lint      - observability standard gate (registry, rule refs, promtool, inventory drift)"
	@echo "  make metrics-inventory - regenerate docs/observability/metric-registry.md (alias: metrics-doc)"
	@echo "  make gincommon-parity  - registry, rules, inventory identical to gincommon's (GINCOMMON_DIR=$(GINCOMMON_DIR))"
	@echo "  make docs-check        - every docs/architecture/mermaid/*.mmd embedded byte-identically in ARCHITECTURE.md"
	@echo "  make ci-scripts-test   - regression tests for the CI scripts (detect-changes.sh)"
	@echo "  make lint-docker       - hadolint against the Dockerfile, CI's threshold (alias: hadolint)"
	@echo "  make test              - unit + integration + e2e in one pytest run (requires Docker; alias: test-all)"
	@echo "  make test-ci           - the CI test gate: unit + integration with coverage, no threshold (see cover-check)"
	@echo "  make test-unit         - unit tests only (no Docker, no wired-up app)"
	@echo "  make test-int          - integration tests: create_platform_app() end-to-end in-process (alias: test-integration)"
	@echo "  make test-e2e          - end-to-end tests against the real Docker image (requires Docker)"
	@echo "  make build             - wheel + sdist into dist/ (uv build), twine check, verify the wheel installs"
	@echo "  make build-constraints - re-lock the build backend (pyproject [build-system]) with hashes"
	@echo "  make ci                - lock-check + pip-check + format-check + lint + typecheck + metrics-lint + docs-check + ci-scripts-test + vulncheck + build + test-ci + coverage gate"
	@echo "  make cover             - test + HTML coverage report in htmlcov/"
	@echo "  make cover-func        - per-file coverage report (missing lines) from the last test run"
	@echo "  make cover-check       - test-ci + coverage gate (>= pyproject.toml fail_under, 90%)"
	@echo "  make clean             - remove venv, caches, build artifacts, coverage output and local logs"
	@echo "  make help              - this list"
	@echo ""
	@echo "Example service image (Dockerfile, requires Docker):"
	@echo "  make docker-build      - build the example service image as $(IMAGE_NAME):local"
	@echo "  make scan              - build + Trivy CVE scan (CI's 'Trivy CVE scan' gate; requires trivy)"
	@echo "  make smoke-test        - build the image + CI's smoke-tests.sh + tests/e2e against it (alias: test-smoke-image)"
	@echo ""
	@echo "Example service + local observability stack (docker-compose.yml; requires Docker):"
	@echo "  make run               - run the example FastAPI service on :$(APP_PORT) (uvicorn --reload)"
	@echo "  make show-config       - print PlatformSettings + the resolved tracing config (.env loaded)"
	@echo "  make docker-up         - start otel-collector, tempo, loki, promtail, prometheus, grafana"
	@echo "  make docker-down       - stop and remove the stack's containers"
	@echo "  make docker-reset      - docker-down (volumes included) + docker-up: empty traces, logs and metrics"
	@echo "  make docker-logs       - tail the stack's logs"
	@echo "  make docker-ps         - list the stack's containers"

# pin-base-images: re-resolve every tagged Dockerfile base image (FROM <image>:<tag>, e.g.
# python:3.14-slim and ghcr.io/astral-sh/uv:<version>) to its current digest and rewrite the
# FROM lines as <image>:<tag>@sha256:<digest> — the format validate-quality's "Dockerfile base
# image digest check" requires. The tags are read from the Dockerfile itself, so bumping a tag
# there and running this pins the new one. Prints what changed (or that nothing did); review
# with `git diff Dockerfile`, rebuild (make smoke-test scan) and commit. Note any
# security-fix package pins in the Dockerfile may become droppable after a refresh.
.PHONY: pin-base-images
pin-base-images:
	@echo "Resolving Dockerfile base-image digests (docker buildx imagetools inspect)..."
	@changed=0; \
	refs=$$(sed -nE 's/^FROM ([^ @]+:[^ @]+)(@sha256:[0-9a-f]+)?([[:space:]].*)?$$/\1/p' Dockerfile | sort -u); \
	[ -n "$$refs" ] || { echo "FAIL: no tagged FROM lines found in Dockerfile" >&2; exit 1; }; \
	for ref in $$refs; do \
	  digest=$$(docker buildx imagetools inspect "$$ref" --format '{{.Manifest.Digest}}') || exit 1; \
	  case "$$digest" in sha256:*) ;; *) echo "FAIL: no digest resolved for $$ref" >&2; exit 1 ;; esac; \
	  re=$$(printf '%s' "$$ref" | sed 's/[.]/\\./g'); \
	  stale=$$(grep -E "^FROM $$re(@sha256:[0-9a-f]+)?([[:space:]]|$$)" Dockerfile | grep -Fv "$$ref@$$digest" || true); \
	  if [ -z "$$stale" ]; then echo "  unchanged  $$ref@$$digest"; continue; fi; \
	  old=$$(printf '%s\n' "$$stale" | sed -nE 's/.*@(sha256:[0-9a-f]+).*/\1/p' | sort -u | tr '\n' ' '); \
	  echo "  updated    $$ref: $${old:-unpinned }-> $$digest"; \
	  sed -i.bak -E "s#^FROM $$re(@sha256:[0-9a-f]+)?([[:space:]]|$$)#FROM $$ref@$$digest\\2#" Dockerfile && rm -f Dockerfile.bak; \
	  changed=1; \
	done; \
	if grep -E '^FROM [^ @]+:[^ @]+( AS .+)?$$' Dockerfile; then echo "FAIL: unpinned FROM line(s) above" >&2; exit 1; fi; \
	if [ "$$changed" = 1 ]; then echo "Dockerfile updated — review (git diff Dockerfile), rebuild, and commit"; \
	else echo "Dockerfile already pinned to the current digests — no change"; fi

# docs-serve: browse the API docs locally — the counterpart of the Go repo's `make godoc`
# (pkgsite). Uses the stdlib's pydoc server against the venv (fastapicommon is installed
# editable by uv sync), so it needs no extra dependency; the package docstrings are the
# reference. Navigate to the "fastapicommon" entry. Ctrl-C to stop. Not APP_PORT's default
# (8080), so it can run next to `make run`.
PYDOC_PORT ?= 8081

.PHONY: docs-serve
docs-serve:
	@echo "Starting pydoc at http://localhost:$(PYDOC_PORT)/fastapicommon.html — press Ctrl-C to stop"
	$(UV) run python -m pydoc -p $(PYDOC_PORT) -b

# -----------------------------
# PYTHON BASICS (uv-managed: .venv + uv.lock)
# -----------------------------

# format: rewrite files — ruff's formatter and black (CI checks both), then ruff's autofixable
# lint findings.
.PHONY: format fmt
format:
	$(UV) run ruff format $(LINT_PATHS)
	$(UV) run black $(LINT_PATHS)
	$(UV) run ruff check --fix $(LINT_PATHS)
fmt: format

# format-check: verify formatting without modifying files (validate-quality's "Format check").
.PHONY: format-check fmt-check
format-check:
	$(UV) run ruff format --check $(LINT_PATHS)
	$(UV) run black --check $(LINT_PATHS)
fmt-check: format-check

# typecheck: mypy --strict over the fastapicommon package ([tool.mypy] in pyproject.toml).
.PHONY: typecheck
typecheck:
	$(UV) run mypy

# lock-check: uv.lock records exactly what pyproject.toml asks for (validate-quality's
# "Lock check"; the Go repo's tidy drift check).
.PHONY: lock-check
lock-check:
	$(UV) lock --check

# pip-check: the installed .venv has no missing or conflicting requirements
# (validate-quality's "Verify dependency resolution"; the closest thing to Go's mod-verify —
# uv already verifies the lock's hashes when it installs).
.PHONY: pip-check
pip-check:
	$(UV) pip check

# vulncheck: pip-audit against the locked dependencies (validate-quality's "Vulnerability
# check"). Trivy also scans the image (make scan).
.PHONY: vulncheck vuln-check
vulncheck:
	$(UV) run pip-audit
vuln-check: vulncheck

# api-compat: public-API diff (griffe) against the previous stable release tag. Without
# API_NEW_VERSION it only warns (PRs); with API_NEW_VERSION=vX.Y.Z it fails on a breaking change
# unless vX.Y.Z may break (a MAJOR bump, or a MINOR bump while 0.y.z) — release.yml. griffe
# loads the tag through a temporary git worktree it removes again.
.PHONY: api-compat
api-compat:
	bash .github/scripts/api-compat.sh $(API_NEW_VERSION)

# release-guard: run before tagging — the package version equals TAG (release_guard.py) and
# CHANGELOG.md has its section (verify-changelog-entry.sh). release.yml's verify job runs both,
# plus the git checks (tag at HEAD, on main) of verify-release-tag.sh.
.PHONY: release-guard
release-guard:
	@test -n "$(TAG)" || { echo "usage: make release-guard TAG=vX.Y.Z" >&2; exit 1; }
	$(UV) run --no-project --with 'packaging==26.2' python .github/scripts/release_guard.py $(TAG)
	RELEASE_TAG=$(TAG) bash .github/scripts/verify-changelog-entry.sh

# -----------------------------
# LINT
# -----------------------------

# lint: ruff check (validate-quality's "Lint (ruff)").
.PHONY: lint
lint:
	@echo "Running linter..."
	$(UV) run ruff check $(LINT_PATHS)

# docs-check: every docs/architecture/mermaid/*.mmd is embedded byte-identically in
# ARCHITECTURE.md (validate-quality's "Architecture diagram sync"; docs.yml runs it on
# docs-only changes). No Docker.
.PHONY: docs-check
docs-check:
	bash .github/scripts/docs-mermaid-sync.sh

# ci-scripts-test: regression tests for the CI shell scripts (no bats; scratch git
# repositories): detect-changes.sh's docs-only decision and log sanitising.
# validate-quality.yml runs the same target. No Docker.
.PHONY: ci-scripts-test
ci-scripts-test:
	bash .github/scripts/detect-changes_test.sh

# lint-docker: hadolint against the Dockerfile with CI's threshold (hadolint/hadolint-action
# v3.5.0, default failure threshold info, first step of ci.yml's build-image job). Uses a local
# hadolint (brew install hadolint) when installed, otherwise HADOLINT_IMAGE — keep it on the
# version hadolint-action v3.5.0 bundles (2.15.1), or local and CI results diverge.
HADOLINT_IMAGE ?= ghcr.io/hadolint/hadolint:v2.15.1-debian

.PHONY: lint-docker hadolint
lint-docker:
	@if command -v hadolint >/dev/null 2>&1; then \
	  echo "hadolint $$(hadolint --version)"; \
	  hadolint --failure-threshold info Dockerfile; \
	else \
	  echo "hadolint not installed — using $(HADOLINT_IMAGE)"; \
	  docker run --rm -i $(HADOLINT_IMAGE) hadolint --failure-threshold info - < Dockerfile; \
	fi
hadolint: lint-docker

# -----------------------------
# OBSERVABILITY STANDARD (mirrors platform-gincommon's `make metrics-lint`)
# -----------------------------

METRICSLINT := $(UV) run --no-sync fastapicommon-metricslint
# promtool from PATH, else this digest-pinned Prometheus image (latest; gincommon pins v3.5.0).
PROMTOOL ?= $(shell command -v promtool 2>/dev/null)
PROMETHEUS_IMAGE ?= prom/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e
# The platform-gincommon checkout to compare with (gincommon-parity); CI checks out GINCOMMON_REF.
GINCOMMON_DIR ?= ../../platform-gincommon

# metrics-registry: the vendored registry is self-consistent (gincommon's `metricslint registry`).
.PHONY: metrics-registry
metrics-registry:
	$(METRICSLINT) registry

# metrics-refs: the reference rules use only registered platform_* names, and no legacy one.
.PHONY: metrics-refs
metrics-refs:
	$(METRICSLINT) refs --deny-deprecated docker/prometheus/rules

# metrics-rules: promtool check + unit tests of the reference recording rules and SLO burn-rate
# alerts (a local promtool when installed, otherwise PROMETHEUS_IMAGE).
.PHONY: metrics-rules
metrics-rules:
ifneq ($(PROMTOOL),)
	cd docker/prometheus/rules && $(PROMTOOL) check rules platform-*.yml && $(PROMTOOL) test rules promtool-tests.yml
else
	docker run --rm --entrypoint /bin/sh -v $(CURDIR)/docker/prometheus/rules:/rules:ro -w /rules $(PROMETHEUS_IMAGE) \
		-c 'promtool check rules platform-*.yml && promtool test rules promtool-tests.yml'
endif

# metrics-inventory: regenerate docs/observability/metric-registry.md from the vendored
# registry (metrics-lint fails when it is stale).
.PHONY: metrics-inventory metrics-doc
metrics-inventory:
	$(METRICSLINT) inventory -o docs/observability/metric-registry.md
metrics-doc: metrics-inventory

# metrics-lint: Enterprise Platform Observability Standard gate — registry, rule refs, promtool,
# and the generated inventory being up to date (validate-quality's "Observability standard").
.PHONY: metrics-lint
metrics-lint: metrics-registry metrics-refs metrics-rules
	@$(METRICSLINT) inventory | diff -u docs/observability/metric-registry.md - \
		|| (echo "docs/observability/metric-registry.md is stale: run make metrics-inventory"; exit 1)

# gincommon-parity: Go is the source of truth — registry.json and the reference rules are
# byte-identical to gincommon's, and the generated inventory matches gincommon's
# metric-registry.md (validate-quality checks out GINCOMMON_REF for this).
.PHONY: gincommon-parity
gincommon-parity:
	bash .github/scripts/gincommon-parity.sh "$(GINCOMMON_DIR)"

# -----------------------------
# TESTS
# -----------------------------
# Python has no race detector, so there is no `race` target or -race flag as in the Go repo.
# pyproject.toml's pytest addopts collect coverage and enforce fail_under (90%) on every run, so
# a subset of the suite must pass --no-cov (as test-e2e does: it measures a container, not this
# process).

# test: every suite in one pytest run (verbose; the coverage threshold applies; requires Docker).
.PHONY: test test-all
test:
	$(UV) run pytest $(TEST_ALL_DIRS) -v
test-all: test

# test-ci: the CI test gate (validate-test.yml). One pass over unit + integration with coverage,
# writing .coverage + coverage.xml; the threshold is checked separately by
# .github/scripts/coverage-gate.sh (_coverage-gate), so a failing test and a coverage drop are
# told apart. --cov-fail-under=0 only defers the threshold; a bare `uv run pytest` still
# enforces it. e2e is not part of it: CI runs it against the image in the smoke job.
.PHONY: test-ci
test-ci:
	$(UV) run pytest $(TEST_CI_DIRS) --cov-fail-under=0

# test-unit: unit tests only — no Docker, no wired-up app.
.PHONY: test-unit
test-unit:
	$(UV) run pytest $(TEST_UNIT_DIR) -v

# test-int: create_platform_app() end-to-end in-process (no Docker). --no-cov: this subset
# covers about two thirds of the package, so pyproject.toml's 90% gate (meant for the whole
# suite) would fail it although every test passes; test-ci measures coverage.
.PHONY: test-int test-integration
test-int:
	$(UV) run pytest $(TEST_INT_DIR) -v -m integration --no-cov
test-integration: test-int

# test-e2e: against the real Docker image (requires Docker). --no-cov: the app runs inside a
# container, not in-process, so coverage.py (instrumenting this pytest process) has nothing to
# measure — running it alone would otherwise fail pyproject.toml's 90% gate at 0%.
.PHONY: test-e2e
test-e2e:
	$(UV) run pytest $(TEST_E2E_DIR) -v -m e2e --no-cov

# smoke-test: CI's "Smoke tests" job — build the image, run .github/scripts/smoke-tests.sh
# (size, startup gate, /health, platform_* metrics, Problem Details, non-root, pip absent), then
# tests/e2e against the same image. Note the size gate is more lenient locally: Docker Desktop's
# containerd store reports the compressed size (see smoke-tests.sh).
.PHONY: smoke-test test-smoke-image
smoke-test:
	docker build -q --build-arg BUILD_VERSION=$(BUILD_VERSION) -t $(CI_IMAGE) . >/dev/null
	IMAGE=$(CI_IMAGE) bash .github/scripts/smoke-tests.sh
	E2E_IMAGE_TAG=$(CI_IMAGE) $(UV) run pytest $(TEST_E2E_DIR) -v -m e2e --no-cov
test-smoke-image: smoke-test

# -----------------------------
# BUILD
# -----------------------------

# The build backend and its dependencies are installed from build-constraints.txt (exact
# versions + hashes), so a wheel never builds with a backend resolved at build time.
UV_BUILD := $(UV) build --build-constraints build-constraints.txt --require-hashes

# build: wheel + sdist into dist/, twine check --strict, then install the wheel into a
# throwaway venv and check it imports and reports pyproject.toml's version
# (validate-quality's "Build check").
.PHONY: build
build:
	@echo "Building wheel + sdist (version: $(BUILD_VERSION))..."
	@rm -rf dist
	$(UV_BUILD)
	@ls dist/*.whl dist/*.tar.gz
	$(UV) run --no-project --with 'twine==7.0.0' twine check --strict dist/*
	@echo "Verifying the wheel installs..."
	@venv=$$(mktemp -d) && trap 'rm -rf "$$venv"' EXIT && \
		$(UV) venv --quiet --python 3.12 "$$venv/venv" && \
		VIRTUAL_ENV="$$venv/venv" $(UV) pip install --quiet dist/*.whl && \
		cd "$$venv" && "$$venv/venv/bin/python" -c "import importlib.metadata, fastapicommon; \
			import fastapicommon.bootstrap, fastapicommon.problem, fastapicommon.metrics; \
			assert fastapicommon.__version__ == importlib.metadata.version('platform-fastapicommon'), fastapicommon.__version__; \
			print('fastapicommon', fastapicommon.__version__)"
	@echo "OK"

# build-constraints: re-lock build-constraints.txt after changing [build-system].requires in
# pyproject.toml.
.PHONY: build-constraints
build-constraints:
	$(UV) run --no-project python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['build-system']['requires'], sep='\n')" \
		| $(UV) pip compile - --universal --python-version 3.12 --generate-hashes \
			--custom-compile-command 'make build-constraints' -o build-constraints.txt

# -----------------------------
# DOCKER (EXAMPLE SERVICE IMAGE)
# -----------------------------

# docker-build: the example service image, tagged $(IMAGE_NAME):local, with CI's build args
# (BUILD_VERSION, SOURCE_DATE_EPOCH = commit time).
.PHONY: docker-build
docker-build:
	@echo "Building example service image (version: $(BUILD_VERSION))..."
	docker build --build-arg APP_NAME=$(APP_NAME) --build-arg BUILD_VERSION=$(BUILD_VERSION) \
		--build-arg SOURCE_DATE_EPOCH=$$(git log -1 --format=%ct 2>/dev/null || echo 0) -t $(IMAGE_NAME):local .

# scan: requires trivy (brew install trivy). Matches CI's "Trivy CVE scan" gate: fails on
# CRITICAL/HIGH/UNKNOWN vulnerabilities that have an available fix.
.PHONY: scan
scan:
	docker build -t $(IMAGE_NAME):scan .
	trivy image --severity CRITICAL,HIGH,UNKNOWN --ignore-unfixed --exit-code 1 --scanners vuln $(IMAGE_NAME):scan

# -----------------------------
# RUN (EXAMPLE SERVICE)
# -----------------------------

# run: the example FastAPI service on the host, so OTLP export reaches the otel-collector
# (localhost:4317) and Prometheus can scrape host.docker.internal. The library writes JSON logs
# to LOG_FILE as well (as gincommon does) for Promtail to tail. These are set as real
# environment variables (not just Make variables) because PlatformSettings() and the logging /
# tracing setup read them from the process environment — scoped to this target only, see the
# note in the CONFIG section above.
.PHONY: run
run: export APP_NAME := $(APP_NAME)
run: export APP_ENV := $(APP_ENV)
run: export OBSERVABILITY_DOMAIN := $(OBSERVABILITY_DOMAIN)
run: export OTEL_EXPORTER_OTLP_ENDPOINT := $(OTEL_EXPORTER_OTLP_ENDPOINT)
run: export LOG_FILE := $(LOG_FILE)
run:
	@test -f .env || cp .env-example .env
	@mkdir -p logs
	@echo "Running example FastAPI service on :$(APP_PORT) (logs -> stderr and $(LOG_FILE))..."
	$(UV) run uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port $(APP_PORT) --reload

# -----------------------------
# DOCKER (LOCAL OBSERVABILITY STACK)
# -----------------------------
# otel-collector, tempo, loki, promtail, prometheus and grafana for trying the library's logs,
# metrics and traces by hand with `make run`; the test suites need none of it.
COMPOSE := docker compose --env-file .env

# prometheus-config: render docker/prometheus/prometheus.yml from its template with the scrape
# target (the example service on the host by default).
.PHONY: prometheus-config
prometheus-config:
	@sed 's|__PROMETHEUS_SCRAPE_TARGET__|$(PROMETHEUS_SCRAPE_TARGET)|g' docker/prometheus/prometheus.yml.tpl > docker/prometheus/prometheus.yml

# docker-up: start the stack (copies .env-example to .env first if missing).
.PHONY: docker-up
docker-up: prometheus-config
	@test -f .env || cp .env-example .env
	@mkdir -p logs
	@echo "Starting observability stack (otel-collector, tempo, loki, promtail, prometheus, grafana)..."
	$(COMPOSE) up -d
	@echo "Grafana:    http://localhost:$(GRAFANA_HTTP_PORT) (admin/admin by default)"
	@echo "Prometheus: http://localhost:$(PROMETHEUS_HTTP_PORT)"
	@echo "Now run 'make run' in another terminal to start the example service."

# docker-down: stop and remove the stack's containers (their data volumes are kept; see
# docker-reset). Only this repo's compose project is touched.
.PHONY: docker-down
docker-down:
	@echo "Stopping observability stack..."
	$(COMPOSE) down --remove-orphans

# docker-reset: start over with empty traces, logs and metrics — docker-down with the volumes,
# then docker-up.
.PHONY: docker-reset
docker-reset:
	$(COMPOSE) down -v --remove-orphans
	@$(MAKE) --no-print-directory docker-up

.PHONY: docker-logs
docker-logs:
	$(COMPOSE) logs -f

# docker-ps: the stack's containers, with state and ports.
.PHONY: docker-ps
docker-ps:
	$(COMPOSE) ps -a

# -----------------------------
# CI
# -----------------------------

# ci: the same gates CI enforces (validate-quality.yml + validate-test.yml), in one local
# command. The image jobs — lint-docker, scan, smoke-test — have their own targets, api-compat
# is informational on PRs, and gincommon-parity needs a gincommon checkout (GINCOMMON_DIR). The
# test gate and its coverage check are sequenced in the recipe so `make -j ci` can't check a
# .coverage before test-ci wrote it. Run this before pushing.
.PHONY: ci
ci: lock-check pip-check format-check lint typecheck metrics-lint docs-check ci-scripts-test vulncheck build
	@$(MAKE) --no-print-directory test-ci
	@$(MAKE) --no-print-directory _coverage-gate

# -----------------------------
# COVERAGE
# -----------------------------
# The threshold has one source: pyproject.toml's [tool.coverage.report] fail_under (90).

# cover: every suite (the threshold applies; requires Docker) + an HTML report.
.PHONY: cover
cover: test
	$(UV) run coverage html
	@echo "HTML coverage report: htmlcov/index.html"

# cover-func: per-file report with missing lines from the last run's .coverage (no test run;
# the Go repo's per-function `go tool cover -func`). Never fails on the threshold.
.PHONY: cover-func
cover-func:
	@test -f .coverage || { echo "FAIL: .coverage missing — run 'make test-ci' (or any test target) first" >&2; exit 1; }
	$(UV) run coverage report -m --fail-under=0

# cover-check: run the suites with coverage, then fail below pyproject.toml's fail_under.
# Sequenced in the recipe (not as prerequisites) so `make -j` can't run the gate first.
.PHONY: cover-check
cover-check:
	@$(MAKE) --no-print-directory test-ci
	@$(MAKE) --no-print-directory _coverage-gate

# _coverage-gate: check an existing .coverage against the threshold (no test run). Used by
# cover-check and ci.
.PHONY: _coverage-gate
_coverage-gate:
	bash .github/scripts/coverage-gate.sh

# -----------------------------
# DEBUG HELPERS
# -----------------------------

# show-config: what the library resolves from the environment with .env loaded — the
# PlatformSettings fields and the tracing configuration create_platform_app would install
# (endpoint, sampling, insecure, ...), plus the log level — without starting an app.
define SHOW_CONFIG_PY
from fastapicommon.bootstrap.app import _tracing_config
from fastapicommon.config import PlatformSettings
from fastapicommon.logging.config import is_dev_env, resolve_level
import logging

settings = PlatformSettings()
for name, value in settings.model_dump().items():
    print(f"{name:28} {value!r}")
environment = settings.environment.strip().lower()
level = resolve_level(settings.log_level or None, is_dev_env(environment))
print(f"{'(log level)':28} {logging.getLevelName(level).lower()}")
tracing = _tracing_config(settings, environment)
for name in ("service_name", "service_version", "domain", "environment", "endpoint",
             "insecure", "sample_ratio", "enable_baggage", "ignore_remote_parent_sampled"):
    print(f"{'tracing.' + name:28} {getattr(tracing, name)!r}")
endef
export SHOW_CONFIG_PY

.PHONY: show-config
show-config:
	@set -a; if [ -f .env ]; then . ./.env; fi; set +a; $(UV) run python -c "$$SHOW_CONFIG_PY"

# -----------------------------
# CLEAN
# -----------------------------

.PHONY: clean
clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build *.egg-info SHA256SUMS
	rm -rf logs/*.log
