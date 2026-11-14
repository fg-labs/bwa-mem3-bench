"""Arms, shell commands and run order for the serialization timing rule.

`workflow/rules/serialization.smk` writes the plan built here (`plan_tsv`) into the job and loops
over it. Every config-derived token is quoted with `shlex`. The index, reads and per-run output
paths are referenced as `"$IDX"`, `"$R1"`, `"$R2"`, `"$RUN_OUT"` and `"$RUN_ERR"`, which the
shell loop exports before each run.
"""

from __future__ import annotations

import random
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MODES = ("default", "fast")
ENDPOINTS = ("pinned", "unpinned", "practical")
TOOLS = ("bwa-mem3", "bwa", "bwa-mem2", "minibwa")
INDEXES = ("stock", "dense")


def flag_clash(token: str) -> str | None:
    """Why a sample flag token clashes with an option the benchmark sets itself, else None."""
    if token.startswith("--"):
        name = token.split("=", 1)[0]
        prefix = len(name) > 2 and ("--fast".startswith(name) or "--bam".startswith(name))  # noqa: PLR2004
        if name.startswith("--bam") or prefix:
            return "sets or abbreviates an option the benchmark controls (--fast, --bam*)"
        return None
    if re.match(r"-[A-Za-z]*[tKof]", token):
        return "sets -t, -K, -o or -f (alone or bundled), which the benchmark controls"
    return None


@dataclass(frozen=True)
class Arm:
    """One timed configuration: a tool, mode, index and output route (`native` or `sam`)."""

    tool: str
    mode: str = "default"
    index: str = "stock"
    route: str = "sam"

    @property
    def label(self) -> str:
        """Unique within a job: the tool alone for comparators, everything for bwa-mem3."""
        if self.tool != "bwa-mem3":
            return self.tool
        return f"bwa-mem3-{self.route}-{self.mode}-{self.index}"


def arm_groups(
    tools: Iterable[str], modes: Iterable[str], indexes: Iterable[str], *, upstream: bool
) -> list[tuple[Arm, ...]]:
    """Arms in run-together groups: each bwa-mem3 native/SAM pair stays adjacent in a round.

    bwa, minibwa and upstream bwa-mem2 (only where `upstream`, i.e. x86) run the SAM route on the
    stock index in default mode, so they need `default` and `stock` to be selected.
    """
    modes, indexes = list(modes), list(indexes)
    groups: list[tuple[Arm, ...]] = []
    if "bwa-mem3" in tools:
        groups += [
            (Arm("bwa-mem3", m, i, "native"), Arm("bwa-mem3", m, i)) for m in modes for i in indexes
        ]
    if "default" in modes and "stock" in indexes:
        comparators = ("bwa", "minibwa", "bwa-mem2") if upstream else ("bwa", "minibwa")
        groups += [(Arm(tool),) for tool in comparators if tool in tools]
    return groups


@dataclass(frozen=True)
class Context:
    """Everything a command needs besides the arm and the paths: binaries, flags and threads."""

    aligner: str
    upstream: str
    batch_k: int
    budget: int
    helpers: int
    mem_flags: tuple[str, ...] = ()
    fg_labs_flags: tuple[str, ...] = ()
    minibwa_flags: tuple[str, ...] = ()


def _aligner_argv(arm: Arm, ctx: Context, threads: int) -> list[str]:
    head = ["-t", str(threads)]
    if arm.tool == "minibwa":
        return ["minibwa", "map", *head, *ctx.minibwa_flags]
    head += ["-K", str(ctx.batch_k), *ctx.mem_flags]
    if arm.tool == "bwa-mem3":
        fast = ["--fast"] if arm.mode == "fast" else []
        return [ctx.aligner, "mem", *head, *ctx.fg_labs_flags, *fast]
    return ["bwa" if arm.tool == "bwa" else ctx.upstream, "mem", *head]


def prewarm_command(arm: Arm) -> str:
    """Untimed page-cache top-off of the arm's own index files and the reads."""
    suffixes = {"bwa": (".bwt", ".sa", ".pac"), "minibwa": (".l2b", ".mbw")}.get(
        arm.tool, (".0123", ".bwt.2bit.64", ".pac")
    )
    files = " ".join(f'"$IDX"{suffix}' for suffix in suffixes)
    return f'cat {files} "$R1" "$R2" > /dev/null 2>&1 || true'


def run_command(arm: Arm, endpoint: str, ctx: Context) -> str:
    """The command one timed run executes (tricorder wraps it in `bash -c`).

    pinned / unpinned: level-1 BAM written through `| cat > "$RUN_OUT"`, the aligner on
    `budget - helpers` threads and the BAM compression on `helpers`. practical: bwa-mem3 native
    `--bam=0`, everything else SAM, into `cat > /dev/null`, aligner on `budget` threads.
    pinned and practical run under `taskset` on the first `budget` CPUs.
    """
    practical = endpoint == "practical"
    argv = _aligner_argv(arm, ctx, ctx.budget if practical else ctx.budget - ctx.helpers)
    if arm.route == "native":
        level = ["--bam=0"] if practical else ["--bam=1", "--bam-threads", str(ctx.helpers)]
        argv += [*level, "-o", "-"]
    stages = [f'{shlex.join(argv)} "$IDX" "$R1" "$R2" 2> "$RUN_ERR"']
    if arm.route == "sam" and not practical:
        stages.append(f"samtools view -@ {ctx.helpers} -O BAM,level=1 -o - -")
    stages.append("cat > /dev/null" if practical else 'cat > "$RUN_OUT"')
    pipeline = "set -o pipefail; " + " | ".join(stages)
    if endpoint == "unpinned":
        return pipeline
    return shlex.join(["taskset", "-c", f"0-{ctx.budget - 1}", "bash", "-c", pipeline])


def _row(fields: Sequence[str]) -> str:
    if any("\t" in field or "\n" in field or not field for field in fields):
        raise ValueError(f"serialization: a plan field is empty or holds a tab/newline: {fields}")
    return "\t".join(fields)


def plan_tsv(
    groups: Sequence[tuple[Arm, ...]], endpoint: str, ctx: Context, reps: int, seed: str
) -> str:
    """Every run in execution order: round 0 (warmup) then `reps` rounds, order seeded per round.

    Columns: round position label tool mode index route writes_file prewarm command.
    """
    writes = "0" if endpoint == "practical" else "1"
    lines = []
    for rnd in range(reps + 1):
        rng = random.Random(f"{seed}:{rnd}")  # noqa: S311 -- run order, not security
        order = [list(group) for group in groups]
        rng.shuffle(order)
        for group in order:
            rng.shuffle(group)
        for pos, a in enumerate(a for group in order for a in group):
            fields = [str(rnd), str(pos), a.label, a.tool, a.mode, a.index, a.route, writes]
            lines.append(_row([*fields, prewarm_command(a), run_command(a, endpoint, ctx)]))
    return "\n".join(lines)
