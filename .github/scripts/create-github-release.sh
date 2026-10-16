#!/usr/bin/env bash
# Creates the GitHub release with the wheel and sdist (the files uploaded to
# PyPI), SHA256SUMS (plus its Cosign Sigstore bundle), the image's SLSA
# provenance and its SBOM attached. SHA256SUMS comes from the dist job, is
# re-verified by release-checksums.sh and signed by release.yml before this
# runs. Ported from the Go repo (binaries → wheel + sdist).
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"

shopt -s nullglob
DISTS=(dist/*.whl dist/*.tar.gz)
if [ "${#DISTS[@]}" -ne 2 ]; then
  echo "::error::expected one wheel and one sdist in dist/, found ${#DISTS[@]} file(s)"
  exit 1
fi

for f in release-notes.md SHA256SUMS SHA256SUMS.sigstore.json provenance.slsa.json sbom.cyclonedx.json; do
  test -f "$f" || {
    echo "::error::Release asset missing: ${f}"
    exit 1
  }
done

PRERELEASE_FLAG=""
if echo "$RELEASE_TAG" | grep -q -- '-'; then
  PRERELEASE_FLAG="--prerelease"
fi

DIGEST_SHORT="${IMAGE_DIGEST#sha256:}"
DIGEST_SHORT="${DIGEST_SHORT:0:12}"
RELEASE_TITLE="${RELEASE_TAG} · sha256:${DIGEST_SHORT}"

# shellcheck disable=SC2086 # PRERELEASE_FLAG is empty or one flag
gh release create "$RELEASE_TAG" \
  --title "$RELEASE_TITLE" \
  --notes-file release-notes.md \
  "${DISTS[@]}" \
  SHA256SUMS \
  SHA256SUMS.sigstore.json \
  provenance.slsa.json \
  sbom.cyclonedx.json \
  $PRERELEASE_FLAG
