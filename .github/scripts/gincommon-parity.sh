#!/usr/bin/env bash
# Go is the source of truth for the Enterprise Platform Observability Standard: this repo
# vendors platform-gincommon's Platform Observability Registry and reference Prometheus rules
# byte for byte, and renders the same Markdown inventory from them. Fails unless, against the
# gincommon checkout in $1 (validate-quality.yml checks out GINCOMMON_REF):
#
#   1. src/fastapicommon/metrics/registry.json == internal/obsregistry/registry.json;
#   2. docker/prometheus/rules/* == gincommon's docker/prometheus/rules/* (same file set);
#   3. docs/observability/metric-registry.md == gincommon's, except for the source path in
#      the generated-file comment.
#
# Run from the repository root (`make gincommon-parity GINCOMMON_DIR=...`).
set -euo pipefail

GO_DIR="${1:?usage: gincommon-parity.sh GINCOMMON_DIR}"
status=0

fail() {
  echo "::error file=$1,title=gincommon parity::$2"
  status=1
}

if ! cmp -s src/fastapicommon/metrics/registry.json "${GO_DIR}/internal/obsregistry/registry.json"; then
  fail src/fastapicommon/metrics/registry.json \
    "registry.json differs from platform-gincommon's internal/obsregistry/registry.json — re-copy it, then run 'make metrics-inventory'"
fi

ours=$(cd docker/prometheus/rules && ls)
theirs=$(cd "${GO_DIR}/docker/prometheus/rules" && ls)
if [ "${ours}" != "${theirs}" ]; then
  fail docker/prometheus/rules "the rule files differ from gincommon's: ours [${ours//$'\n'/ }], gincommon [${theirs//$'\n'/ }]"
else
  for f in ${ours}; do
    if ! cmp -s "docker/prometheus/rules/${f}" "${GO_DIR}/docker/prometheus/rules/${f}"; then
      fail "docker/prometheus/rules/${f}" "differs from platform-gincommon's docker/prometheus/rules/${f} — re-copy it"
    fi
  done
fi

# The inventories differ only in the generated-from path.
if ! diff -u \
  <(sed 's#src/fastapicommon/metrics/registry.json#internal/obsregistry/registry.json#' docs/observability/metric-registry.md) \
  "${GO_DIR}/docs/observability/metric-registry.md"; then
  fail docs/observability/metric-registry.md "differs from platform-gincommon's docs/observability/metric-registry.md (see the diff above)"
fi

if [ "${status}" -eq 0 ]; then
  echo "  ✔  registry.json, docker/prometheus/rules and metric-registry.md match platform-gincommon (${GO_DIR})"
fi
exit "${status}"
