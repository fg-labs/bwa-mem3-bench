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
# intent for these targets — both are "every arch" rules.
_FULL_SWEEP_TARGETS = frozenset({"all", "baseline_all"})


def submit(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    target: str = "smoke",
    samples: str = "",
    archs: str = "",
    reps: int = 1,
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
    :param job_name: Batch job name; defaults to ``<target>-<sha>``.
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

    container_overrides: dict[str, object] = {"environment": env_overrides}
    name = job_name or f"{target}-{fg_labs_sha}"

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
