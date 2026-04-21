#!/usr/bin/env bash
# End-to-end smoke test for the Snakemake workflow — runs `smoke-1M` through
# stage → align (fg-labs + baseline) → name-sort → compare locally.
#
# Prereqs (run once — see Plan 2 Task 9):
#   - Plan 1 image `bwa-mem3-bench:<sha>-native` loaded in Docker.
#   - /tmp/bmm3-plan2-smoke/ staged with fastq + chr17 reference + bwa index.
#
# Usage: ./scripts/local-smoke.sh <fg-labs-sha>
set -euo pipefail

FG_LABS_SHA="${1:?usage: $0 <fg-labs-sha> (must exist as bwa-mem3-bench:<sha>-native)}"
SMOKE_DIR="/tmp/bmm3-plan2-smoke"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "${SMOKE_DIR}/scratch/data/smoke-1M/r1.fq.gz" ]]; then
    echo "error: ${SMOKE_DIR}/scratch/data/smoke-1M/r1.fq.gz missing" >&2
    echo "run the prep steps from Plan 2 Task 9 first" >&2
    exit 1
fi
if [[ ! -f "${SMOKE_DIR}/scratch/references/hs38DH/hs38DH.fa.0123" ]]; then
    echo "error: reference index missing at ${SMOKE_DIR}/scratch/references/hs38DH/" >&2
    echo "run 'bwa-mem2.fg-labs index' via docker first (see Plan 2 Task 9)" >&2
    exit 1
fi

# Use bwa-mem2.fg-labs for the baseline on arm64 (bwa-mem2.upstream is a shim).
# Both query and baseline use fg-labs here — proves pipeline wiring; expect 100% concordance.
export BASELINE_BINARY="bwa-mem2.fg-labs"

echo "=== smoke: fg_labs_sha=${FG_LABS_SHA} ==="
echo "=== smoke: SMOKE_DIR=${SMOKE_DIR} ==="
echo "=== smoke: BASELINE_BINARY=${BASELINE_BINARY} ==="

# Run snakemake from SMOKE_DIR so that $PWD in shell rules matches the Docker volume
# mounts (-v $PWD/scratch:/scratch, -v $PWD/runs:/runs, etc.).
# The Snakefile is read from the repo; the bwa_mem3_bench package is editable-installed.
pixi --manifest-path "${REPO_ROOT}/pixi.toml" run snakemake \
    -j2 \
    -s "${REPO_ROOT}/workflow/Snakefile" \
    --directory "${SMOKE_DIR}" \
    --config fg_labs_sha="${FG_LABS_SHA}" \
             samples=smoke-1M \
             archs=c8g \
             reps=1 \
             image_tag="bwa-mem3-bench:${FG_LABS_SHA}-native"

echo
echo "=== vs-baseline.json ==="
cat "${SMOKE_DIR}/runs/${FG_LABS_SHA}/smoke-1M/c8g/rep-1/compare/vs-baseline.json"
