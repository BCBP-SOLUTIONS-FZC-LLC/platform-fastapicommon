#!/usr/bin/env bash
# Installs the environment validate-test.yml's `lowest-deps` job tests: every
# RUNTIME dependency of pyproject.toml at the lowest version its range admits
# (what a consumer with old pins gets), and the dev tools resolved around them.
# The counterpart of platform-pgcommon-py's asyncpg-compat job, for every
# runtime dependency instead of one.
#
# `uv lock --resolution lowest-direct` is not used: it lowers the dev group too
# (pyyaml 6.0 no longer builds; old pytest / mypy break the suite for reasons no
# consumer sees), and installing runtime floors over the locked environment
# breaks pairs such as opentelemetry-api / -sdk, which must match exactly. So:
#
#   1. resolve the runtime dependencies alone with lowest-direct (uv picks the
#      lowest mutually compatible set, e.g. boto3 1.34.70+ for aioboto3 13.0);
#   2. pin those direct dependencies as constraints and resolve the dev group
#      around them (the dev tools follow, e.g. opentelemetry-sdk == the API),
#      preferring uv.lock's versions for everything else — the output file is
#      seeded with the locked set, which uv pip compile treats as preferences —
#      so a new pytest or mypy release cannot turn this job red on its own;
#   3. install that into a fresh .venv, plus the project itself (editable).
#
# Run from the repository root; then `uv run --no-sync pytest`. Needs uv; the
# Python is uv's default (CI: setup-uv's python-version, 3.12).
set -euo pipefail

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Direct runtime dependency names, lower-cased (constraints match by name).
uv run --no-project python - > "$work/direct.txt" <<'PY'
import re
import tomllib

with open("pyproject.toml", "rb") as f:
    for req in tomllib.load(f)["project"]["dependencies"]:
        print(re.match(r"[A-Za-z0-9._-]+", req).group(0).lower().replace("_", "-"))
PY

uv pip compile pyproject.toml --resolution lowest-direct --no-header --no-annotate --quiet \
  -o "$work/runtime-lowest.txt"
grep -iE "^($(paste -sd '|' "$work/direct.txt"))==" "$work/runtime-lowest.txt" > "$work/floors.txt"

echo "::group::Runtime dependencies at their floors"
cat "$work/floors.txt"
echo "::endgroup::"

uv export --frozen --all-groups --no-emit-project --no-hashes --no-header --quiet \
  -o "$work/lowest.txt"
uv pip compile pyproject.toml --group dev --constraint "$work/floors.txt" \
  --no-header --quiet -o "$work/lowest.txt"

rm -rf .venv
uv venv --quiet
uv pip install --quiet -r "$work/lowest.txt"
uv pip install --quiet --no-deps -e .
uv pip check

# The floors must be what is installed (a dev tool must not have pulled one up).
uv run --no-sync python - "$work/floors.txt" <<'PY'
import importlib.metadata
import sys

bad = []
for line in open(sys.argv[1]):
    name, want = line.strip().split("==")
    have = importlib.metadata.version(name)
    if have != want:
        bad.append(f"{name}: installed {have}, floor {want}")
if bad:
    sys.exit("lowest-deps: " + "; ".join(bad))
print("  ✔  every runtime dependency is installed at its floor")
PY
