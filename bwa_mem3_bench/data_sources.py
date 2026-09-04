"""Source → destination maps for uploading benchmark inputs.

Source paths are configured per-host via env vars:

* ``BWA_MEM3_BENCH_VENDOR_ROOT`` — root of the vendor data tree (the
  fgumi-benchmarks layout). Raw vendor FASTQs are read from
  ``<root>/data/raw/vendor/`` — e.g. ``<root>/data/raw/vendor/agilent-qxt_{1,2}.fastq.gz``
  and ``<root>/data/raw/vendor/twist-emseq_{1,2}.fastq.gz``. Defaults to
  ``./vendor-fastqs`` under the repo root if unset. ``agilent-qxt`` is a public
  human Agilent SureSelect QXT cancer panel (SRR15497869 / PRJNA755485);
  ``twist-emseq`` is a Twist Bioscience example QC dataset (request from Twist).
  See ``docs/data-setup.md``.
* ``BWA_MEM3_BENCH_STAGE_ROOT`` — scratch directory for downsampled FASTQs
  written by ``upload-data``. Defaults to ``./data-stage`` under the repo
  root.
* ``BWA_MEM3_BENCH_ZENODO_ROOT`` — directory holding the Zenodo HG002 dataset
  (DOI 10.5281/zenodo.19703025; HiC/long-read FASTQs, CC BY 4.0, Heng Li).
  Defaults to ``./zenodo-fastqs`` under the repo root.
* ``BWA_MEM3_BENCH_SBX_ROOT`` — directory holding the Roche SBX BAM tree.
  Defaults to ``./sbx-bams`` under the repo root.

See ``docs/data-setup.md`` for how to obtain the source FASTQs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

SCRATCH_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_VENDOR_ROOT", str(REPO_ROOT / "vendor-fastqs")))
STAGE_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_STAGE_ROOT", str(REPO_ROOT / "data-stage")))
# HG002 Zenodo dataset (HiC/long-read FASTQs; CC BY 4.0, Heng Li) and the Roche
# SBX BAM tree. Repo-relative defaults; point the env vars at the real data on
# each host (see docs/data-setup.md).
ZENODO_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_ZENODO_ROOT", str(REPO_ROOT / "zenodo-fastqs")))
SBX_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_SBX_ROOT", str(REPO_ROOT / "sbx-bams")))


@dataclass(frozen=True)
class DataSource:
    sample: str
    source_r1: Path  # local FASTQ to upload as r1.fq.gz (staged from BAM if source_bam set)
    dest_prefix: str  # e.g. "data/wgs/1kg-HG00096/downsampled-5M/"
    source_r2: Path | None = None  # None => single-end
    downsample_every_nth: int | None = None  # None = full file
    source_bam: Path | None = None  # single-end: convert this BAM -> source_r1 FASTQ
    subsample_frac: float | None = None  # samtools view -s 42.<frac> when staging from BAM


def sample_sources(bucket: str) -> dict[str, DataSource]:
    """Return the canonical source→dest mapping for the benchmark samples.

    All non-smoke samples target ~5M pairs so a full `--target all` run on
    one arch completes in a few minutes on a `.4xlarge`. Relative fg-labs vs
    upstream ratios converge well under 5M pairs; longer runs burn compute
    without adding signal.

    Note: `bucket` is currently unused in the dest_prefix (prefixes are bucket-
    relative), but the param exists for future where we may want to vary
    destination bucket per sample.
    """
    vendor = SCRATCH_ROOT / "data/raw/vendor"
    return {
        "wgs-5M": DataSource(
            sample="wgs-5M",
            # Downsampled from 1kg-wgs-HG00096.primary-only.bam (384M pairs)
            # via `samtools view -bs 42.013 | collate | fastq`.
            source_r1=STAGE_ROOT / "wgs-5M_1.fastq.gz",
            source_r2=STAGE_ROOT / "wgs-5M_2.fastq.gz",
            dest_prefix="data/wgs/1kg-HG00096/downsampled-5M/",
        ),
        "wes-5M": DataSource(
            sample="wes-5M",
            # Downsampled from 1kg-wes-HG00100.primary-only.bam (102M pairs)
            # via `samtools view -bs 42.049 | collate | fastq`.
            source_r1=STAGE_ROOT / "wes-5M_1.fastq.gz",
            source_r2=STAGE_ROOT / "wes-5M_2.fastq.gz",
            dest_prefix="data/wes/1kg-HG00100/downsampled-5M/",
        ),
        "panel-agilent-qxt-5M": DataSource(
            sample="panel-agilent-qxt-5M",
            # SRR15497869 (PRJNA755485): human germline blood, Agilent SureSelect
            # QXT 93-gene hereditary/colorectal-cancer panel, 2x150, non-UMI.
            # Replaces the mislabeled cat SRR34589119 — see docs/data-setup.md.
            source_r1=vendor / "agilent-qxt_1.fastq.gz",
            source_r2=vendor / "agilent-qxt_2.fastq.gz",
            dest_prefix="data/panel/agilent-qxt/downsampled-5M/",
            # Source is ~5.31M pairs — already ~5M, so keep the full file.
        ),
        "meth-twist-emseq-5M": DataSource(
            sample="meth-twist-emseq-5M",
            source_r1=vendor / "twist-emseq_1.fastq.gz",
            source_r2=vendor / "twist-emseq_2.fastq.gz",
            dest_prefix="data/meth/twist-emseq/downsampled-5M/",
            downsample_every_nth=3,  # 15.6M pairs / 3 ≈ 5.2M
        ),
        "smoke-1M": DataSource(
            sample="smoke-1M",
            # Derived from the same human Agilent QXT panel as panel-agilent-qxt-5M
            # (replaces the mislabeled cat source). Wiring smoke test only.
            source_r1=vendor / "agilent-qxt_1.fastq.gz",
            source_r2=vendor / "agilent-qxt_2.fastq.gz",
            dest_prefix="data/smoke/1M/",
            downsample_every_nth=168,  # 5.31M pairs / 168 ≈ 31.6K (fast wiring smoke)
        ),
        "smoke-meth": DataSource(
            sample="smoke-meth",
            source_r1=vendor / "twist-emseq_1.fastq.gz",
            source_r2=vendor / "twist-emseq_2.fastq.gz",
            dest_prefix="data/smoke-meth/10K/",
            downsample_every_nth=1500,  # 15.6M pairs / 1500 ≈ 10.4K
        ),
        "hic-1M": DataSource(
            sample="hic-1M",
            source_r1=ZENODO_ROOT / "HG002.HiC-1M_1.fq.gz",
            source_r2=ZENODO_ROOT / "HG002.HiC-1M_2.fq.gz",
            dest_prefix="data/hic/hg002-1M/",
            # Already 1M pairs (2x151) — no downsample.
        ),
        "sbx-1M": DataSource(
            sample="sbx-1M",
            # source_r1 is the staged single-end FASTQ produced from source_bam.
            source_r1=STAGE_ROOT / "sbx-1M.fq.gz",
            dest_prefix="data/sbx/hg002-1M/",
            source_bam=SBX_ROOT / "2026/HG002.bam",
            # 1_000_000 / ~858M primary reads (874.2M records - ~1.9% supplementary).
            subsample_frac=0.001166,
        ),
    }
