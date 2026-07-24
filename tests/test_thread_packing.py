"""Guards for AWS Batch vCPU packing of the alignment rules.

Our snakemake-executor-plugin-aws-batch fork derives a Batch job's VCPU
resourceRequirement from the snakemake ``threads`` directive::

    vcpu = max(1, job.threads if job.threads > 0 else resources["_cores"])

Two things therefore have to hold, and neither fails loudly:

1. The align rules must declare ``threads:`` (not a ``params.threads``). As a
   bare param the aligner still runs ``-t 16`` while Batch is told the job
   needs ONE vCPU, so Batch packs by memory alone. On m7i.4xlarge (64 GB) two
   non-meth alignments (28 GB each) then land on one 16-vCPU host and each runs
   16 threads against ~8 effective CPUs.

2. Snakemake must not be given ``--cores``. It clamps ``threads`` to the core
   count, so a ``--cores 2`` coordinator would silently emit ``VCPU=2`` and
   reintroduce the same over-packing. Verified empirically: ``--jobs 50`` alone
   preserves ``threads: 16``; ``--jobs 50 --cores 2`` yields ``threads: 2``.

Both were live bugs on the 394f8f8 sweep (m7i mean_load 1494 -> 792, parallel
efficiency 93% -> 50% on wgs-5M/wes-5M) and are cheap to regress into again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.workflow_config import load_config

# Rules whose shell body launches a multi-threaded aligner. Each must reserve
# the vCPUs it actually uses so Batch cannot co-schedule two on one host.
ALIGN_RULES = (
    ("workflow/rules/align.smk", "align_fg_labs"),
    ("workflow/rules/align.smk", "align_baseline"),
    ("workflow/rules/align_minibwa.smk", "align_minibwa"),
)

ENTRYPOINT = Path(REPO_ROOT) / "docker" / "coordinator-entrypoint.sh"
PROFILE_TEMPLATE = Path(REPO_ROOT) / "workflow" / "profiles" / "aws-batch" / "config.yaml.template"


def _rule_body(smk_path: str, rule_name: str) -> str:
    """Source text of one rule, from its ``rule <name>:`` line to the next rule."""
    text = (Path(REPO_ROOT) / smk_path).read_text()
    start = text.index(f"rule {rule_name}:")
    nxt = text.find("\nrule ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


@pytest.mark.parametrize(("smk_path", "rule_name"), ALIGN_RULES)
def test_align_rule_declares_threads_directive(smk_path: str, rule_name: str) -> None:
    """The rule sets `threads:`, so the executor requests real vCPUs."""
    body = _rule_body(smk_path, rule_name)
    assert re.search(r"^\s*threads:\s*CONFIG\.threads\s*$", body, re.MULTILINE), (
        f"{rule_name} must declare `threads: CONFIG.threads`; without it the "
        "executor plugin requests VCPU=1 and Batch over-packs the host."
    )


@pytest.mark.parametrize(("smk_path", "rule_name"), ALIGN_RULES)
def test_align_rule_has_no_threads_param(smk_path: str, rule_name: str) -> None:
    """`params.threads` must not shadow the directive.

    A param is invisible to the executor, so it would set the aligner's `-t`
    without reserving the matching vCPUs — the exact bug this guards.
    """
    body = _rule_body(smk_path, rule_name)
    assert not re.search(r"^\s*threads\s*=\s*CONFIG\.threads", body, re.MULTILINE), (
        f"{rule_name} declares threads as a param; use the `threads:` directive "
        "so the Batch vCPU request matches the aligner's -t."
    )
    assert "{params.threads}" not in body, (
        f"{rule_name} shell references {{params.threads}}; use {{threads}}."
    )


def test_coordinator_does_not_pass_cores() -> None:
    """Snakemake clamps `threads` to `--cores`, which would shrink the vCPU request."""
    text = ENTRYPOINT.read_text()
    # Match the flag only as an argument, so prose in comments cannot trip it.
    assert not re.search(r"(?<![\w-])--cores(?![\w-])", text), (
        "coordinator-entrypoint.sh passes --cores; snakemake clamps `threads` to "
        "it, silently reducing the Batch VCPU request for every align job."
    )
    assert not re.search(r"(?<![\w-])-c\s+\d", text), (
        "coordinator-entrypoint.sh passes -c <n> (--cores); same clamping problem."
    )


def test_batch_profile_sets_no_cores() -> None:
    """A `cores:` profile key is equivalent to passing --cores."""
    text = PROFILE_TEMPLATE.read_text()
    assert not re.search(r"^\s*cores\s*:", text, re.MULTILINE), (
        "the aws-batch profile sets `cores:`; snakemake clamps `threads` to it, "
        "silently reducing the Batch VCPU request for every align job."
    )


def test_batch_profile_jobs_covers_thread_ladder() -> None:
    """`jobs` also caps `threads`, so it must clear the ladder's largest rung.

    In remote-executor mode snakemake clamps every rule's `threads` to `--jobs`,
    not just to `--cores`. Observed directly: with `jobs: 50`, the ladder's
    `threads: 64` came back as `threads: 50`. That understates the vCPU
    reservation, and if `jobs` ever dropped below half the host's vCPUs two
    ladder jobs could co-schedule and corrupt every point on the curve.
    """
    config = load_config(Path(REPO_ROOT) / "config")
    text = PROFILE_TEMPLATE.read_text()
    match = re.search(r"^jobs:\s*(\d+)", text, re.MULTILINE)
    assert match, "aws-batch profile does not set `jobs:`"
    jobs = int(match.group(1))
    needed = config.thread_scaling.max_threads
    assert jobs >= needed, (
        f"profile sets `jobs: {jobs}` but the thread-scaling ladder needs "
        f"`threads: {needed}`; snakemake clamps threads to jobs, so the ladder "
        f"job would reserve only {jobs} vCPUs."
    )
