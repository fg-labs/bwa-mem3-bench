#!/usr/bin/env bash
set -euo pipefail

FG_LABS_SHA="${1:-}"
UPSTREAM_TAG="${2:-v2.2.1}"
IMAGE_NAME="${IMAGE_NAME:-bwa-mem3-bench}"

if [[ -z "$FG_LABS_SHA" ]]; then
    echo "usage: pixi run build-docker <fg-labs-sha> [<upstream-tag>]" >&2
    exit 2
fi

docker buildx build \
    --file docker/Dockerfile \
    --platform linux/amd64,linux/arm64 \
    --build-arg UPSTREAM_REPO=https://github.com/bwa-mem2/bwa-mem2 \
    --build-arg UPSTREAM_TAG="${UPSTREAM_TAG}" \
    --build-arg FG_LABS_REPO=https://github.com/fg-labs/bwa-mem3 \
    --build-arg FG_LABS_SHA="${FG_LABS_SHA}" \
    --build-arg SAMTOOLS_VERSION=1.22.1 \
    --build-arg BWA_VERSION=0.7.19 \
    --build-arg BWAMETH_VERSION=0.2.7 \
    --tag "${IMAGE_NAME}:${FG_LABS_SHA}" \
    .
