#!/usr/bin/env bash
# Extracts RELEASE_TAG's section from CHANGELOG.md (via changelog-section.sh,
# the resolver verify-changelog-entry.sh gated the release on) into
# release-notes.md and appends the PyPI install line plus the example-service
# image metadata. Ported from platform-pgcommon-py (Go: `go get` line).
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${IMAGE_VERSION:?IMAGE_VERSION is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"

# shellcheck source-path=SCRIPTDIR source=changelog-section.sh
source "$(dirname "$0")/changelog-section.sh"

VERSION="${RELEASE_TAG#v}"
if ! SECTION=$(changelog_section_version "$RELEASE_TAG"); then
  echo "::error file=CHANGELOG.md::Missing entry for ${VERSION}"
  exit 1
fi
# Plain redirects, not `| tee file | true`: under pipefail, `true` exits
# without reading, tee can die of SIGPIPE and fail the whole release.
changelog_section_body "$SECTION" > release-notes.md

if [ ! -s release-notes.md ] || ! grep -q '[^[:space:]]' release-notes.md; then
  echo "::error file=CHANGELOG.md::Section [${SECTION}] for ${VERSION} is empty"
  exit 1
fi
if [ "$SECTION" != "$VERSION" ]; then
  release_notes_prefix="> Release candidate ${RELEASE_TAG} of ${SECTION}."
fi

# The PyPI version is pyproject.toml's (PEP 440: tag v0.3.0-rc.1 is 0.3.0rc1;
# release_guard.py has checked they are the same version).
PYPI_VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")

{
  if [ -n "${release_notes_prefix:-}" ]; then
    echo "${release_notes_prefix}"
    echo ""
  fi
  cat release-notes.md
  echo ""
  echo "---"
  echo "**PyPI:** \`pip install platform-fastapicommon==${PYPI_VERSION}\` · \`uv add platform-fastapicommon==${PYPI_VERSION}\` (<https://pypi.org/project/platform-fastapicommon/${PYPI_VERSION}/>)"
  echo "**Example service image:** \`${IMAGE_NAME}:${IMAGE_VERSION}\`"
  echo "**Digest:** \`${IMAGE_DIGEST}\`"
  echo "**Verify:** the image and \`SHA256SUMS\` (wheel + sdist) are Cosign keyless-signed (bundle: \`SHA256SUMS.sigstore.json\`); see VERSIONING.md's *Verifying a release* section for the commands."
} > release-notes.md.tmp
mv release-notes.md.tmp release-notes.md
cat release-notes.md
