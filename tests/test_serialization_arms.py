"""Unit tests for `bwa_mem3_bench.serialization_arms`: flag guard, arms, commands and plan."""

from __future__ import annotations

import shlex
from collections import Counter

import pytest

from bwa_mem3_bench.serialization_arms import (
    Arm,
    Context,
    arm_groups,
    bwa_mem3_argv,
    flag_clashes,
    plan_tsv,
    prewarm_command,
    run_command,
)

PLAN_COLUMNS = 11
MODES = ("default", "fast")
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
    + ["-Mt4", "-SK", "-Mo", "--fa", "--ba", "--bam-t", "--", "--compat", "--compa=bwa-mem2"]
    + ["-5t4", "-5K100", "-1o", "-5SPt8", "-5pK1"],
)
def test_flag_clashes_rejects_benchmark_controls(token: str) -> None:
    assert flag_clashes([token])


@pytest.mark.parametrize(
    "tokens",
    [["-5"], ["-S"], ["-P"], ["-M"], ["-Y"], ["--meth"], ["--compat=bwa-mem2"], ["x"]]
    + [["--compat=off"], ["--compat-allow-divergent"], ["-xpacbio"], ["-xont2d"], ["-Rfoo"]]
    + [["-5SPR", "-t"], ["-R", "-o"], ["-5", "-x", "ont2d"]],
)
def test_flag_clashes_accepts_other_flags_and_option_values(tokens: list[str]) -> None:
    assert flag_clashes(tokens) == []


def test_flag_clashes_reports_each_clash_once() -> None:
    assert flag_clashes(["-5t4", "-M", "--fast"]) == [
        "'-5t4' sets -t",
        "'--fast' sets or abbreviates --fast, --bam or --bam-threads",
    ]


def test_arm_groups_pair_bwa_mem3_routes_and_drop_upstream_on_arm() -> None:
    tools = ("bwa-mem3", "bwa", "bwa-mem2", "minibwa")
    x86 = arm_groups(tools, ["default", "fast"], ["stock", "dense"], upstream=True)
    arm = arm_groups(tools, ["default", "fast"], ["stock", "dense"], upstream=False)
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


def _first_stage_argv(pipeline: str) -> list[str]:
    """The aligner argv of a pipeline's first stage, without the `set -o pipefail;` prefix."""
    argv = shlex.split(pipeline.split(" | ", 1)[0])
    assert argv[:3] == ["set", "-o", "pipefail;"]
    return argv[3 : argv.index("$IDX")]


def test_pinned_native_writes_level1_bam_through_cat_under_taskset() -> None:
    pipeline = _inner(run_command(Arm("bwa-mem3", "fast", "dense", "native"), "pinned", CTX))
    assert _first_stage_argv(pipeline) == [
        *("bwa-mem2.fg-labs", "mem", "-t", "6", "-K", "160000000", "-5", "-S"),
        *("-R", "@RG\\tID:a b;touch x", "--fast", "--bam=1", "--bam-threads", "2", "-o", "-"),
    ]
    assert pipeline.endswith('| cat > "$RUN_OUT"')
    assert "samtools" not in pipeline


def test_pinned_fast_sam_route_pipes_into_samtools() -> None:
    pipeline = _inner(run_command(Arm("bwa-mem3", "fast"), "pinned", CTX))
    assert _first_stage_argv(pipeline)[-3:] == ["-R", "@RG\\tID:a b;touch x", "--fast"]
    assert pipeline.split(" | ")[1:] == [
        "samtools view -@ 2 -O BAM,level=1 -o - -",
        'cat > "$RUN_OUT"',
    ]


def test_upstream_bwa_mem2_gets_mem_flags_but_not_fg_labs_flags() -> None:
    pipeline = _inner(run_command(Arm("bwa-mem2"), "pinned", CTX))
    assert _first_stage_argv(pipeline) == [
        *("bwa-mem2.upstream", "mem", "-t", "6", "-K", "160000000", "-5", "-S"),
    ]


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


def _without_threads(argv: list[str]) -> list[str]:
    i = argv.index("-t")
    return argv[:i] + argv[i + 2 :]


@pytest.mark.parametrize("mode", ["default", "fast"])
def test_fidelity_and_timing_run_the_same_bwa_mem3_command(mode: str) -> None:
    """Apart from `-t` and the output destination, fidelity's three commands are timing's."""
    timed = {
        None: _inner(run_command(Arm("bwa-mem3", mode), "pinned", CTX)),
        0: _inner(run_command(Arm("bwa-mem3", mode, route="native"), "practical", CTX)),
        1: _inner(run_command(Arm("bwa-mem3", mode, route="native"), "pinned", CTX)),
    }
    for level, pipeline in timed.items():
        timing = _first_stage_argv(pipeline)
        if level is not None:
            assert timing[-2:] == ["-o", "-"]
            timing = timing[:-2]
        fidelity = bwa_mem3_argv(CTX, mode, 16, level)
        assert _without_threads(fidelity) == _without_threads(timing)


def test_prewarm_reads_each_tools_own_index_files() -> None:
    expected = {
        "bwa": '"$IDX".bwt "$IDX".sa "$IDX".pac',
        "minibwa": '"$IDX".l2b "$IDX".mbw',
        "bwa-mem2": '"$IDX".0123 "$IDX".bwt.2bit.64 "$IDX".pac',
        "bwa-mem3": '"$IDX".0123 "$IDX".bwt.2bit.64 "$IDX".pac',
    }
    for tool, files in expected.items():
        assert prewarm_command(Arm(tool)) == f'cat {files} "$R1" "$R2" > /dev/null 2>&1 || true'


def _plan(endpoint: str = "pinned", reps: int = 4, seed: str = "abc1234") -> list[list[str]]:
    groups = arm_groups(
        ["bwa-mem3", "bwa", "bwa-mem2"], ["default", "fast"], ["stock", "dense"], upstream=True
    )
    return [line.split("\t") for line in plan_tsv(groups, endpoint, CTX, reps, seed).splitlines()]


def test_plan_columns_the_shell_reads_by_position() -> None:
    rows = _plan()
    assert all(len(row) == PLAN_COLUMNS for row in rows)
    by_label = {row[3]: row for row in rows}
    native = by_label["bwa-mem3-native-fast-dense"]
    assert native[4:9] == ["bwa-mem3", "fast", "dense", "native", "1"]
    assert native[9] == prewarm_command(Arm("bwa-mem3"))
    assert by_label["bwa"][4:9] == ["bwa", "default", "stock", "sam", "1"]
    assert by_label["bwa"][9] == prewarm_command(Arm("bwa"))
    assert {row[8] for row in _plan("practical")} == {"0"}


def test_plan_is_deterministic_and_seeded() -> None:
    assert _plan() == _plan()
    assert _plan() != _plan(seed="def5678")


def test_plan_runs_index_family_blocks_with_a_warmup_round_each() -> None:
    rows = _plan(reps=4)
    blocks = [row[0] for row in rows]
    order = list(dict.fromkeys(blocks))
    assert sorted(order) == ["bwa", "bwa-mem2-dense", "bwa-mem2-stock"]
    assert blocks == sorted(blocks, key=order.index), "each block runs contiguously"
    family = {
        "bwa": {"bwa"},
        "bwa-mem2-dense": {f"bwa-mem3-{r}-{m}-dense" for r in ("native", "sam") for m in MODES},
        "bwa-mem2-stock": {"bwa-mem2"}
        | {f"bwa-mem3-{r}-{m}-stock" for r in ("native", "sam") for m in MODES},
    }
    for block, labels in family.items():
        rounds = Counter(row[1] for row in rows if row[0] == block)
        assert rounds == {str(r): len(labels) for r in range(5)}
        for rnd in map(str, range(5)):
            members = [row[3] for row in rows if row[0] == block and row[1] == rnd]
            assert sorted(members) == sorted(labels), "each round is a permutation of the block"


def test_plan_keeps_pairs_adjacent_and_alternates_their_lead() -> None:
    rows = _plan(reps=4)
    for mode in MODES:
        for index in ("stock", "dense"):
            native, sam = (f"bwa-mem3-{r}-{mode}-{index}" for r in ("native", "sam"))
            leads = []
            for rnd in map(str, range(5)):
                labels = [row[3] for row in rows if row[1] == rnd and row[0].endswith(index)]
                i, j = labels.index(native), labels.index(sam)
                assert abs(i - j) == 1
                leads.append(i < j)
            assert all(a != b for a, b in zip(leads, leads[1:], strict=False)), leads
