#!/usr/bin/env bash
#
# Generate one holodeck truth dataset for the accuracy benchmark.
#
# Deterministic (everything is seeded). Given a reference FASTA, an output
# directory, and a dataset kind, writes exactly the files the workflow resolves
# from a sample's S3 `source` prefix — fixed, bare filenames so staging is a
# plain `aws s3 cp <out-dir>/ s3://<bucket>/data/sim/<name>/` with no renames:
#
#   <out-dir>/truth.vcf           mutate truth — the SNVs eval scores for
#                                 representation (indels are simulated for
#                                 realism but eval only scores substitutions)
#   <out-dir>/r1.fq.gz            simulated R1 (truth encoded in read names)
#   <out-dir>/r2.fq.gz            simulated R2
#   <out-dir>/golden.bam          ground-truth alignment (with Bismark meth tags
#                                 for the meth kinds), used as eval's --truth
#   <out-dir>/cpg-truth.bedGraph  per-CpG coverage-weighted methylation truth
#                                 (meth kinds only), used as eval's --cpg-truth
#
# These names match `Sample.fastq_names` (r1.fq.gz / r2.fq.gz) and the
# `_truth_inputs` helper (golden.bam / truth.vcf / cpg-truth.bedGraph) in the
# workflow. `holodeck simulate` writes fixed-suffix names (<prefix>.r1.fastq.gz,
# <prefix>.r2.fastq.gz, <prefix>.golden.bam), so we run it against an internal
# prefix and rename its outputs to the canonical names below.
#
# Kinds:
#   wgs-place   genome-wide low coverage — placement / MAPQ calibration
#   wgs-vars    depth over a target BED — variant representation
#   meth-place  EM-seq, genome-wide low coverage — placement + methylation
#   meth-vars   EM-seq, depth over a target BED — variant + methylation
#
# The reference is always full hg38; -vars datasets bound depth with a target
# BED rather than a reduced reference, so mismapping to off-target loci is still
# observable. `holodeck` must be on PATH (the bench Docker image installs it).

set -euo pipefail

usage() {
    echo "usage: $0 <ref.fa> <out-dir> <kind> [seed] [targets.bed]" >&2
    echo "  kind: wgs-place | wgs-vars | meth-place | meth-vars" >&2
    echo "  targets.bed is required for the -vars kinds" >&2
    exit 2
}

[ $# -ge 3 ] || usage
REF=$1
OUT_DIR=$2
KIND=$3
SEED=${4:-42}
BED=${5:-}

case "$KIND" in
    wgs-place | wgs-vars | meth-place | meth-vars) ;;
    *) usage ;;
esac

# -vars datasets need depth over a bounded region to exercise variant
# representation; -place datasets sweep the genome at low coverage to probe
# placement and MAPQ calibration across diverse loci.
case "$KIND" in
    *-vars)
        [ -n "$BED" ] || { echo "error: $KIND requires a targets BED (5th argument)" >&2; exit 2; }
        coverage=(--coverage 30 --targets "$BED")
        ;;
    *-place)
        # ~0.5x genome-wide => ~5M read pairs over hg38, the bench-scale
        # placement/MAPQ probe per the design spec. The point is breadth across
        # diverse/hard loci, not depth, so coverage stays well below 1x.
        coverage=(--coverage 0.5)
        ;;
esac

mkdir -p "$OUT_DIR"
# Internal prefix for holodeck's fixed-suffix outputs, inside the out dir.
prefix="$OUT_DIR/_sim"

truth_vcf="$OUT_DIR/truth.vcf"
echo ">> mutate -> $truth_vcf (seed=$SEED)" >&2
holodeck mutate \
    --reference "$REF" \
    --output "$truth_vcf" \
    --snp-rate 0.001 \
    --indel-rate 0.0001 \
    --seed "$SEED"

# Non-meth kinds simulate directly from the truth VCF. Meth kinds first add
# per-CpG MT/MB methylation truth (carrying the SNVs through), then simulate
# under EM-seq chemistry and emit the coverage-weighted cpg-truth bedGraph.
sim_vcf="$truth_vcf"
meth=()
case "$KIND" in
    meth-*)
        # `holodeck methylate` (and simulate's methylation path) require PHASED
        # genotypes for allele-specific methylation, but `mutate` emits unphased
        # `0/1`. Phase the truth VCF in place — deterministically assign every
        # het allele to haplotype 1 by switching the GT separator `/`→`|` (GT is
        # mutate's only FORMAT field, so it is exactly column 10). The staged
        # truth.vcf is the phased one, consistent with the golden BAM's
        # per-haplotype (hp:i) tags that simulate stamps from it.
        echo ">> phase $truth_vcf (unphased GT -> phased for methylate)" >&2
        phased_tmp="$OUT_DIR/_truth.phased.vcf"
        mawk 'BEGIN{FS=OFS="\t"} /^#/{print; next} {gsub("/","|",$10); print}' \
            "$truth_vcf" > "$phased_tmp"
        mv "$phased_tmp" "$truth_vcf"

        meth_vcf="$OUT_DIR/_meth.vcf.gz"
        echo ">> methylate -> $meth_vcf" >&2
        holodeck methylate \
            --reference "$REF" \
            --vcf "$truth_vcf" \
            --output "$meth_vcf" \
            --seed "$SEED"
        sim_vcf="$meth_vcf"
        meth=(--methylation-mode em-seq --cpg-truth-bedgraph "$OUT_DIR/cpg-truth.bedGraph")
        ;;
esac

echo ">> simulate -> $prefix.{r1,r2}.fastq.gz + $prefix.golden.bam" >&2
holodeck simulate \
    --reference "$REF" \
    --vcf "$sim_vcf" \
    --output "$prefix" \
    --golden-bam \
    --seed "$SEED" \
    "${coverage[@]}" \
    "${meth[@]}"

# Rename holodeck's fixed-suffix outputs to the workflow's canonical bare names.
mv "$prefix.r1.fastq.gz" "$OUT_DIR/r1.fq.gz"
mv "$prefix.r2.fastq.gz" "$OUT_DIR/r2.fq.gz"
mv "$prefix.golden.bam" "$OUT_DIR/golden.bam"

echo ">> done: $KIND (seed=$SEED) -> $OUT_DIR/{r1.fq.gz,r2.fq.gz,golden.bam,truth.vcf$(
    [ "${#meth[@]}" -gt 0 ] && printf ',cpg-truth.bedGraph'
)}" >&2
