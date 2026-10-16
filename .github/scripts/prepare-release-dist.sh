#!/usr/bin/env bash
# Release `dist` job: builds the wheel and sdist ONCE — the exact files that
# are uploaded to PyPI and attached to the GitHub Release — and proves they
# are publishable. Ported from platform-pgcommon-py (the Go repos'
# prepare-release-binary.sh: one pure-Python wheel replaces the binaries).
#
#   1. uv build with the hash-locked build backend (build-constraints.txt) and
#      SOURCE_DATE_EPOCH (commit time): a reproducible wheel and sdist.
#   2. release_guard.py --dist: both artifacts' metadata carry the tag's version.
#   3. twine check: the metadata/README render as PyPI requires.
#   4. the wheel installs into a clean venv, reports the release version
#      (fastapicommon.__version__) and its modules import.
#   5. SHA256SUMS over dist/* (bare file names, as the release assets are
#      flat): `sha256sum --check SHA256SUMS` next to the downloaded files.
#      release-checksums.sh re-verifies it after every artifact hand-off.
#
# Run from the repository root. Inputs (env): RELEASE_TAG; SOURCE_DATE_EPOCH
# (defaults to the HEAD commit time).
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
here=$(cd "$(dirname "$0")" && pwd)

echo "Building wheel + sdist for ${RELEASE_TAG} (SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH})"
rm -rf dist
uv build --build-constraints build-constraints.txt --require-hashes --out-dir dist

uv run --no-project --with 'packaging==26.2' python "$here/release_guard.py" "$RELEASE_TAG" --dist dist

uv run --no-project --with 'twine==7.0.0' twine check --strict dist/*

venv=$(mktemp -d)
trap 'rm -rf "$venv"' EXIT
uv venv --quiet --python 3.12 "$venv/venv"
VIRTUAL_ENV="$venv/venv" uv pip install --quiet dist/*.whl
installed=$("$venv/venv/bin/python" -c 'import fastapicommon; print(fastapicommon.__version__)')
want=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
if [ "$installed" != "$want" ]; then
  echo "::error title=Installed version::the wheel installs fastapicommon ${installed}, expected ${want}"
  exit 1
fi
(cd "$venv" && "$venv/venv/bin/python" -c 'import fastapicommon.bootstrap, fastapicommon.problem, fastapicommon.metrics')
echo "  ✔  wheel installs fastapicommon ${installed}; its modules import"

(cd dist && sha256sum -- * > ../SHA256SUMS)
echo "  ✔  SHA256SUMS:"
cat SHA256SUMS
