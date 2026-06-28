#!/usr/bin/env bash
#
# Generate every holodeck truth dataset the accuracy benchmark consumes, and
# (optionally) stage them to S3 where `submit --target accuracy` resolves them.
#
# This is the single source of truth for the dataset matrix: which datasets
# exist, what kind each is, and which target region the -vars datasets sample.
# Per-kind coverage is fixed inside gen_holodeck_dataset.sh (-place 0.5x ->
# ~5M read pairs genome-wide; -vars 30x over the target BED). Everything is
# seeded (default 42), so regenerating reproduces byte-identical inputs.
#
#   Dataset              Kind        Coverage  Target
#   sim-wgs-place         wgs-place   0.5x      genome-wide
#   sim-meth-place        meth-place  0.5x      genome-wide                    (EM-seq)
#   sim-wgs-vars          wgs-vars    30x       scripts/sim-targets/chr22.bed
#   sim-meth-vars         meth-vars   30x       scripts/sim-targets/chr22.bed  (EM-seq)
#   sim-smoke-vars        wgs-vars    30x       scripts/sim-targets/smoke.bed
#   sim-smoke-meth-vars   meth-vars   30x       scripts/sim-targets/smoke.bed  (EM-seq)
#
# These names match the `source: data/sim/<name>/` prefixes in
# config/samples.yaml. The *-genomic meth arms (sim-meth-place-genomic,
# sim-meth-vars-genomic, sim-smoke-meth-vars-genomic) reuse a sibling's source,
# so they are graded against the same artifacts and are not generated here. The
# full datasets sweep / depth-sample full hg38; the smoke datasets are the same
# kinds over a small chr22 BED (scripts/sim-targets/smoke.bed) so accuracy_smoke
# has fast, cheap inputs — still the full reference, so off-target mismapping
# stays observable.
#
# Requirements: `holodeck` on PATH (build fg-labs/holodeck at the
# docker/build-arg-defaults.env `HOLODECK_REF`, or extract it from the bench
# image), `samtools` only if you need to (re)build the reference index, and AWS
# credentials when staging.
#
# Usage:
#   scripts/gen_all_sim_datasets.sh <ref.fa> <out-root> [seed] [s3-dest]
#
#   <ref.fa>   full hg38 reference (indexed: .fai + .dict)
#   <out-root> local directory to write <name>/ subdirs into
#   seed       RNG seed for mutate/methylate/simulate (default: 42)
#   s3-dest    optional, e.g. s3://fg-bwa-mem3-bench/data/sim — when given,
#              each dataset's canonical files (excluding _* intermediates) are
#              copied to <s3-dest>/<name>/
#
# Example (generate + stage):
#   scripts/gen_all_sim_datasets.sh \
#       ~/work/references/hg38/Homo_sapiens_assembly38.fasta \
#       /tmp/sim 42 s3://fg-bwa-mem3-bench/data/sim

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GEN="$SCRIPT_DIR/gen_holodeck_dataset.sh"
BED="$SCRIPT_DIR/sim-targets/chr22.bed"
SMOKE_BED="$SCRIPT_DIR/sim-targets/smoke.bed"

usage() {
    echo "usage: $0 <ref.fa> <out-root> [seed] [s3-dest]" >&2
    exit 2
}

[ $# -ge 2 ] || usage
REF=$1
OUT_ROOT=$2
SEED=${3:-42}
S3=${4:-}

command -v holodeck >/dev/null 2>&1 || {
    echo "error: holodeck not on PATH (build fg-labs/holodeck at the HOLODECK_REF pin)" >&2
    exit 1
}
[ -f "$GEN" ] || { echo "error: missing $GEN" >&2; exit 1; }
[ -f "$BED" ] || { echo "error: missing target BED $BED" >&2; exit 1; }
[ -f "$SMOKE_BED" ] || { echo "error: missing smoke target BED $SMOKE_BED" >&2; exit 1; }
# When staging is requested, fail now rather than after a long generation run.
[ -z "$S3" ] || command -v aws >/dev/null 2>&1 || {
    echo "error: aws not on PATH (required when s3-dest is set)" >&2
    exit 1
}

echo ">> holodeck: $(holodeck --version 2>&1 | head -1)  seed=$SEED" >&2

# Generate one dataset and, if an S3 destination was given, stage its canonical
# files (the `_*` intermediates stay local).
gen_one() {
    local name=$1 kind=$2 bed=${3:-}
    echo ">> generate $name ($kind)${bed:+ target=$bed}" >&2
    if [ -n "$bed" ]; then
        "$GEN" "$REF" "$OUT_ROOT/$name" "$kind" "$SEED" "$bed"
    else
        "$GEN" "$REF" "$OUT_ROOT/$name" "$kind" "$SEED"
    fi
    if [ -n "$S3" ]; then
        echo ">> stage $name -> ${S3%/}/$name/" >&2
        aws s3 cp "$OUT_ROOT/$name/" "${S3%/}/$name/" --recursive --exclude '_*'
    fi
}

# Full datasets (decisive accuracy run): full-hg38 sweep / chr22-wide depth.
gen_one sim-wgs-place  wgs-place
gen_one sim-meth-place meth-place
gen_one sim-wgs-vars   wgs-vars  "$BED"
gen_one sim-meth-vars  meth-vars "$BED"

# Smoke datasets (fast accuracy_smoke wiring check): same kinds over a small
# chr22 BED. The *-genomic smoke arm shares sim-smoke-meth-vars' source, so it
# is not generated separately.
gen_one sim-smoke-vars      wgs-vars  "$SMOKE_BED"
gen_one sim-smoke-meth-vars meth-vars "$SMOKE_BED"

echo ">> ALL DONE (seed=$SEED)${S3:+ staged to ${S3%/}/}" >&2
