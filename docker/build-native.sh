#!/usr/bin/env bash
set -euo pipefail

FG_LABS_SHA="${1:?usage: build-native.sh <fg-labs-sha> [<upstream-tag>]}"
UPSTREAM_TAG="${2:-v2.2.1}"
# Default platform is docker's native arch, overridable via $PLATFORM.
# NOTE: On arm64 the image's `bwa-mem2.upstream` is a shim that errors (upstream
# v2.2.1 lacks ARM). Set PLATFORM=linux/amd64 on Mac to build a full upstream
# under Rosetta (slow but complete).
if [[ -z "${PLATFORM:-}" ]]; then
    NATIVE_ARCH=$(docker version --format '{{.Server.Arch}}')
    PLATFORM="linux/${NATIVE_ARCH/x86_64/amd64}"
    PLATFORM="${PLATFORM/aarch64/arm64}"
fi
echo "Building for platform: ${PLATFORM}" >&2

docker buildx build \
    --file docker/Dockerfile \
    --platform "${PLATFORM}" \
    --build-arg UPSTREAM_REPO=https://github.com/bwa-mem2/bwa-mem2 \
    --build-arg UPSTREAM_TAG="${UPSTREAM_TAG}" \
    --build-arg FG_LABS_REPO=https://github.com/fg-labs/bwa-mem3 \
    --build-arg FG_LABS_SHA="${FG_LABS_SHA}" \
    --build-arg SAMTOOLS_VERSION=1.22.1 \
    --build-arg BWA_VERSION=0.7.19 \
    --build-arg BWAMETH_VERSION=0.2.7 \
    --tag "bwa-mem3-bench:${FG_LABS_SHA}-native" \
    --load \
    .
