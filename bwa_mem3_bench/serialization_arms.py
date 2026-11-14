"""Arms, shell commands and run order for the serialization rules.

`workflow/rules/serialization.smk` renders the fidelity commands from `bwa_mem3_argv` and writes
the timing plan built here (`plan_tsv`) into the job, then loops over it. Every config-derived
token is quoted with `shlex`. The index, reads and per-run output paths are referenced as
`"$IDX"`, `"$R1"`, `"$R2"`, `"$RUN_OUT"` and `"$RUN_ERR"`, which the shell loop exports before
each run.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from bwa_mem3_bench.arena_arms import build_schedule

MODES = ("default", "fast")
ENDPOINTS = ("pinned", "unpinned", "practical")
TOOLS = ("bwa-mem3", "bwa", "bwa-mem2", "minibwa")
INDEXES = ("stock", "dense")
COMPARATORS = ("bwa", "minibwa", "bwa-mem2")

# bwa-mem3 `mem`'s getopt option string (src/fastmap.cpp); a letter before `:` takes an argument.
MEM_OPTSTRING = "51qpaMCSPVYjuk:c:v:s:r:t:R:A:B:O:E:U:w:L:d:T:Q:D:m:I:N:W:x:G:h:y:K:X:H:o:f:z:"
_TAKES_ARG = frozenset(
    c for c, nxt in zip(MEM_OPTSTRING, MEM_OPTSTRING[1:] + " ", strict=True) if nxt == ":"
)
_CONTROLLED_SHORT = "tKof"
_CONTROLLED_LONG = ("fast", "bam", "bam-threads")


def flag_clashes(tokens: Sequence[str]) -> list[str]:
    """Why each flag in `tokens` clashes with an option the benchmark sets itself.

    Tokens are read as getopt reads them: a short bundle (`-5St4`) is one option per character
    until an option that takes an argument, whose value is the rest of the token or, if nothing
    is left, the next token. Long options match by prefix, as getopt_long allows. `--compat` must
    be spelled `--compat=<target>` so that `Sample.compat_target` sees it.
    """
    problems: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token.startswith("--"):
            name = token[2:].split("=", 1)[0]
            if not name or any(option.startswith(name) for option in _CONTROLLED_LONG):
                problems.append(f"{token!r} sets or abbreviates --fast, --bam or --bam-threads")
            elif "compat".startswith(name) and not token.startswith("--compat="):
                problems.append(f"{token!r} must be spelled --compat=<target>")
            continue
        if not token.startswith("-") or token == "-":
            continue
        for j, option in enumerate(token[1:], start=2):
            if option in _CONTROLLED_SHORT:
                problems.append(f"{token!r} sets -{option}")
                break
            if option in _TAKES_ARG:
                if j == len(token):
                    i += 1  # the value is the next token
                break
    return problems


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
    """Arms in run-together groups: each bwa-mem3 (native, SAM) pair is one group.

    bwa, minibwa and upstream bwa-mem2 (only where `upstream`, i.e. x86) run the SAM route on the
    stock index in default mode, so they need `default` and `stock` to be selected.
    """
    tools, modes, indexes = list(tools), list(modes), list(indexes)
    groups: list[tuple[Arm, ...]] = []
    if "bwa-mem3" in tools:
        groups += [
            (Arm("bwa-mem3", m, i, "native"), Arm("bwa-mem3", m, i)) for m in modes for i in indexes
        ]
    if "default" in modes and "stock" in indexes:
        groups += [(Arm(t),) for t in COMPARATORS if t in tools and (upstream or t != "bwa-mem2")]
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


def bwa_mem3_argv(ctx: Context, mode: str, threads: int, level: int | None = None) -> list[str]:
    """The bwa-mem3 `mem` argv shared by fidelity and timing, up to the output destination.

    SAM to stdout when `level` is None, else native BAM at that compression level (`--bam=1`
    compresses on `ctx.helpers` threads).
    """
    fast = ["--fast"] if mode == "fast" else []
    argv = [ctx.aligner, "mem", "-t", str(threads), "-K", str(ctx.batch_k), *ctx.mem_flags]
    argv += [*ctx.fg_labs_flags, *fast]
    if level == 0:
        argv.append("--bam=0")
    elif level == 1:
        argv += ["--bam=1", "--bam-threads", str(ctx.helpers)]
    return argv


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
    threads = ctx.budget if practical else ctx.budget - ctx.helpers
    if arm.tool == "bwa-mem3":
        level = None if arm.route == "sam" else 0 if practical else 1
        argv = bwa_mem3_argv(ctx, arm.mode, threads, level)
        argv += ["-o", "-"] if level is not None else []
    elif arm.tool == "minibwa":
        argv = ["minibwa", "map", "-t", str(threads), *ctx.minibwa_flags]
    else:
        binary = "bwa" if arm.tool == "bwa" else ctx.upstream
        argv = [binary, "mem", "-t", str(threads), "-K", str(ctx.batch_k), *ctx.mem_flags]
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
    """Every run in execution order, scheduled by the arena's `build_schedule`.

    Arms are blocked by index family; each block runs a warmup round 0 then `reps` rounds, each
    round a seeded permutation of the block's arms in which every bwa-mem3 (native, SAM) pair
    runs back to back, alternating which leads. Columns: block round position label tool mode
    index route writes_file prewarm command.
    """
    arms = {arm.label: arm for group in groups for arm in group}
    binaries = {"bwa": "bwa", "minibwa": "minibwa", "bwa-mem2": ctx.upstream}
    schedule = build_schedule(
        [(a.label, binaries.get(a.tool, ctx.aligner), a.mode) for a in arms.values()],
        dense_labels=frozenset(label for label, a in arms.items() if a.index == "dense"),
        pairs=[(group[0].label, group[1].label) for group in groups if len(group) == 2],  # noqa: PLR2004
        candidate_label=next(iter(arms)),
        seed=seed,
        headline_rounds=reps,
        context_rounds=reps,
    )
    writes = "0" if endpoint == "practical" else "1"
    lines = []
    for run in schedule:
        a = arms[run.label]
        fields = [run.block, str(run.round), str(run.position), a.label, a.tool, a.mode, a.index]
        lines.append(
            _row([*fields, a.route, writes, prewarm_command(a), run_command(a, endpoint, ctx)])
        )
    return "\n".join(lines)
