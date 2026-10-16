#!/usr/bin/env bash
# Fails the release if CHANGELOG.md has no section for RELEASE_TAG
# (a prerelease tag may use its base version's section — see
# changelog-section.sh), or if that section is still undated
# ("## [X.Y.Z] - Unreleased", this repo's placeholder until release day) or
# empty. A pre-release tag (v0.3.0-rc.1) that falls back to its base version's
# section may find it still undated — the final release sets the date — so the
# date check applies only to a stable tag or a pre-release's own section. Ported from platform-pgcommon-py (and its Go repo); the date and
# empty-body checks are this repo's.
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"

# shellcheck source-path=SCRIPTDIR source=changelog-section.sh
source "$(dirname "$0")/changelog-section.sh"

if ! section=$(changelog_section_version "$RELEASE_TAG"); then
  echo "::error file=CHANGELOG.md::Missing entry for ${RELEASE_TAG#v} — add a '## [${RELEASE_TAG#v}]' section (a prerelease may use its base version's section)"
  exit 1
fi

heading=$(awk -v h="## [${section}]" 'index($0, h) == 1 { print; exit }' CHANGELOG.md)
if [ "$section" != "${RELEASE_TAG#v}" ]; then
  echo "  ✔  pre-release ${RELEASE_TAG} uses its base section [${section}] (${heading#"## [${section}]"}); the date is checked at the final release"
elif ! [[ "$heading" =~ ^"## [${section}] - "[0-9]{4}-[0-9]{2}-[0-9]{2}([[:space:]]|$) ]]; then
  echo "::error file=CHANGELOG.md::Section '${heading}' is not dated — set it to '## [${section}] - YYYY-MM-DD' before tagging"
  exit 1
fi

# Captured first: `… | grep -q` under pipefail fails a long section with SIGPIPE.
body=$(changelog_section_body "$section")
if ! grep -q '[^[:space:]]' <<< "$body"; then
  echo "::error file=CHANGELOG.md::Section [${section}] is empty"
  exit 1
fi
echo "  ✔  changelog entry for ${RELEASE_TAG#v} found in section [${section}]"
