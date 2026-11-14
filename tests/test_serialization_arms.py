"""Unit tests for `bwa_mem3_bench.serialization_arms`: flag guard, arms, commands and plan."""

from __future__ import annotations

import shlex

import pytest

from bwa_mem3_bench.serialization_arms import (
    Arm,
    Context,
    arm_groups,
    flag_clash,
    plan_tsv,
    run_command,
)

PLAN_COLUMNS = 10
CTX = Context(
    aligner="bwa-mem2.fg-labs",
    upstream="bwa-mem2.upstream",
    batch_k=160000000,
    budget=8,
    helpers=2,
    mem_flags=("-5", "-S"),
    fg_labs_flags=("-R", "@RG\\tID:a b;touch x"),
)


@pytest.mark.parametrize(
    "token",
    ["--fast", "-K", "-K100", "-t", "-t4", "-o", "-f", "--bam", "--bam=1", "--bam-threads=2"]
    + ["-Mt4", "-SK", "-Mo", "--fa", "--ba", "--bam-t"],
)
def test_flag_clash_rejects_benchmark_controls(token: str) -> None:
    assert flag_clash(token) is not None


@pytest.mark.parametrize(
    "token", ["-5", "-S", "-P", "-M", "-Y", "--meth", "--compat=bwa-mem2", "x"]
)
def test_flag_clash_accepts_other_flags(token: str) -> None:
    assert flag_clash(token) is None


def test_arm_groups_pair_bwa_mem3_routes_and_drop_upstream_on_arm() -> None:
    x86 = arm_groups(
        ["bwa-mem3", "bwa", "bwa-mem2", "minibwa"],
        ["default", "fast"],
        ["stock", "dense"],
        upstream=True,
    )
    arm = arm_groups(
        ["bwa-mem3", "bwa", "bwa-mem2", "minibwa"],
        ["default", "fast"],
        ["stock", "dense"],
        upstream=False,
    )
    assert [len(g) for g in x86] == [2, 2, 2, 2, 1, 1, 1]
    assert {a.route for a in x86[0]} == {"native", "sam"}
    assert "bwa-mem2" in {g[0].tool for g in x86}
    assert "bwa-mem2" not in {g[0].tool for g in arm}
    assert arm_groups(["bwa-mem2"], ["default"], ["stock"], upstream=False) == []


def _inner(command: str) -> str:
    """The pipeline a `taskset ... bash -c <pipeline>` command runs."""
    argv = shlex.split(command)
    assert argv[:3] == ["taskset", "-c", "0-7"]
    assert argv[3:5] == ["bash", "-c"]
    return argv[5]


def test_pinned_native_writes_level1_bam_through_cat_under_taskset() -> None:
    pipeline = _inner(run_command(Arm("bwa-mem3", "fast", "dense", "native"), "pinned", CTX))
    first = pipeline.split(" | ")[0]
    assert shlex.split(first)[:9] == [
        "set",
        "-o",
        "pipefail;",
        "bwa-mem2.fg-labs",
        "mem",
        "-t",
        "6",
        "-K",
        "160000000",
    ]
    assert "-5 -S -R '@RG\\tID:a b;touch x' --fast --bam=1 --bam-threads 2 -o -" in first
    assert pipeline.endswith('| cat > "$RUN_OUT"')
    assert "samtools" not in pipeline


def test_unpinned_sam_route_has_no_taskset() -> None:
    command = run_command(Arm("bwa"), "unpinned", CTX)
    assert "taskset" not in command
    assert command.startswith("set -o pipefail; bwa mem -t 6 -K 160000000 -5 -S ")
    assert '| samtools view -@ 2 -O BAM,level=1 -o - - | cat > "$RUN_OUT"' in command


def test_practical_streams_to_dev_null_on_the_full_budget() -> None:
    native = _inner(run_command(Arm("bwa-mem3", route="native"), "practical", CTX))
    assert " -t 8 " in native
    assert "--bam=0 -o -" in native
    assert native.endswith("| cat > /dev/null")
    sam = _inner(run_command(Arm("minibwa"), "practical", CTX))
    assert "samtools" not in sam
    assert "--bam" not in sam


def test_plan_is_deterministic_and_keeps_pairs_adjacent() -> None:
    groups = arm_groups(["bwa-mem3", "bwa"], ["default", "fast"], ["stock"], upstream=True)
    plan = plan_tsv(groups, "pinned", CTX, reps=2, seed="abc1234")
    assert plan == plan_tsv(groups, "pinned", CTX, reps=2, seed="abc1234")
    assert plan != plan_tsv(groups, "pinned", CTX, reps=2, seed="def5678")
    rows = [line.split("\t") for line in plan.splitlines()]
    assert all(len(row) == PLAN_COLUMNS for row in rows)
    assert [row[0] for row in rows] == ["0"] * 5 + ["1"] * 5 + ["2"] * 5
    for rnd in "012":
        labels = [row[2] for row in rows if row[0] == rnd]
        for mode in ("default", "fast"):
            positions = [i for i, label in enumerate(labels) if f"-{mode}-" in label]
            assert positions[1] - positions[0] == 1
