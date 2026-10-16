#!/usr/bin/env bash
# Content fingerprint of a local Docker image, used by ci.yml and release.yml to
# prove the image pushed to GHCR is the image trivy scanned and smoke tested.
# Ported verbatim from the Go repo.
#
#   image-fingerprint.sh IMAGE                 → prints and outputs fingerprint=
#   image-fingerprint.sh IMAGE EXPECTED...     → also fails unless every
#                                                EXPECTED equals the fingerprint
#
# The fingerprint is the sha256 of a canonical JSON of what the gates judged:
# the filesystem (rootfs diff_ids, i.e. uncompressed layer digests, so registry
# compression does not matter), os/architecture, and the runtime config
# (User, Entrypoint, Cmd, Env, WorkingDir, ExposedPorts). Labels are excluded
# on purpose: push adds docker/metadata-action's OCI labels (including a
# wall-clock `created`), which changes the config digest but neither what
# trivy scans nor how the CLI behaves. A manifest or image-ID comparison
# would therefore always differ; this one differs only if the content does.
#
# The image must be in the local Docker store (`load: true`, or `docker pull`
# of the pushed digest), so both sides are read with the same tool.
set -euo pipefail

IMAGE=${1:?usage: image-fingerprint.sh IMAGE [EXPECTED_FINGERPRINT...]}
shift

canonical=$(docker image inspect "$IMAGE" | jq -cS '.[0] | {
    os: .Os,
    architecture: .Architecture,
    diff_ids: .RootFS.Layers,
    user: (.Config.User // ""),
    entrypoint: (.Config.Entrypoint // []),
    cmd: (.Config.Cmd // []),
    env: (.Config.Env // []),
    workdir: (.Config.WorkingDir // ""),
    exposed_ports: ((.Config.ExposedPorts // {}) | keys)
  }')

if command -v sha256sum >/dev/null; then
  fingerprint=$(printf '%s' "$canonical" | sha256sum | cut -d' ' -f1)
else
  fingerprint=$(printf '%s' "$canonical" | shasum -a 256 | cut -d' ' -f1)
fi

echo "::group::Image fingerprint of ${IMAGE}"
jq . <<< "$canonical"
echo "::endgroup::"
echo "fingerprint: sha256:${fingerprint}"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "fingerprint=sha256:${fingerprint}" >> "$GITHUB_OUTPUT"
fi

status=0
for expected in "$@"; do
  if [ -z "$expected" ]; then
    echo "::error title=Image fingerprint::an expected fingerprint is empty — did the trivy / smoke job record one?"
    status=1
  elif [ "$expected" != "sha256:${fingerprint}" ]; then
    echo "::error title=Image mismatch::${IMAGE} is sha256:${fingerprint}, but the gated image was ${expected} — the image is not the one that was scanned and smoke-tested"
    status=1
  else
    echo "  ✔  matches gated image ${expected}"
  fi
done
exit "$status"
