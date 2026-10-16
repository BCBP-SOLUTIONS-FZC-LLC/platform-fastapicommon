#!/usr/bin/env bash
# Records the HEAD commit time as SOURCE_DATE_EPOCH (GITHUB_ENV and
# GITHUB_OUTPUT): the one timestamp every image build (and the release wheel /
# sdist) of a run shares, so they are reproducible. Ported from the Go repo.
set -euo pipefail
SDE=$(git log -1 --format=%ct)
echo "SOURCE_DATE_EPOCH=${SDE}" >> "$GITHUB_ENV"
echo "SOURCE_DATE_EPOCH=${SDE}" >> "$GITHUB_OUTPUT"
