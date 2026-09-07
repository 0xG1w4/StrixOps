#!/usr/bin/env bash
# Build the shared StrixOps sandbox image. Deploy-time artifact — the engine only
# checks image presence at run time.
#
#   bash containers/build-images.sh [tag]
#
# * strixops-sandbox:<tag> — upstream web sandbox + internal-network tool layer

set -euo pipefail

TAG="${1:-1.3.0}"
BASE="${BASE_IMAGE:-ghcr.io/usestrix/strix-sandbox:${TAG}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> pulling base image ${BASE}"
docker pull "${BASE}"

echo ">> building strixops-sandbox:${TAG}"
docker build \
  --build-arg BASE_IMAGE="${BASE}" \
  -t "strixops-sandbox:${TAG}" \
  -f "${HERE}/Dockerfile.sandbox" \
  "${HERE}"

echo ">> done: strixops-sandbox:${TAG}"
