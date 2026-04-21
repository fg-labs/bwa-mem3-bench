"""Source → destination maps for uploading benchmark inputs.

Source paths are configured per-host via two env vars:

* ``BWA_MEM3_BENCH_VENDOR_ROOT`` — directory holding raw vendor FASTQs (e.g.
  ``twist-umi_{1,2}.fastq.gz``). Defaults to ``./vendor-fastqs`` under the
  repo root if unset. The ``twist-*`` files are example QC datasets
  provided by Twist Bioscience; OSS users should request equivalent files
  directly from Twist (see ``docs/data-setup.md``).
* ``BWA_MEM3_BENCH_STAGE_ROOT`` — scratch directory for downsampled FASTQs
  written by ``upload-data``. Defaults to ``./data-stage`` under the repo
  root.

See ``docs/data-setup.md`` for how to obtain the source FASTQs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

SCRATCH_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_VENDOR_ROOT", str(REPO_ROOT / "vendor-fastqs")))
STAGE_ROOT = Path(os.environ.get("BWA_MEM3_BENCH_STAGE_ROOT", str(REPO_ROOT / "data-stage")))


@dataclass(frozen=True)
class DataSource:
    sample: str
    source_r1: Path
    source_r2: Path
    dest_prefix: str  # e.g. "data/wgs/1kg-HG00096/downsampled-5M/"
    downsample_every_nth: int | None = None  # None = full file


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
        "panel-twist-5M": DataSource(
            sample="panel-twist-5M",
            source_r1=vendor / "twist-umi_1.fastq.gz",
            source_r2=vendor / "twist-umi_2.fastq.gz",
            dest_prefix="data/panel/twist-umi/downsampled-5M/",
            downsample_every_nth=2,  # 7.9M pairs / 2 ≈ 3.95M (source is smaller than nominal)
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
            source_r1=vendor / "twist-umi_1.fastq.gz",
            source_r2=vendor / "twist-umi_2.fastq.gz",
            dest_prefix="data/smoke/1M/",
            downsample_every_nth=250,
        ),
        "smoke-meth": DataSource(
            sample="smoke-meth",
            source_r1=vendor / "twist-emseq_1.fastq.gz",
            source_r2=vendor / "twist-emseq_2.fastq.gz",
            dest_prefix="data/smoke-meth/10K/",
            downsample_every_nth=1500,  # 15.6M pairs / 1500 ≈ 10.4K
        ),
    }
