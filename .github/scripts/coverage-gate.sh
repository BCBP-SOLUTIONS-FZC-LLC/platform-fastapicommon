#!/usr/bin/env bash
# Enforces minimum total test coverage on the .coverage data written by
# `make test-ci` (one pytest run over unit + integration + e2e, line + branch
# coverage of src/fastapicommon). Ported from platform-pgcommon-py.
#
# The threshold has one source: pyproject.toml's
# [tool.coverage.report].fail_under (90), which a bare `uv run pytest` also
# enforces locally. COVERAGE_THRESHOLD overrides it (tests of this script).
# What this adds over pytest-cov's own check: the gate is a separate step
# (a failing test and a coverage drop are told apart), it annotates the
# failure, and it outputs pct= for the PR summary.
set -euo pipefail

test -f .coverage || {
  echo "::error file=pyproject.toml,title=Coverage gate::.coverage missing — run 'make test-ci' before the coverage gate"
  exit 1
}

THRESHOLD="${COVERAGE_THRESHOLD:-$(uv run --no-sync python -c \
  "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['tool']['coverage']['report']['fail_under'])")}"

echo "::group::Coverage report summary"
uv run --no-sync coverage report --skip-covered --skip-empty --fail-under=0 | tail -n 25
pct=$(uv run --no-sync coverage report --format=total --precision=2)
echo "Total coverage: ${pct}%"
echo "::endgroup::"

# Emit before the gate check so the value is available even when coverage fails.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "pct=${pct}" >> "$GITHUB_OUTPUT"
fi

gate=$(awk -v p="$pct" -v t="$THRESHOLD" 'BEGIN { print (p+0 < t+0) ? "FAIL" : "OK" }')
if [ "${gate}" = "FAIL" ]; then
  echo "::error file=pyproject.toml,title=Coverage gate::Coverage is ${pct}% — below the ${THRESHOLD}% threshold. Run 'make cover' locally to identify gaps."
  exit 1
fi
echo "  ✔  coverage ${pct}% ≥ ${THRESHOLD}%"
