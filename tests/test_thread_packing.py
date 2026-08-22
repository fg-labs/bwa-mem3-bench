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

2. The coordinator must pass a LARGE ``--cores``. Snakemake clamps ``threads``
   to the core count, and with ``--cores`` omitted that resolves to the local
   core count -- the coordinator runs on a c6a.large (2 vCPUs). A real run
   without the flag submitted ``align_fg_labs`` (threads: 16) as ``VCPU=2``.
   ``--jobs`` clamps it too, so the profile's ``jobs:`` must also clear the
   largest ``threads:`` any rule declares.

   None of this is visible to ``--dry-run``, which prints the unclamped rule
   value: a 12-core laptop reported ``threads: 16`` for a rule that a real
   submission turned into ``VCPU=2``. Only a submitted job exposes it.

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
PROFILE_TEMPLATE = Path(REPO_ROOT) / "workflow" / "profiles" / "aws-batch.config.yaml.template"


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


def test_coordinator_passes_enough_cores() -> None:
    """The coordinator MUST pass a large `--cores`, or every vCPU request shrinks.

    Snakemake clamps `threads` to the core count, and with `--cores` omitted that
    resolves to the LOCAL core count — the coordinator is a c6a.large, i.e. 2
    vCPUs. A real run with the flag absent submitted `align_fg_labs`
    (threads: 16) as VCPU=2.

    This is invisible to `--dry-run`, which reports the unclamped rule value: a
    12-core laptop happily printed `threads: 16`. Only a submitted job reveals
    it, which is why this assertion exists rather than a dry-run check.
    """
    text = ENTRYPOINT.read_text()
    match = re.search(r"(?<![\w-])--cores[= ]+(\d+)", text)
    assert match, (
        "coordinator-entrypoint.sh does not pass --cores; snakemake then clamps "
        "every rule's `threads` to the coordinator's own 2 vCPUs, so each worker "
        "is submitted as a 2-vCPU Batch job and Batch over-packs the host."
    )
    cores = int(match.group(1))
    needed = load_config(Path(REPO_ROOT) / "config").thread_scaling.max_threads
    assert cores >= needed, (
        f"coordinator passes --cores {cores}, below the largest `threads:` any "
        f"rule declares ({needed}, the thread-scaling ladder); that rule's vCPU "
        f"reservation would be clamped to {cores}."
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


def test_batch_profile_task_timeout_covers_every_rule_runtime() -> None:
    """`aws-batch-task-timeout` is the ONLY timeout the executor ever sends to
    AWS Batch -- every per-rule `resources.runtime` (e.g. `align_thread_scaling`'s
    `14400`) is dead for this executor and exists purely as in-DAG documentation
    (see the profile template's own comment on `aws-batch-task-timeout` and
    `scaling.smk`'s `runtime = 14400`). A real ladder run was killed at exactly
    the profile's OLD 7200 s despite declaring `runtime: 14400`, which is what
    this PR fixes -- but the fix is a hand-kept duplicate: nothing stops a
    future rule from declaring a `runtime` above the profile's value again.

    Scans every `resources: runtime = <seconds>` across `workflow/rules/*.smk`
    (not a hardcoded rule list, so a new rule with its own long-running
    `runtime` is covered automatically) and asserts the profile's
    `aws-batch-task-timeout` is at least the largest one declared.
    """
    declared: dict[str, int] = {}
    for smk_path in sorted((Path(REPO_ROOT) / "workflow" / "rules").glob("*.smk")):
        text = smk_path.read_text()
        # Widen first, then require every hit to be a plain integer literal --
        # a future rule computing `runtime = lambda wc: ...` would silently
        # evade a regex that only ever looked for digits, defeating the point
        # of this test without any signal that it had.
        for wide in re.finditer(r"^\s*runtime\s*=\s*(\S.*?),?\s*$", text, re.MULTILINE):
            site = f"{smk_path.name}:{wide.start()}"
            literal = re.fullmatch(r"(\d+)", wide.group(1))
            assert literal, (
                f"{site} declares `runtime = {wide.group(1)}`, not a plain integer "
                "literal -- this test can't check it against aws-batch-task-timeout; "
                "either make it a literal or extend this test to parse it."
            )
            declared[site] = int(literal.group(1))
    assert declared, "no `resources: runtime = <seconds>` found under workflow/rules/*.smk"

    text = PROFILE_TEMPLATE.read_text()
    match = re.search(r"^aws-batch-task-timeout:\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, "aws-batch profile does not set `aws-batch-task-timeout:`"
    task_timeout = int(match.group(1))

    slowest_site, slowest_runtime = max(declared.items(), key=lambda kv: kv[1])
    assert task_timeout >= slowest_runtime, (
        f"profile sets `aws-batch-task-timeout: {task_timeout}` but "
        f"{slowest_site} declares `runtime = {slowest_runtime}`; the executor "
        f"sends ONLY `aws-batch-task-timeout` to AWS Batch, so that job would "
        f"be killed at {task_timeout}s despite snakemake believing it has "
        f"{slowest_runtime}s."
    )
