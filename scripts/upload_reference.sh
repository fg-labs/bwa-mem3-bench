#!/usr/bin/env bash
# Upload the hg38 reference (Homo_sapiens_assembly38.fasta) and bwa-mem2 indexes to S3.
# Usage: REF_ROOT=/path/to/Homo_sapiens_assembly38 bash scripts/upload_reference.sh <bucket> [<which>]
#
# REF_ROOT must point at the directory containing Homo_sapiens_assembly38.fasta
# and the bwa-mem2 indexes (.0123, .amb, .ann, .bwt.2bit.64, .pac, .fai, and
# the matching .dict). See docs/data-setup.md for how to obtain the Broad
# hg38 bundle and build the indexes.
#
# <which> is one of:
#   hg38         — standard bwa-mem2 index (default)
#   hg38-u1      — stride-2 `re-sa -u 1` copy of the standard index, used by the
#                  arena's `> v0.12.0` arms (same DNA sidecar set as hg38, only
#                  .bwt.2bit.64 differs; build with `bwa-mem2.fg-labs re-sa -u 1`). See
#                  docs/data-setup.md "Stride-2 arena index".
#   hg38-u2      — stride-4 `re-sa -u 2` copy, used by the regular sweep +
#                  thread-scaling `> v0.12.0` fg-labs arms (`sweep_dense_sa_shift:
#                  2`; +4 GB, fits the 32 GB sweep hosts). Same sidecar set as
#                  hg38.
#   hg38-meth-u2 — stride-4 copy of the hg38-meth index (only the `.meth` seed's
#                  .bwt.2bit.64 re-sa'd; build with `bwa-mem2.fg-labs re-sa -u 2 <ref>.meth`)
#                  for the meth sweep's `> v0.12.0` arms.
#   hg38-meth    — bwameth.c2t doubled reference + bwa-mem2 index (build with
#                  `bwa-mem2 index --meth <fasta>`; peaks ~150 GB RAM during
#                  FMI construction — use a 256 GB instance.)
#   hg38-minibwa — minibwa-format hg38 index (build with `minibwa index <fasta>`;
#                  produces .l2b + .mbw sidecars). Used only on the local-only
#                  `private/minibwa-bench` branch.
#   all          — hg38 + hg38-meth + hg38-minibwa (the densified copies
#                  hg38-u1 / hg38-u2 / hg38-meth-u2 are excluded: each is a
#                  separately re-sa'd tree with its own REF_ROOT — upload each
#                  explicitly)
set -euo pipefail

BUCKET="${1:?usage: $0 <bucket> [hg38|hg38-u1|hg38-u2|hg38-meth|hg38-meth-u2|hg38-minibwa|all]; set REF_ROOT=/path/to/Homo_sapiens_assembly38}"
WHICH="${2:-hg38}"
REF_ROOT="${REF_ROOT:?REF_ROOT must point at the directory containing Homo_sapiens_assembly38.fasta and its bwa-mem2 indexes}"
FASTA_NAME="Homo_sapiens_assembly38.fasta"

# Upload the standard DNA index sidecar set to references/<ref>/. `hg38` and
# its stride-2 `re-sa` copy `hg38-u1` share the exact same file list (only the
# .bwt.2bit.64 bytes differ), so both route here with a different <ref>.
_upload_dna_index() {
    local ref="$1"
    local dest="s3://${BUCKET}/references/${ref}/"
    # Require BOTH the .0123 and the .bwt.2bit.64: `aws s3 sync --include` would
    # silently omit a missing sidecar and still exit 0, publishing an incomplete
    # index that only fails during a paid run. .bwt.2bit.64 is the file `re-sa`
    # rewrites, so a dense copy (hg38-u1/-u2) missing it is exactly the mistake
    # to catch here.
    local sidecar
    for sidecar in .0123 .bwt.2bit.64; do
        if [[ ! -f "${REF_ROOT}/${FASTA_NAME}${sidecar}" ]]; then
            echo "error: bwa-mem2 index (${FASTA_NAME}${sidecar}) missing; reindex the FASTA first" >&2
            exit 1
        fi
    done
    aws s3 sync --exclude '*' \
        --include "${FASTA_NAME}" \
        --include "${FASTA_NAME}.0123" \
        --include "${FASTA_NAME}.amb" \
        --include "${FASTA_NAME}.ann" \
        --include "${FASTA_NAME}.bwt.2bit.64" \
        --include "${FASTA_NAME}.pac" \
        --include "${FASTA_NAME}.fai" \
        --include "Homo_sapiens_assembly38.dict" \
        "${REF_ROOT}/" "${dest}"
}

_upload_hg38_meth() {
    local ref="${1:-hg38-meth}"
    local dest="s3://${BUCKET}/references/${ref}/"
    if [[ ! -f "${REF_ROOT}/${FASTA_NAME}.bwameth.c2t.bwt.2bit.64" ]]; then
        echo "error: meth index (${FASTA_NAME}.bwameth.c2t.bwt.2bit.64) missing." >&2
        echo "build with: bwa-mem2 index --meth ${FASTA_NAME}" >&2
        echo "(peaks ~150 GB RAM; run on an instance with >=256 GB, e.g. r7i.8xlarge)" >&2
        exit 1
    fi
    # Original fasta + fai + dict are also required at runtime (alignment rule
    # points snakemake storage at the base path; the fg-labs binary auto-appends
    # .bwameth.c2t when --meth is set).
    aws s3 sync --exclude '*' \
        --include "${FASTA_NAME}" \
        --include "${FASTA_NAME}.fai" \
        --include "Homo_sapiens_assembly38.dict" \
        --include "${FASTA_NAME}.bwameth.c2t" \
        --include "${FASTA_NAME}.bwameth.c2t.0123" \
        --include "${FASTA_NAME}.bwameth.c2t.amb" \
        --include "${FASTA_NAME}.bwameth.c2t.ann" \
        --include "${FASTA_NAME}.bwameth.c2t.bwt.2bit.64" \
        --include "${FASTA_NAME}.bwameth.c2t.pac" \
        "${REF_ROOT}/" "${dest}"
}

# Upload the bwa-mem3 D3 methylation file set (the ORIGINAL bwa-mem2 index +
# the `.meth.*` 3-letter SEED index) to references/<ref>/. This is what
# `align_fg_labs`'s D3 meth path stages (workflow/rules/align.smk `_ref_inputs`
# meth_index="d3"), and it is DISTINCT from `_upload_hg38_meth`'s `.bwameth.c2t`
# doubled reference, which only the bwameth baseline uses. Used for the
# stride-4 sweep copy `hg38-meth-u2`, whose `.meth.bwt.2bit.64` has been
# `re-sa -u 2`'d.
_upload_meth_d3_index() {
    local ref="$1"
    local dest="s3://${BUCKET}/references/${ref}/"
    if [[ ! -f "${REF_ROOT}/${FASTA_NAME}.meth.bwt.2bit.64" ]]; then
        echo "error: D3 meth seed (${FASTA_NAME}.meth.bwt.2bit.64) missing in ${REF_ROOT}." >&2
        echo "hg38-meth-u2 is a copy of the hg38-meth tree with the .meth seed re-sa'd:" >&2
        echo "  cp -r <hg38-meth tree> ${REF_ROOT} && bwa-mem2.fg-labs re-sa -u 2 ${REF_ROOT}/${FASTA_NAME}.meth" >&2
        exit 1
    fi
    # The ORIGINAL (non-meth) DNA BWT sidecar is synced below and is declared by
    # the D3 `_ref_inputs` staging path, but `aws s3 sync --include` would silently
    # omit it if absent and still exit 0 -- publishing a partial reference that only
    # fails during a paid run. It rides along from the hg38-meth tree copy unchanged
    # (`re-sa -u 2` rewrites only the .meth seed), so require it explicitly too --
    # the same gap already guarded in `_upload_dna_index`.
    if [[ ! -f "${REF_ROOT}/${FASTA_NAME}.bwt.2bit.64" ]]; then
        echo "error: original DNA index (${FASTA_NAME}.bwt.2bit.64) missing in ${REF_ROOT}." >&2
        echo "it rides along in the copied hg38-meth tree beside the .meth seed; recopy the full tree." >&2
        exit 1
    fi
    aws s3 sync --exclude '*' \
        --include "${FASTA_NAME}" \
        --include "${FASTA_NAME}.fai" \
        --include "Homo_sapiens_assembly38.dict" \
        --include "${FASTA_NAME}.amb" \
        --include "${FASTA_NAME}.ann" \
        --include "${FASTA_NAME}.bwt.2bit.64" \
        --include "${FASTA_NAME}.pac" \
        --include "${FASTA_NAME}.meth.fa" \
        --include "${FASTA_NAME}.meth.amb" \
        --include "${FASTA_NAME}.meth.ann" \
        --include "${FASTA_NAME}.meth.bwt.2bit.64" \
        --include "${FASTA_NAME}.meth.pac" \
        "${REF_ROOT}/" "${dest}"
}

_upload_hg38_minibwa() {
    # minibwa index produces two sidecars: .l2b and .mbw. The plain .fasta is
    # already uploaded by _upload_hg38; minibwa only needs its own sidecars
    # alongside the existing bwa-mem2 ones in references/hg38/. Used only on
    # the local-only `private/minibwa-bench` branch.
    local dest="s3://${BUCKET}/references/hg38/"
    if [[ ! -f "${REF_ROOT}/${FASTA_NAME}.l2b" ]]; then
        echo "error: minibwa index (${FASTA_NAME}.l2b) missing." >&2
        echo "build with: minibwa index ${REF_ROOT}/${FASTA_NAME}" >&2
        exit 1
    fi
    aws s3 sync --exclude '*' \
        --include "${FASTA_NAME}.l2b" \
        --include "${FASTA_NAME}.mbw" \
        "${REF_ROOT}/" "${dest}"
}

if [[ ! -f "${REF_ROOT}/${FASTA_NAME}" ]]; then
    echo "error: ${REF_ROOT}/${FASTA_NAME} not found" >&2
    echo "override REF_ROOT if the Broad hg38 bundle lives elsewhere" >&2
    exit 1
fi

case "${WHICH}" in
    hg38)         _upload_dna_index hg38 ;;
    hg38-u1)      _upload_dna_index hg38-u1 ;;
    hg38-u2)      _upload_dna_index hg38-u2 ;;
    hg38-meth)    _upload_hg38_meth hg38-meth ;;
    hg38-meth-u2) _upload_meth_d3_index hg38-meth-u2 ;;
    hg38-minibwa) _upload_hg38_minibwa ;;
    all)          _upload_dna_index hg38; _upload_hg38_meth hg38-meth; _upload_hg38_minibwa ;;
    *)            echo "error: unknown <which>=${WHICH}" >&2; exit 2 ;;
esac
