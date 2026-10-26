#!/usr/bin/env bash
# API-compatibility gate for the package's public API (`griffe check fastapicommon`).
# Ported from platform-pgcommon-py (the Go repos' api-compat.sh / apidiff).
#
#   api-compat.sh vX.Y.Z[-pre]   release mode: compare with the highest stable
#                                tag below vX.Y.Z and FAIL on any breaking
#                                change unless vX.Y.Z is allowed to break
#                                (see below).
#   api-compat.sh                PR mode: compare with the highest stable tag
#                                merged into HEAD and only WARN (the next
#                                version is not known yet; the release gate
#                                decides).
#
# Allowed to break (VERSIONING.md): a MAJOR bump, and — while the package is
# 0.y.z (SemVer §4, VERSIONING.md's pre-1.0 caveat) — a MINOR bump
# (0.2.x -> 0.3.0). A 0.y.z PATCH release must stay compatible, as must every
# MINOR/PATCH release from 1.0.0 on.
#
# griffe compares the public API of the working tree (uncommitted changes
# included) with the tag: names starting with `_` are private, and re-exports
# are followed to their targets, so a change to a private function that a
# public fastapicommon module re-exports IS reported. That is why the Go
# repo's api-exposed-internal.sh has no counterpart here. griffe loads the tag through a temporary git worktree (and
# a temporary `griffe-<ref>` branch it deletes again), so it needs the full
# tag history (actions/checkout fetch-depth: 0) but no credentials.
#
# GRIFFE ("uv run --no-sync griffe" by default) can be overridden for tests.
set -euo pipefail

GRIFFE=${GRIFFE:-uv run --no-sync griffe}
NEW=${1:-}

stable_re='^v[0-9]+\.[0-9]+\.[0-9]+$'
major() { local v=${1#v}; echo "${v%%.*}"; }
minor() { local v=${1#v}; v=${v#*.}; echo "${v%%.*}"; }

if [ -n "$NEW" ]; then
  if ! [[ "$NEW" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-.+)?$ ]]; then
    echo "::error title=API compatibility::'$NEW' is not a vX.Y.Z[-pre] version"
    exit 1
  fi
  core=${NEW%%-*}
  # Highest stable tag strictly below the release's X.Y.Z (a hotfix of an
  # older line is compared with that line, not with the newest release).
  BASE=$( { git tag --list 'v*' | grep -E "$stable_re" | grep -vxF "$core" || true; echo "$core"; } \
          | sort -V | grep -B1 -xF "$core" | head -n1)
  [ "$BASE" = "$core" ] && BASE=''
  level=error
else
  BASE=$(git tag --merged HEAD --list 'v*' | grep -E "$stable_re" | sort -V | tail -n1 || true)
  level=warning
fi

if [ -z "$BASE" ]; then
  echo "No earlier stable release tag — nothing to compare against."
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# griffe writes its report to stderr, one "path:line: Object: change" line per
# breaking change (--format oneline), interleaved with any log line, and exits
# 1 when it found one. Anything else (e.g. a tree that does not load) must stop
# the gate, with its output visible.
rc=0
# shellcheck disable=SC2086 # GRIFFE is a command line on purpose
$GRIFFE check fastapicommon --against "$BASE" --search src --format oneline --no-color \
  > "$tmp/raw.txt" 2>&1 || rc=$?
grep -E '^[^[:space:]:]+:[0-9]+: ' "$tmp/raw.txt" > "$tmp/breaking.txt" || true
grep -vE '^[^[:space:]:]+:[0-9]+: ' "$tmp/raw.txt" > "$tmp/other.txt" || true
if [ "$rc" -gt 1 ] || { [ "$rc" -eq 1 ] && [ ! -s "$tmp/breaking.txt" ]; } \
   || { [ "$rc" -eq 0 ] && [ -s "$tmp/breaking.txt" ]; }; then
  cat "$tmp/raw.txt" >&2
  echo "::error title=API compatibility::griffe check failed (exit ${rc}) — see the output above"
  exit 1
fi
# `__version__` changes value on every release by design; griffe reports that as a
# breaking attribute change, which would fail every PATCH release.
grep -vE '^[^[:space:]:]+:[0-9]+: __version__: Attribute value was changed' "$tmp/breaking.txt" \
  > "$tmp/filtered.txt" || true
mv "$tmp/filtered.txt" "$tmp/breaking.txt"
if [ -s "$tmp/other.txt" ]; then
  echo "::group::griffe log"
  cat "$tmp/other.txt"
  echo "::endgroup::"
fi

echo "::group::Breaking API changes since ${BASE}"
if [ -s "$tmp/breaking.txt" ]; then cat "$tmp/breaking.txt"; else echo "(none)"; fi
echo "::endgroup::"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### API compatibility vs \`${BASE}\`${NEW:+ (releasing \`${NEW}\`)}"
    echo
    if [ -s "$tmp/breaking.txt" ]; then
      echo '```'
      cat "$tmp/breaking.txt"
      echo '```'
    else
      echo "No breaking public-API change."
    fi
  } >> "$GITHUB_STEP_SUMMARY"
fi

if [ ! -s "$tmp/breaking.txt" ]; then
  echo "No breaking API change since ${BASE}."
  exit 0
fi

if [ -n "$NEW" ]; then
  if [ "$(major "$NEW")" -gt "$(major "$BASE")" ]; then
    echo "Breaking API changes since ${BASE}, allowed by the major-version bump to ${NEW}."
    exit 0
  fi
  if [ "$(major "$NEW")" -eq 0 ] && [ "$(major "$BASE")" -eq 0 ] \
     && [ "$(minor "$NEW")" -gt "$(minor "$BASE")" ]; then
    echo "Breaking API changes since ${BASE}, allowed by the 0.y minor bump to ${NEW} (pre-1.0, VERSIONING.md)."
    exit 0
  fi
fi

# One annotation per change ("path:line: Object: Change").
while IFS= read -r line; do
  [ -n "$line" ] || continue
  echo "::${level} title=Breaking API change since ${BASE}::${line}"
done < "$tmp/breaking.txt"

if [ "$level" = error ]; then
  if [ "$(major "$BASE")" -eq 0 ]; then
    next="v0.$(( $(minor "$BASE") + 1 )).0"
  else
    next="v$(( $(major "$BASE") + 1 )).0.0"
  fi
  echo "${NEW} may not break the public API of ${BASE} (above)."
  echo "Restore compatibility, or release ${next} instead (and document the break in CHANGELOG.md)."
  exit 1
fi
echo "Breaking API changes since ${BASE} (warning only in CI; the release gate fails a release that may not break on them)."
