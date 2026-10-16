#!/usr/bin/env bash
# Exports the BuildKit SLSA provenance attestation of the pushed image
# (build-push-action provenance: mode=max) to provenance.slsa.json.
#
# BuildKit stores provenance as an attestation manifest inside the image
# index, NOT as a cosign attestation, so it is read with `docker buildx
# imagetools inspect` (Docker's documented method). `.Provenance` is keyed by
# platform for an index; a single-platform image yields one entry.
# Ported verbatim from the Go repo.
set -euo pipefail

: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"
PLATFORM="${PLATFORM:-linux/amd64}"

IMAGE_REF="${IMAGE_NAME}@${IMAGE_DIGEST}"

docker buildx imagetools inspect "${IMAGE_REF}" --format '{{json .Provenance}}' > provenance.raw.json

if ! jq -e --arg p "${PLATFORM}" '
      if type != "object" then empty
      elif has("SLSA") then .SLSA
      elif has($p) then .[$p].SLSA
      else (to_entries[0].value.SLSA) end
      | select(. != null)' provenance.raw.json > provenance.slsa.json; then
  echo "::error title=Provenance export::No SLSA provenance found on ${IMAGE_REF} — was the image built with provenance: mode=max?"
  exit 1
fi
rm -f provenance.raw.json

echo "  ✔  exported SLSA provenance for ${PLATFORM} to provenance.slsa.json ($(wc -c < provenance.slsa.json) bytes)"
