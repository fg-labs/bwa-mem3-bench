"""`submit` — launch a coordinator Batch job that drives snakemake remotely."""

from __future__ import annotations

import json
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.workflow_config import load_config

_cfg = aws_config.load()
COORDINATOR_QUEUE = _cfg.coordinator_queue
COORDINATOR_JOB_DEF = _cfg.coordinator_job_definition

# Targets whose `rule <target>` definition iterates over ARCHS (the Snakefile's
# user-overridable arch list, defaulting to `core_arch`). Without explicit
# --archs these silently shrink to a one-arch sweep, which is almost never the
# intent for these targets — they are all "every arch" rules. The minibwa
# targets iterate MINIBWA_ARCHS (= ARCHS minus m7i), so passing full_archs is
# correct: m7i is filtered out in the Snakefile.
_FULL_SWEEP_TARGETS = frozenset({"all", "baseline_all", "minibwa", "minibwa_smoke"})


def submit(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    target: str = "smoke",
    samples: str = "",
    archs: str = "",
    reps: int = 1,
    make_target: str = "",
    golden_ref_sha: str = "",
    job_name: str | None = None,
    dry_run: bool = False,
) -> None:
    """Submit a coordinator job to AWS Batch that runs snakemake targeting `target`.

    The coordinator job runs snakemake inside the Docker image; it in turn submits
    child Batch jobs for each alignment rule.  Developer only needs `batch:SubmitJob`
    — no `iam:PassRole` required.

    :param fg_labs_sha: fg-labs/bwa-mem3 SHA. Must already be built + pushed to ECR.
    :param target: Snakemake target (e.g. ``smoke``, ``all``, ``baseline_all``).
    :param samples: comma-separated sample subset (empty = all).
    :param archs: comma-separated arch subset. Empty falls back to
        ``full_archs`` for ``all`` / ``baseline_all`` (so "full benchmark"
        actually sweeps every arch), and to ``core_arch`` for everything else
        (so ad-hoc rule invocations stay cheap).
    :param reps: replicate count.
    :param make_target: fg-labs/bwa-mem3 Makefile target used at build time.
        Must match the ``--make-target`` passed to ``build`` for the same SHA.
        Empty (default) selects the vanilla image tag ``<sha>`` and writes
        results to ``s3://.../runs/<sha>/``. Non-empty propagates as the
        ``BUILD_VARIANT`` env var to the coordinator entrypoint, which
        derives both the worker image tag (``<sha>-<make_target>``) and the
        snakemake ``fg_labs_sha`` config (``<sha>-<make_target>``) — so the
        S3 output namespace becomes ``s3://.../runs/<sha>-<make_target>/``
        and a same-SHA default-build run cannot clobber the variant run's
        BAMs (or vice versa).
    :param golden_ref_sha: pinned previous-release fg-labs SHA to compare against
        for the vs-golden (Gate #2) dimension. Empty (default) disables vs-golden
        comparison. When set, ``compare_vs_golden`` reads
        ``golden/fg-labs-<golden_ref_sha>/`` rather than the run's own SHA.
    :param job_name: Batch job name; defaults to ``<target>-<sha>`` (or
        ``<target>-<sha>-<make_target>`` when make_target is set).
    :param dry_run: print the ``aws batch submit-job`` command without executing.
    """
    if not archs and target in _FULL_SWEEP_TARGETS:
        archs = ",".join(load_config(Path(REPO_ROOT) / "config").full_archs)
        print(f"[submit] target={target}: auto-set --archs={archs}")
    env_overrides: list[dict[str, str]] = [
        {"name": "FG_LABS_SHA", "value": fg_labs_sha},
        {"name": "TARGET", "value": target},
    ]
    if samples:
        env_overrides.append({"name": "SAMPLES", "value": samples})
    if archs:
        env_overrides.append({"name": "ARCHS", "value": archs})
    if reps:
        env_overrides.append({"name": "REPS", "value": str(reps)})
    if golden_ref_sha:
        env_overrides.append({"name": "GOLDEN_REF_SHA", "value": golden_ref_sha})
    if make_target:
        # Coordinator entrypoint derives both `image_tag` (worker image to pull)
        # and a suffixed `fg_labs_sha` (S3 output namespace + DB primary key)
        # from `BUILD_VARIANT`. Sending just the variant — not the composed
        # tag — keeps the two derived values guaranteed-consistent: a future
        # refactor can change the composition rule in one place instead of
        # auditing every caller.
        #
        # Concretely the coordinator composes:
        #   IMAGE_TAG     = <fg_labs_sha>-<build_variant>   (tag only, no ECR prefix)
        #   fg_labs_sha   = <fg_labs_sha>-<build_variant>   (snakemake config)
        # so workers pull the right image AND write to a non-colliding
        # `s3://.../runs/<sha>-<variant>/` prefix (otherwise the LTO run
        # would clobber the baseline run's BAMs for the same SHA).
        env_overrides.append({"name": "BUILD_VARIANT", "value": make_target})

    container_overrides: dict[str, object] = {"environment": env_overrides}
    default_name = (
        f"{target}-{fg_labs_sha}-{make_target}" if make_target else f"{target}-{fg_labs_sha}"
    )
    name = job_name or default_name

    cmd = [
        "aws",
        "batch",
        "submit-job",
        "--job-name",
        name,
        "--job-queue",
        COORDINATOR_QUEUE,
        "--job-definition",
        COORDINATOR_JOB_DEF,
        "--container-overrides",
        json.dumps(container_overrides),
    ]
    run_cmd(cmd, dry_run=dry_run)
