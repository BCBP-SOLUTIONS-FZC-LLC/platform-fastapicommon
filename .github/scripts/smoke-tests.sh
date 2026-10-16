#!/usr/bin/env bash
# Image size + behaviour smoke tests for the CI-built example service image
# (examples/fastapi_service). Invoked by ci.yml / release.yml and
# `make smoke-test` (keeps shell operators out of inline YAML run blocks).
# Ported from platform-gincommon's smoke-tests.sh (same gates: size, fail-fast
# on an invalid observability identity, /health, platform_* metrics, RFC 9457
# not-found) plus the Python image gates of platform-eventcommon's (installed
# version, pip stripped, numeric non-root UID). Run from the repository root:
# the expected library version is read from pyproject.toml.
set -euo pipefail

IMAGE="${IMAGE:-platform-fastapicommon-ci-test}"
NAME="fastapicommon-smoke-$$"
PORT="${SMOKE_PORT:-18080}"

# GNU timeout exists on the CI runners; fall back to gtimeout (Homebrew
# coreutils) or no timeout so the script also runs on a developer's Mac.
if ! command -v timeout >/dev/null; then
  if command -v gtimeout >/dev/null; then
    timeout() { gtimeout "$@"; }
  else
    timeout() { shift; "$@"; }
  fi
fi

cleanup() { docker rm -f "${NAME}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "::group::Image size check (linux/amd64)"
# .Size is the UNCOMPRESSED size on CI's runners (classic image store) but the
# compressed size under Docker Desktop's containerd store (about 60 MB here), so
# a local run is far more lenient than CI. Measured uncompressed (linux/amd64):
# python:3.14-slim is ~120 MB, plus fastapi/starlette/pydantic, grpcio (16 MB,
# needed by the OTLP exporter), opentelemetry and plain uvicorn: ~154 MB. The
# limit (about 10% headroom) catches regressions: build tooling, uv, pip, tests,
# a virtualenv or uvicorn[standard] leaking into the runtime stage. It is above
# gincommon's 150 MB, which budgets a Go binary, not a Python runtime.
MAX_MB=170
size=$(docker image inspect "${IMAGE}" --format='{{.Size}}')
mb=$((size / 1024 / 1024))
echo "Image size: ${mb} MB (limit: ${MAX_MB} MB)"
echo "::endgroup::"
if [ "${mb}" -gt "${MAX_MB}" ]; then
  echo "::error file=Dockerfile,title=Image size::Image is ${mb} MB, exceeds ${MAX_MB} MB limit — check COPY instructions, the runtime stage and .dockerignore"
  exit 1
fi

echo "::group::Image gates"
# 1. The installed library is this commit's version, and the example app
#    module is importable (the entrypoint is overridden, so nothing starts).
want=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
ver=$(timeout 30s docker run --rm --entrypoint python "${IMAGE}" \
        -c 'import fastapicommon; print(fastapicommon.__version__)')
echo "fastapicommon in the image: ${ver} (pyproject.toml: ${want})"
if [ "${ver}" != "${want}" ]; then
  echo "::error file=pyproject.toml,title=Version gate::the image has fastapicommon ${ver}, expected ${want}"
  exit 1
fi

# 2. A HEALTHCHECK is defined.
health=$(docker image inspect "${IMAGE}" --format='{{if .Config.Healthcheck}}{{json .Config.Healthcheck.Test}}{{end}}')
echo "healthcheck: ${health:-<none>}"
if [ -z "${health}" ]; then
  echo "::error file=Dockerfile,title=Healthcheck gate::the image must define a HEALTHCHECK"
  exit 1
fi

# 3. pip is stripped from the runtime image (its vendored packages show up as
#    CVEs and nothing at runtime uses it).
rc=0
timeout 30s docker run --rm --entrypoint python "${IMAGE}" -c 'import pip' >/dev/null 2>&1 || rc=$?
if [ "${rc}" -eq 0 ]; then
  echo "::error file=Dockerfile,title=pip gate::pip is importable in the runtime image — the Dockerfile must remove it"
  exit 1
fi
echo "pip: absent"

# 4. The image runs as non-root, with a numeric UID (Kubernetes can only
#    enforce runAsNonRoot for a numeric user).
user=$(docker image inspect "${IMAGE}" --format='{{.Config.User}}')
echo "image user: ${user:-<root>}"
if [ -z "${user}" ] || [ "${user}" = "root" ] || [ "${user}" = "0" ] || [ "${user%%:*}" = "0" ]; then
  echo "::error file=Dockerfile,title=Non-root gate::image must set a non-root USER"
  exit 1
fi
case "${user%%:*}" in
  ''|*[!0-9]*)
    echo "::error file=Dockerfile,title=Numeric UID gate::USER must be a numeric UID (got ${user}) so runAsNonRoot can be enforced"
    exit 1
    ;;
esac
echo "::endgroup::"

echo "::group::Startup gate — invalid identity must fail fast"
exit_code=0
gate_log=$(timeout 30s docker run --rm -e OBSERVABILITY_DOMAIN=not-a-domain "${IMAGE}" 2>&1) || exit_code=$?
echo "Container exit code: ${exit_code} (expected non-zero, not 124/timeout)"
# Assert the reason too, so an unrelated crash cannot pass the gate.
if [ "${exit_code}" -eq 0 ] || [ "${exit_code}" -eq 124 ] ||
  ! grep -q 'is not in the registry vocabulary' <<< "${gate_log}"; then
  echo "${gate_log}" | tail -5
  echo "::error file=src/fastapicommon/metrics/platform.py,title=Startup gate::An unknown OBSERVABILITY_DOMAIN must stop the service at startup with the registry error"
  exit 1
fi
echo "::endgroup::"

echo "::group::Serve — /health, platform metrics, Problem Details"
docker run -d --name "${NAME}" -p "127.0.0.1:${PORT}:8080" \
  -e OBSERVABILITY_DOMAIN=iam -e APP_ENV=prod -e BUILD_VERSION=smoke \
  -e OTLP_ENDPOINT=127.0.0.1:1 \
  "${IMAGE}" >/dev/null
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:${PORT}/health" | grep -q '"ok"'; then
  docker logs "${NAME}" || true
  echo "::error file=examples/fastapi_service/main.py,title=Health::/health did not return ok"
  exit 1
fi
metrics=$(curl -fsS "http://127.0.0.1:${PORT}/metrics")
for want_metric in \
  'platform_build_info{domain="iam",environment="prod",service="platform-fastapicommon-example",version="smoke"} 1' \
  'platform_http_requests_total{domain="iam",environment="prod",error_class="success",method="GET",'; do
  if ! grep -qF "${want_metric}" <<< "${metrics}"; then
    echo "::error title=Metrics::/metrics is missing: ${want_metric}"
    exit 1
  fi
done
# RFC 9457: an unregistered route is a platform/not-found problem, not Starlette's 404.
problem_body=$(mktemp)
problem_status=$(curl -sS -o "${problem_body}" -w '%{http_code} %{content_type}' \
  "http://127.0.0.1:${PORT}/no-such-route")
if [ "${problem_status}" != "404 application/problem+json" ] \
  || ! grep -qF '"type":"https://errors.company.com/platform/not-found"' "${problem_body}"; then
  echo "::error title=Problem Details::unknown route answered '${problem_status}': $(cat "${problem_body}")"
  exit 1
fi
echo "::endgroup::"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Smoke test results"
    echo "- ✅ fastapicommon \`${ver}\` installed; HEALTHCHECK defined; pip absent; runs as non-root (\`${user}\`)"
    echo "- ✅ Startup gate: invalid OBSERVABILITY_DOMAIN exits ${exit_code}"
    echo "- ✅ /health ok; /metrics exposes platform_build_info and platform_http_requests_total with domain/service/environment"
    echo "- ✅ Unknown route answers an RFC 9457 platform/not-found problem (application/problem+json)"
    echo "- 📦 Image size (linux/amd64): **${mb} MB** (limit: ${MAX_MB} MB)"
    echo "- 🔍 [Security (Trivy SARIF results)](https://github.com/${GITHUB_REPOSITORY}/security/code-scanning)"
  } >> "$GITHUB_STEP_SUMMARY"
fi
