"""`bless-baseline` — run the upstream baseline once per sample×arch (n=5), cached in S3."""

from __future__ import annotations

from bwa_mem3_bench.commands.submit import submit

# Default number of replicates used to establish a statistically sound baseline.
_REPS_BASELINE = 5


def bless_baseline(
    *,
    upstream_tag: str = "v2.2.1",
    reps: int = _REPS_BASELINE,
    job_name: str | None = None,
    dry_run: bool = False,
) -> None:
    """Submit a coordinator Batch job that runs the upstream baseline for all samples × archs.

    Delegates to :func:`submit` with ``target="baseline_all"`` so the same
    coordinator-job pattern is used.

    :param upstream_tag: upstream bwa-mem2 tag (recorded in S3 key for the baseline).
    :param reps: replicates per (sample, arch). Default 5 for a statistically stable baseline.
    :param job_name: Batch job name; defaults to ``baseline_all-<upstream_tag>``.
    :param dry_run: print the underlying command without executing.
    """
    submit(
        fg_labs_sha=upstream_tag,
        target="baseline_all",
        reps=reps,
        job_name=job_name or f"baseline_all-{upstream_tag}",
        dry_run=dry_run,
    )
