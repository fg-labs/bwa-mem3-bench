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
#   hg38       — standard bwa-mem2 index (default)
#   hg38-meth  — bwameth.c2t doubled reference + bwa-mem2 index (build with
#                `bwa-mem2 index --meth <fasta>`; peaks ~150 GB RAM during
#                FMI construction — use a 256 GB instance.)
#   all        — both
set -euo pipefail

BUCKET="${1:?usage: $0 <bucket> [hg38|hg38-meth|all]; set REF_ROOT=/path/to/Homo_sapiens_assembly38}"
WHICH="${2:-hg38}"
REF_ROOT="${REF_ROOT:?REF_ROOT must point at the directory containing Homo_sapiens_assembly38.fasta and its bwa-mem2 indexes}"
FASTA_NAME="Homo_sapiens_assembly38.fasta"

_upload_hg38() {
    local dest="s3://${BUCKET}/references/hg38/"
    if [[ ! -f "${REF_ROOT}/${FASTA_NAME}.0123" ]]; then
        echo "error: bwa-mem2 index (${FASTA_NAME}.0123) missing; reindex the FASTA first" >&2
        exit 1
    fi
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
    local dest="s3://${BUCKET}/references/hg38-meth/"
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

if [[ ! -f "${REF_ROOT}/${FASTA_NAME}" ]]; then
    echo "error: ${REF_ROOT}/${FASTA_NAME} not found" >&2
    echo "override REF_ROOT if the Broad hg38 bundle lives elsewhere" >&2
    exit 1
fi

case "${WHICH}" in
    hg38)       _upload_hg38 ;;
    hg38-meth)  _upload_hg38_meth ;;
    all)        _upload_hg38; _upload_hg38_meth ;;
    *)          echo "error: unknown <which>=${WHICH}" >&2; exit 2 ;;
esac
