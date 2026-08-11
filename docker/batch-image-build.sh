#!/bin/sh
# Build one architecture of a bench image inside an AWS Batch job, and push it.
#
# Runs in a `docker:*-dind` container on a privileged Batch job, submitted to the
# arch-specific queue whose compute environment has native hardware for the
# platform being built. That is the entire point: the amd64 half of a multi-arch
# build on an Apple Silicon laptop runs under QEMU, which for a C++ compile is
# 5-15x slower than native. The queues already exist and scale to zero, so this
# costs nothing between builds.
#
# Two Batch jobs cannot be one `buildx` invocation, so each job pushes a
# single-platform image under a per-arch tag (`<sha>-amd64`, `<sha>-arm64`) and
# the submitter joins them into a manifest list afterwards with
# `docker buildx imagetools create`. See `commands/build_remote.py`.
#
# Required environment (set by the submitter as container overrides):
#   CONTEXT_S3_URI  s3://... location of the build-context tarball
#   ECR_REPO        target repository URI, sans :tag
#   IMAGE_TAG       tag to push (already arch-suffixed)
#   DOCKERFILE      path to the Dockerfile within the context
#   PLATFORM        linux/amd64 | linux/arm64
#   AWS_REGION      region for the ECR login
#   BUILD_ARGS      newline-separated NAME=VALUE pairs
set -eu

log() { echo "[batch-image-build] $*" >&2; }

: "${CONTEXT_S3_URI:?}" "${ECR_REPO:?}" "${IMAGE_TAG:?}" "${DOCKERFILE:?}"
: "${PLATFORM:?}" "${AWS_REGION:?}"

log "platform=${PLATFORM} tag=${IMAGE_TAG} dockerfile=${DOCKERFILE}"
log "uname -m: $(uname -m)"

# Refuse to build the wrong architecture. Batch routes by queue, and a queue
# wired to the wrong compute environment would silently emulate instead --
# producing a correct image very slowly and defeating the reason this exists.
case "${PLATFORM}" in
    linux/amd64) want=x86_64 ;;
    linux/arm64) want=aarch64 ;;
    *) log "unsupported PLATFORM=${PLATFORM}"; exit 2 ;;
esac
if [ "$(uname -m)" != "${want}" ]; then
    log "ERROR: PLATFORM=${PLATFORM} needs a ${want} host, but this host is $(uname -m)."
    log "The job went to a queue whose compute environment is the wrong arch;"
    log "building here would silently fall back to emulation."
    exit 3
fi

# dockerd is not running in a fresh dind container; the entrypoint is bypassed
# because Batch overrides the command.
log "starting dockerd"
dockerd-entrypoint.sh dockerd >/tmp/dockerd.log 2>&1 &
tries=0
until docker info >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "${tries}" -gt 60 ]; then
        log "dockerd did not come up; last lines of its log:"
        tail -30 /tmp/dockerd.log >&2 || true
        exit 4
    fi
    sleep 1
done
log "dockerd up after ${tries}s"

log "fetching build context from ${CONTEXT_S3_URI}"
mkdir -p /build
aws s3 cp "${CONTEXT_S3_URI}" /tmp/context.tar.gz
tar xzf /tmp/context.tar.gz -C /build
rm -f /tmp/context.tar.gz

log "logging in to ECR"
registry="$(echo "${ECR_REPO}" | cut -d/ -f1)"
aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${registry}"

# Rebuild the --build-arg list from the newline-separated env var. Passed this
# way rather than as individual env vars so the submitter stays the single source
# of truth for the pin set -- the same list the local build path sends.
set -- \
    --file "${DOCKERFILE}" \
    --platform "${PLATFORM}" \
    --provenance=false \
    --tag "${ECR_REPO}:${IMAGE_TAG}" \
    --push
if [ -n "${BUILD_ARGS:-}" ]; then
    # A literal newline as IFS; `read` splits the list one pair per line so a
    # value containing spaces survives intact.
    printf '%s\n' "${BUILD_ARGS}" > /tmp/build-args
    while IFS= read -r pair; do
        [ -n "${pair}" ] || continue
        set -- "$@" --build-arg "${pair}"
    done < /tmp/build-args
fi

cd /build
log "docker buildx build $*"
docker buildx build "$@" .

log "pushed ${ECR_REPO}:${IMAGE_TAG}"
