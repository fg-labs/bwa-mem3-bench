"""`bless-baseline` — run the upstream baseline once per sample×arch (n=5), cached in S3."""

from __future__ import annotations

from bwa_mem3_bench.commands.submit import submit

# Default number of replicates used to establish a statistically sound baseline.
_REPS_BASELINE = 5


def bless_baseline(
    *,
    fg_labs_sha: str,
    reps: int = _REPS_BASELINE,
    job_name: str | None = None,
    dry_run: bool = False,
) -> None:
    """Submit a coordinator Batch job that runs the upstream baseline for all samples × archs.

    Delegates to :func:`submit` with ``target="baseline_all"`` so the same
    coordinator-job pattern is used.

    The baseline is keyed in S3 by the upstream tag (the Snakefile's
    ``upstream_tag`` config), independent of which fg-labs image runs it. But the
    coordinator and its ``align_baseline`` workers still need a real, already-pushed
    image to execute in (workers pull ``<ECR>:<fg_labs_sha>``), so ``fg_labs_sha``
    must be a built SHA on ECR — e.g. the current release. Any recent image works;
    they all ship ``bwa-mem2.upstream``. Passing the upstream tag here (the previous
    behaviour) is wrong twice over: ``:v2.2.1`` is not a pushed image tag, and the
    dotted name is rejected by ``aws batch submit-job`` (job names are ``[A-Za-z0-9_-]``).

    :param fg_labs_sha: fg-labs/bwa-mem3 SHA whose image runs the baseline. Must
        already be built + pushed to ECR.
    :param reps: replicates per (sample, arch). Default 5 for a statistically stable baseline.
    :param job_name: Batch job name; defaults to ``baseline_all-<fg_labs_sha>``.
    :param dry_run: print the underlying command without executing.
    """
    submit(
        fg_labs_sha=fg_labs_sha,
        target="baseline_all",
        reps=reps,
        job_name=job_name,
        dry_run=dry_run,
    )
