#!/usr/bin/env bash
# Re-verifies SHA256SUMS (written by prepare-release-dist.sh in the `dist`
# job) against the downloaded dist/ artifact, proving the artifact hand-off did
# not alter a file, and that SHA256SUMS covers exactly the files in dist/.
# Run before the PyPI upload (pypi-publish) and before SHA256SUMS is
# Cosign-signed and attached to the GitHub Release (publish). Ported from the
# Go repo's release-checksums.sh (there the aggregate is written here; here it
# is written once, next to the build, and only verified downstream).
#
# Format matches `sha256sum --check SHA256SUMS` run next to the release assets.
set -euo pipefail

DIST="${DIST:-dist}"
SUMS="$(pwd)/${SUMS:-SHA256SUMS}"

if command -v sha256sum >/dev/null; then
  sha256() { sha256sum "$@"; }
else
  sha256() { shasum -a 256 "$@"; }
fi

test -s "$SUMS" || {
  echo "::error title=Checksums::$(basename "$SUMS") missing or empty — did the dist artifact download?"
  exit 1
}

shopt -s nullglob
files=("$DIST"/*)
if [ "${#files[@]}" -eq 0 ]; then
  echo "::error title=Checksums::no files in ${DIST}/ — did the dist artifact download?"
  exit 1
fi

(cd "$DIST" && sha256 --check --strict "$SUMS")

listed=$(awk '{ sub(/^\*/, "", $2); print $2 }' "$SUMS" | sort)
present=$(cd "$DIST" && printf '%s\n' * | sort)
if [ "$listed" != "$present" ]; then
  echo "::error title=Checksums::$(basename "$SUMS") does not list exactly the files in ${DIST}/"
  diff <(echo "$listed") <(echo "$present") || true
  exit 1
fi

for f in "${files[@]}"; do
  case "$f" in
    *.whl | *.tar.gz) ;;
    *) echo "::error title=Checksums::unexpected file ${f} in ${DIST}/ (only the wheel and the sdist are released)"; exit 1 ;;
  esac
done
echo "  ✔  $(basename "$SUMS") covers the ${#files[@]} files in ${DIST}/"
