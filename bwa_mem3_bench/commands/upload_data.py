"""`upload-data` — stage FASTQs and references to the S3 bucket."""

from __future__ import annotations

import shlex

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.data_sources import sample_sources

_DEFAULT_BUCKET = aws_config.load().bucket


def _upload_reference(bucket: str, *, dry_run: bool) -> None:
    """Invoke scripts/upload_reference.sh (wraps aws s3 sync for hs38DH indexes)."""
    script = REPO_ROOT / "scripts/upload_reference.sh"
    run_cmd(["bash", str(script), bucket], dry_run=dry_run)


def _upload_sample(sample: str, bucket: str, *, dry_run: bool) -> None:
    srcs = sample_sources(bucket)
    if sample not in srcs:
        raise ValueError(f"unknown sample '{sample}'; known: {sorted(srcs)}")
    src = srcs[sample]
    dest = f"s3://{bucket}/{src.dest_prefix}"

    if src.downsample_every_nth is not None:
        # Paired-end only: downsample requires both r1 and r2.
        if src.source_r2 is None:
            raise ValueError(f"sample '{sample}' has downsample_every_nth but no source_r2")
        n = src.downsample_every_nth
        r1_ds = src.source_r1.with_name(src.source_r1.name.replace(".fastq.gz", f".ds{n}.fastq.gz"))
        r2_ds = src.source_r2.with_name(src.source_r2.name.replace(".fastq.gz", f".ds{n}.fastq.gz"))
        # Deterministic every-Nth-pair downsample via mawk (4 lines per record).
        for src_fq, dst_fq in ((src.source_r1, r1_ds), (src.source_r2, r2_ds)):
            if dst_fq.exists():
                continue
            cmd = (
                f"gunzip -c {shlex.quote(str(src_fq))} | "
                f"mawk 'NR % (4*{n}) <= 4 && NR % (4*{n}) > 0' | "
                f"gzip > {shlex.quote(str(dst_fq))}"
            )
            run_cmd(["bash", "-c", cmd], dry_run=dry_run)
        run_cmd(["aws", "s3", "cp", str(r1_ds), dest + "r1.fq.gz"], dry_run=dry_run)
        run_cmd(["aws", "s3", "cp", str(r2_ds), dest + "r2.fq.gz"], dry_run=dry_run)
    elif src.source_bam is not None:
        # Single-end from BAM: staging is handled by Task 5 (BAM→FASTQ conversion).
        # source_r1 is the pre-staged FASTQ written by that step; upload it as-is.
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)
    elif src.source_r2 is not None:
        # Paired-end, no downsample: upload both FASTQs directly.
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)
        run_cmd(["aws", "s3", "cp", str(src.source_r2), dest + "r2.fq.gz"], dry_run=dry_run)
    else:
        # Single-end FASTQ: upload r1 only.
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)


def upload_data(
    *,
    what: str = "all",
    bucket: str = _DEFAULT_BUCKET,
    dry_run: bool = False,
) -> None:
    """Upload inputs and references to the S3 bucket.

    For `--what references|all`, set the ``REF_ROOT`` env var to the directory
    holding the Broad hg38 bundle (``Homo_sapiens_assembly38.fasta`` plus
    bwa-mem2 indexes); see ``docs/data-setup.md``.

    :param what: one of `all`, `references`, `data`, or a single sample name.
    :param bucket: destination S3 bucket. Defaults to the bucket from
        ``cdk/outputs.json`` (or the ``BWA_MEM3_BENCH_S3_BUCKET`` env var).
    :param dry_run: print commands only.
    """
    if what in ("all", "references"):
        _upload_reference(bucket, dry_run=dry_run)
    if what in ("all", "data"):
        for sample in sample_sources(bucket):
            _upload_sample(sample, bucket, dry_run=dry_run)
    elif what not in ("all", "references", "data"):
        _upload_sample(what, bucket, dry_run=dry_run)
