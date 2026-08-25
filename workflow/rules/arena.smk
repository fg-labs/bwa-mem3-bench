"""The "arena": every blessed bwa-mem3 release, interleaved with lh3/bwa,
upstream bwa-mem2, and minibwa, on ONE fixed on-demand host per arch.

Why this exists. The regular sweep (`rule all`) measures ONE fg-labs SHA per
Batch run, on spot instances, and a release-over-release wall-time claim is
built by comparing two SEPARATE runs' recorded medians -- which can be weeks
apart, on different (spot) hosts. `fg-labs/bwa-mem3#92` and the c6a hic-1M
false regression (see CLAUDE.md) both turned out to be exactly this: a
measurement-substrate artifact, not a codegen change, and each needed an
expensive one-off bare-metal reproduction to settle. The arena makes that
reproduction a routine, repeatable part of every release bless: every arm
below runs on the SAME host, in the SAME job, so "is this release faster than
the last one" is answered by one wall-clock ratio measured under identical
conditions -- not by two medians that were never comparable in the first
place.

Scope, deliberately narrow. ONE sample (`config/defaults.yaml`'s
`arena.sample`, wgs-5M), TWO archs (c8a, c8g) -- see the CDK on-demand queues
below for why archs are capped, and AskUserQuestion scoping in the PR that
added this rule for why samples are capped to one. This is a
correctness-anchored progression view ALONGSIDE the cross-arch sweep, not a
replacement for it.

Why c8a, not c7i, for the x86 leg. `aws ec2 describe-instance-types` on
`.4xlarge`: c7i (Intel Sapphire Rapids) and m7i report `DefaultThreadsPerCore:
2` -- 16 vCPUs is 8 PHYSICAL cores under 2-way SMT. c8a (AMD, next-gen after
c7a's Genoa) and c8g (Graviton4) both report `DefaultThreadsPerCore: 1` and
`ValidThreadsPerCore: [1]` -- SMT isn't even an option, so 16 vCPUs IS 16
physical cores. `-t 16` on the old c7i leg was therefore 16 software threads
contending for 8 cores' worth of execution ports, while the c8g leg got 16
independent cores for the same thread count -- not an apples-to-apples core
count despite matching vCPU/thread numbers. c8a fixes that: both arena legs
now run `-t 16` on 16 real cores.

Scheduled first, not just included. `align_arena` carries `priority: 100`
(every other rule defaults to 0), so when `bless_release`'s full matrix
outstrips the profile's `jobs:` cap, snakemake's scheduler prefers to submit
the arena's jobs over other ready-but-lower-priority ones. It is the newest
and least battle-tested piece of the bless -- unlike the rest of the matrix,
it has never run against real historical binaries end to end -- so surfacing
its result (and its cost) early in a run is worth more than a first-in
DAG-order or alphabetical position would give it for free. This only affects
which ready job gets a submission slot next; it cannot make the arena's own
jobs finish before work the scheduler already started elsewhere.

The arm list. Hardcoded here (NOT config-driven, unlike `thread_scaling`'s
ladder) because it names literal binaries the builder base image bakes in:

  - `bwa`               -- lh3/bwa v{bwa_version} (timing only, matches the
                            existing `align_bwa` rule's own "wall-time only"
                            scope for a third-party comparator).
  - `bwa-mem2-upstream`  -- upstream bwa-mem2 v2.2.1. x86 ONLY -- upstream has
                            no ARM build (`_has_upstream_baseline`), so c8g's
                            arm list is one shorter than c8a's.
  - `minibwa`            -- lh3/minibwa (timing only, matches `align_minibwa`).
  - `v021` .. `v090`     -- every prior BLESSED bwa-mem3 release
                            (docs/release-allowances.yaml `to_sha`s), built
                            fresh in `docker/Dockerfile.base` and installed as
                            `bwa-mem3.<label>` -- see that file for why the
                            list lives there, inlined, rather than in a
                            separate COPY'd file. Each release also gets a
                            `<label>-fast` arm attempting `--fast` (fg-labs/
                            bwa-mem3 PR #189) on the SAME binary. `--fast`
                            postdates several of these releases, and this rule
                            has no reliable way to know which ones support it
                            without risking an expensive on-demand job on an
                            unsupported-flag crash -- so it does not try to
                            know: every `-fast` arm is wrapped by "Never
                            hard-fail on an old binary" below the same way the
                            default-mode arms already are, and a release that
                            predates `--fast` entirely just records a SKIPPED
                            row for its `-fast` arm rather than crashing the
                            job or needing a hand-maintained support list.
  - `fg-labs-default`,
    `fg-labs-fast`       -- today's candidate (`bwa-mem2.fg-labs`, the name
                            every other rule in this repo already installs it
                            under -- see docker/Dockerfile), in both modes.
                            Guaranteed to exist on the SHA being blessed, so
                            these two arms are never expected to SKIP.

Never hard-fail on an old binary. A flag or subcommand added after v0.2.1 (or
even a Makefile/ABI change severe enough to crash) is a real risk across a
multi-year release history, and the arena runs on a paid on-demand host --
losing the whole job to one old release's CLI drift would be an expensive way
to learn that. Each arm's alignment attempt is therefore wrapped so a failure
records a SKIPPED row and the loop continues, rather than the rule's `set -e`
aborting everything measured so far. This is a deliberate, narrow exception to
this codebase's normal fail-fast contract (see e.g. `align_fg_labs`'s
`set -o pipefail` discipline) -- justified here specifically because the
failure mode is "an ancient binary doesn't understand a flag", which carries
no ambiguity the way a silent partial output would.

Interleaved, with a discarded warmup cycle. The measured cycles run
REP-OUTER / ARM-INNER (rep 1 of every arm, then rep 2 of every arm, ...) so a
monotonic drift across the job's wall-clock (thermal throttling, a neighbour
arriving) is spread evenly across every arm instead of biasing whichever arm
happened to run first or last. One additional UNMEASURED warmup cycle runs
first (every arm once, discarded) -- mirrors the project's own bare-metal
reproduction protocol (CLAUDE.md's "reproduce with interleaved runs and
discarded warmups"), which caught page-cache and allocator warm-up trends
masquerading as a binary difference.

Correctness, narrowly scoped. Every arm is a WALL-TIME comparator except one
pairwise check: the run's own `fg-labs-default` BAM against the immediately
PRIOR blessed release's (the last entry in ARENA_RELEASES) default-mode BAM,
via `fgumi compare bams` -- the same boolean full-content identity tool
`docker/Dockerfile.base` documents for `--compat=bwa-mem2`. Extending this
pairwise check across the full 9-release history was considered and rejected:
the "every prior release" want is about TIMING progression (which the arm
list above already gives, wall-time-only, matching the minibwa precedent's
"output equivalency is out of scope" -- see CLAUDE.md's minibwa integration
notes), and older releases' `@SQ` dictionaries are increasingly likely to
violate `fgumi compare bams`'s header-compatibility precondition the further
back they go. One pairwise check against the immediate predecessor is the
question the release bless actually asks: did THIS release change behaviour.
Best-effort and non-blocking -- a header mismatch is recorded, not fatal.

Prewarm: cat, not shm. `align_fg_labs` excludes index load from the timed
region via `bwa-mem2 shm`, but `shm` may postdate some of the older releases
just as plausibly as `--fast` and `--bam=0` do (see above), and upstream
bwa-mem2 has never had it at all (`align_baseline`'s own docstring). So every
arm here -- bwa-mem3 (every release), bwa-mem2.upstream, bwa, and minibwa
alike -- is warmed the SAME way `align_baseline`/`align_bwa`/`align_minibwa`
already do: an untimed `cat` of each index family's sidecars into
`/dev/null` before the loop starts, and every timed run emits SAM piped
through `samtools view -u` rather than any binary's native BAM writer (which
`--bam=0` may also not be universal). This trades a little fidelity against
`align_fg_labs`'s numbers (which use the native writer) for one uniform
measurement technique every arm here can be compared against — the arena's
rows are compared against EACH OTHER, not against `trials.wall_seconds`.
"""

from collections import namedtuple

# label:sha for every historical release baked into the base image
# (docker/Dockerfile.base) -- oldest first, matching that file's list. Kept in
# sync there BY HAND: `base_image_tag()` content-addresses the base image tag
# over Dockerfile.base's own bytes (see bwa_mem3_bench/base_image.py), which is
# exactly why the list is inlined there rather than in a separate file this
# module could import -- see that Dockerfile's comment for the full rationale.
ARENA_RELEASES = [
    ("v021", "89bd589db9fcb56279912fa6b23e0831f4916a62"),
    ("v022", "bffae5a09267877fe514c458d4956b717bcefb8f"),
    ("v030", "a02fcb446574d5b5d03abdbf73c9b129deead2d4"),
    ("v040", "2681143bb7ab665488cdcd5d46380cc928f5bd05"),
    ("v050", "9dd30dd0e5e477ddfd33bec752179978ac9f5a1d"),
    ("v060", "48cf0a46824e26df2986efe940121d34b2cc7109"),
    ("v070", "04777b3c3f3c2f18d5838b6f4116015c7f5f2ad9"),
    ("394f8f8", "394f8f8110f7d15be7ef2ca38c335590aa1e0284"),
    ("v080", "4acb09562b5109e2f26d85b0158fde35d03a4fb8"),
    ("v090", "4d341b7ba81246509a87680fa569ac3210af540e"),
]

# The release immediately preceding today's candidate -- the one arm the
# fgumi correctness check runs against (see the module docstring). Always the
# LAST entry: ARENA_RELEASES is oldest-first.
ARENA_PRIOR_RELEASE_LABEL = ARENA_RELEASES[-1][0]

# ON-DEMAND queues, deliberately separate from the regular spot queues in
# config/archs.yaml (CONFIG.archs[arch].batch_queue): a spot reclaim mid-job
# would corrupt every arm's interleaved timing at once, the same failure mode
# `align_thread_scaling` avoids by running its coordinator on-demand. See
# cdk/stacks/batch_stack.py's ArenaCe for the compute-environment side.
#
# Derived from CONFIG.arena.archs (config/defaults.yaml), not a fixed literal
# dict: a hardcoded {"c7i": ..., "c8g": ...} here would accept a THIRD arch at
# config-validation time (`_arena_from` only checks it against the full
# `config/archs.yaml` registry) and then raise a bare KeyError building this
# rule's `resources.batch_queue`, or worse route to a nonexistent queue if the
# project name ever changed. The queue-name template
# (f"bwa-mem3-bench-{arch}-arena") must still match
# cdk/stacks/batch_stack.py's ARENA_ARCHS-derived queue names by hand -- CDK
# is a separate (non-Python-importable-from-here) stack, so that half of the
# contract can't be derived the same way; `tests/test_cdk_synth.py`'s
# `test_arena_queues_are_on_demand` pins the CDK side of it.
ARENA_QUEUES = {arch: f"bwa-mem3-bench-{arch}-arena" for arch in CONFIG.arena.archs}

# Matches `wildcard_constraints: arch = ...` below, so an arch config adds
# without a second hand-edit -- the same reasoning as ARENA_QUEUES above.
ARENA_ARCH_PATTERN = "|".join(CONFIG.arena.archs)

ARENA_SAMPLE = CONFIG.arena.sample
_arena_sample_cfg = CONFIG.samples[ARENA_SAMPLE]
ARENA_FG_LABS_FLAGS = _fg_labs_flags(ARENA_SAMPLE)
ARENA_MEM_FLAGS = _mem_flags(ARENA_SAMPLE)
ARENA_MINIBWA_FLAGS = " ".join(_arena_sample_cfg.minibwa_flags)
ARENA_BATCH_FLAG = _batch_flag()


def _arena_arms(arch: str) -> list[tuple[str, str, str]]:
    """Return (label, binary, mode) for every arm `arch` runs.

    `mode` is 'default' or 'fast'. Every historical release gets BOTH a
    default-mode arm (label `<release>`) and a `--fast` arm (label
    `<release>-fast`) on the same binary -- see the module docstring for why
    this needs no hand-maintained "which releases support --fast" list: a
    release that predates the flag just SKIPs its `-fast` arm via the same
    "never hard-fail on an old binary" wrapper the default-mode arms already
    rely on. bwa-mem2-upstream is dropped on ARM (upstream v2.2.1 has no ARM
    build), matching `_has_upstream_baseline`.
    """
    arms: list[tuple[str, str, str]] = [
        ("bwa", "bwa", "default"),
        ("minibwa", "minibwa", "default"),
    ]
    if _has_upstream_baseline(arch):
        arms.append(("bwa-mem2-upstream", BASELINE_BINARY, "default"))
    for label, _ in ARENA_RELEASES:
        arms.append((label, f"bwa-mem3.{label}", "default"))
        arms.append((f"{label}-fast", f"bwa-mem3.{label}", "fast"))
    arms += [
        ("fg-labs-default", "bwa-mem2.fg-labs", "default"),
        ("fg-labs-fast", "bwa-mem2.fg-labs", "fast"),
    ]
    return arms


def _arena_arm_spec(arch: str) -> str:
    """`_arena_arms` as `label|binary|mode` tokens for the shell loop."""
    return " ".join(f"{label}|{binary}|{mode}" for label, binary, mode in _arena_arms(arch))


# `_ref_inputs` / `_bwa_ref_inputs` / `_minibwa_ref_inputs` each take a
# wildcards object and read ONLY `.sample` from it. `align_arena`'s own
# wildcards carry `sha` and `arch` but no `sample` -- the arena pins ONE
# fixed sample (`CONFIG.arena.sample`), it is not a per-run selection -- so
# this fixed stand-in satisfies those helpers without adding a real `sample`
# wildcard the rule has no other use for.
_ArenaSampleWildcards = namedtuple("_ArenaSampleWildcards", ["sample"])
_ARENA_WC = _ArenaSampleWildcards(sample=ARENA_SAMPLE)


def _arena_ref_inputs(wc):
    """Union of every reference sidecar the arena's three index families need.

    Delegates to each family's own helper -- `_ref_inputs` (bwa-mem2/bwa-mem3,
    from align.smk), `_bwa_ref_inputs` (align_bwa.smk), `_minibwa_ref_inputs`
    (align_minibwa.smk) -- rather than restating the file lists, so a future
    change to any one family's sidecars (e.g. a new bwa-mem3 index file)
    reaches the arena automatically. All three compute the same plain-.fasta
    path first (same sample, same reference), so deduping preserves it at
    index 0, which the shell body relies on via `{input.ref[0]}`.

    `wc` (align_arena's real wildcards, `sha`/`arch` only) is accepted for
    signature compatibility with snakemake's `input:` calling convention but
    unused -- see `_ARENA_WC` above for why the sample is fixed instead.
    """
    seen: list[str] = []
    for path in (
        _ref_inputs(_ARENA_WC, meth_index="d3")
        + _bwa_ref_inputs(_ARENA_WC)
        + _minibwa_ref_inputs(_ARENA_WC)
    ):
        if path not in seen:
            seen.append(path)
    return seen


rule align_arena:
    input:
        ref = _arena_ref_inputs,
        fastqs = [f"{_arena_sample_cfg.source}{name}" for name in _arena_sample_cfg.fastq_names],
    output:
        tsv            = "arena/{sha}/{arch}/arena.tsv",
        profile        = "arena/{sha}/{arch}/runtime-profiles.tar.gz",
        meta           = "arena/{sha}/{arch}/meta.json",
        host_probe     = "arena/{sha}/{arch}/host-probe.jsonl",
        fgumi_compare  = "arena/{sha}/{arch}/fgumi-compare.txt",
    wildcard_constraints:
        arch = ARENA_ARCH_PATTERN,
    # Highest in the DAG (everything else defaults to 0): the arena is the
    # newest, least-proven piece of `bless_release` -- it has never run
    # against real historical binaries end to end. Preferring it whenever the
    # profile's `jobs:` cap forces a choice among ready jobs means its result
    # (and cost) lands early in a bless, rather than after hours of the rest
    # of the matrix have already run. This does not make the arena finish
    # first (its jobs still queue behind whatever the scheduler already
    # started), only that it is submitted first among what's ready -- see the
    # module docstring.
    priority: 100
    threads: CONFIG.arena.threads
    resources:
        batch_queue = lambda wc: ARENA_QUEUES[wc.arch],
        mem_mb = _mem_mb_for(ARENA_SAMPLE),
        container_image = lambda wc: image_for_arch(wc.arch),
        # The arena is long by construction: an interleaved warmup + N measured
        # cycles across 13-14 arms. The profile default (7200 s) can be tight
        # once reps > 3; match the ladder's own bump.
        runtime = 14400,
    params:
        arm_spec = lambda wc: _arena_arm_spec(wc.arch),
        reps = CONFIG.arena.reps,
        prior_label = ARENA_PRIOR_RELEASE_LABEL,
        probe_seconds = CONFIG.arena.host_probe_seconds,
        fg_labs_flags = ARENA_FG_LABS_FLAGS,
        mem_flags = ARENA_MEM_FLAGS,
        minibwa_flags = ARENA_MINIBWA_FLAGS,
        batch_flag = ARENA_BATCH_FLAG,
        sample = ARENA_SAMPLE,
    shell:
        r"""
        set -euo pipefail
        OUTDIR=$(dirname {output.tsv})
        mkdir -p "$OUTDIR/runs"

        emit-host-meta "{wildcards.sha}" "{params.sample}" "{wildcards.arch}" 0 > {output.meta}

        # Untimed page-cache prewarm across all three index families -- see the
        # module docstring for why this replaces `bwa-mem2 shm` here. `|| true`
        # on each: a missing sidecar (e.g. no upstream .bwt/.sa on an arch that
        # never stages them) must not fail the whole job over a warm-cache nicety.
        cat {input.ref[0]}.0123 {input.ref[0]}.bwt.2bit.64 {input.ref[0]}.pac \
            > /dev/null 2>/dev/null || true
        cat {input.ref[0]}.bwt {input.ref[0]}.sa {input.ref[0]}.pac \
            > /dev/null 2>/dev/null || true
        cat {input.ref[0]}.l2b {input.ref[0]}.mbw \
            > /dev/null 2>/dev/null || true

        emit-host-probe pre {params.probe_seconds} > {output.host_probe}

        printf 'label\tmode\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\n' > {output.tsv}

        run_arm() {{
            # Runs one (label, binary, mode), writing "${{out}}.timing.tsv" /
            # "${{out}}.stderr.log" / "${{out}}.bam.raw". Returns 1 on a
            # 0-record (or missing) BAM; the caller reads it under `set +e`,
            # deliberately suspended around this call -- see the module
            # docstring's "Never hard-fail on an old binary".
            local label="$1" binary="$2" mode="$3" rep="$4" out="$5"
            local cmd
            case "$binary" in
                bwa)
                    cmd="bwa mem -t {threads} {params.batch_flag} {params.mem_flags} {input.ref[0]} {input.fastqs}"
                    ;;
                minibwa)
                    cmd="minibwa map -t {threads} {params.minibwa_flags} {input.ref[0]} {input.fastqs}"
                    ;;
                bwa-mem2.fg-labs)
                    local fast_flag=""
                    [ "$mode" = "fast" ] && fast_flag="--fast"
                    # -v 3 is needed ONLY on the warmup invocation (rep=0), so
                    # it prints its resolved "phase-2 SMEM lockstep width"
                    # line (see the BWA3_SMEM_LOCKSTEP_N pin below). Measured
                    # reps must NOT get it: the verbose stderr writes happen
                    # inside the tricorder-timed region and can perturb the
                    # very wall_s values this pin exists to keep clean.
                    local verbosity_flag=""
                    [ "$rep" -eq 0 ] && verbosity_flag="-v 3"
                    cmd="$binary mem -t {threads} $verbosity_flag {params.batch_flag} {params.mem_flags} {params.fg_labs_flags} $fast_flag {input.ref[0]} {input.fastqs}"
                    ;;
                *)
                    # Historical bwa-mem3 releases and bwa-mem2-upstream: NEVER
                    # today's candidate's fg_labs_flags -- those are scoped to
                    # the CURRENT candidate's CLI surface (align.smk's
                    # _fg_labs_flags), not to an arbitrary older/foreign binary.
                    # bwa-mem2-upstream never gets a `mode = fast` arm (see
                    # _arena_arms), so $fast_flag is always empty for it -- this
                    # branch only ever adds --fast for a historical bwa-mem3
                    # release's `<label>-fast` arm. A release that predates the
                    # flag exits non-zero here, which `run_arm`'s caller turns
                    # into a SKIPPED row, not a job failure.
                    local fast_flag=""
                    [ "$mode" = "fast" ] && fast_flag="--fast"
                    cmd="$binary mem -t {threads} {params.batch_flag} {params.mem_flags} $fast_flag {input.ref[0]} {input.fastqs}"
                    ;;
            esac
            tricorder --out "${{out}}.timing.tsv" -- \
                bash -c "set -o pipefail; $cmd 2>'${{out}}.stderr.log' | samtools view -@4 -u -o '${{out}}.bam.raw' -"
            if [ "$(samtools view -c "${{out}}.bam.raw" 2>/dev/null || echo 0)" -eq 0 ]; then
                return 1
            fi
        }}

        # `{{params.arm_spec}}` is a Snakemake `.format()` field -- it is
        # substituted into this script's LITERAL SOURCE TEXT before bash ever
        # parses it, not expanded at runtime the way a shell variable is. Its
        # tokens are `|`-delimited (`label|binary|mode`), and `|` is a shell
        # metacharacter (pipe) ANYWHERE it appears in literal source text,
        # regardless of context -- `for entry in bwa|bwa|default ...; do` is
        # therefore a bash SYNTAX ERROR (confirmed live: this killed the
        # entire rule immediately, on the very first arm of the warmup cycle,
        # not a graceful per-arm SKIPPED row). Assigning it to a shell
        # variable FIRST and iterating over `$ARM_SPEC` unquoted sidesteps
        # this: unquoted VARIABLE expansion only word-splits on IFS, it never
        # re-tokenizes shell operators like `|` the way parsing literal
        # source text does.
        ARM_SPEC="{params.arm_spec}"

        # One UNMEASURED warmup cycle -- every arm once, discarded -- before
        # the interleaved measured cycles. See the module docstring.
        echo "=== warmup cycle ===" >&2
        for entry in $ARM_SPEC; do
            IFS='|' read -r label binary mode <<< "$entry"
            set +e
            run_arm "$label" "$binary" "$mode" 0 "$OUTDIR/runs/${{label}}.${{mode}}.warmup"
            status=$?
            set -e
            [ $status -ne 0 ] && echo "warmup: $label/$mode failed (exit=$status), ignoring" >&2
            rm -f "$OUTDIR/runs/${{label}}.${{mode}}.warmup.bam.raw"
        done

        # Pin today's candidate's phase-2 SMEM lockstep width (fg-labs/bwa-mem3
        # #393) from the warmup cycle's own natural probe, instead of re-probing
        # on every measured invocation. The probe runs once per process during
        # index setup, before the binary's own PROCESS() timer starts, so it is
        # invisible to process_s -- but NOT to wall_s, and it costs a much bigger
        # fraction of a short --fast run's wall time than a long stock run's,
        # which otherwise makes v0.10.0+'s --fast arm look artificially worse
        # against an older release with no such probe. Every release before
        # #393 predates the feature entirely and simply never reads this env
        # var, so exporting it for the whole rest of the script is a no-op for
        # every other arm -- safe to set unconditionally, not just around the
        # fg-labs invocations.
        #
        # Clear any INHERITED BWA3_SMEM_LOCKSTEP_N before parsing the warmup
        # log. Without this, a value already present in the job's environment
        # (e.g. a worker host that leaked one from a prior invocation) would
        # silently survive into the measured cycles below whenever THIS run's
        # warmup log is missing or lacks a probe line -- masquerading as a
        # pin from a probe that never actually ran this time.
        unset BWA3_SMEM_LOCKSTEP_N
        FG_LABS_WARMUP_LOG="$OUTDIR/runs/fg-labs-default.default.warmup.stderr.log"
        if [ -f "$FG_LABS_WARMUP_LOG" ]; then
            FG_LABS_LOCKSTEP_N=$(grep -oE 'phase-2 SMEM lockstep width: [0-9]+' "$FG_LABS_WARMUP_LOG" \
                | grep -oE '[0-9]+$' | tail -1 || true)
            if [ -n "$FG_LABS_LOCKSTEP_N" ]; then
                export BWA3_SMEM_LOCKSTEP_N="$FG_LABS_LOCKSTEP_N"
                echo "pinned BWA3_SMEM_LOCKSTEP_N=$FG_LABS_LOCKSTEP_N from the warmup probe" >&2
            else
                echo "WARNING: could not find a phase-2 SMEM lockstep width line in $FG_LABS_WARMUP_LOG -- today's candidate's measured --fast rows will pay the per-invocation probe cost" >&2
            fi
        else
            echo "WARNING: $FG_LABS_WARMUP_LOG missing -- fg-labs-default's warmup likely failed; today's candidate's measured --fast rows will pay the per-invocation probe cost" >&2
        fi

        # Interleaved measured cycles: REP-OUTER / ARM-INNER, so a monotonic
        # drift across the job's wall-clock lands on every arm equally rather
        # than biasing whichever ran first or last.
        for rep in $(seq 1 {params.reps}); do
            echo "=== measured cycle rep $rep ===" >&2
            for entry in $ARM_SPEC; do
                IFS='|' read -r label binary mode <<< "$entry"
                OUT="$OUTDIR/runs/${{label}}.${{mode}}.rep${{rep}}"
                set +e
                run_arm "$label" "$binary" "$mode" "$rep" "$OUT"
                status=$?
                set -e
                if [ $status -ne 0 ]; then
                    echo "WARNING: arm=$label mode=$mode rep=$rep FAILED (exit=$status) -- likely an unsupported flag/subcommand on an old binary; recording SKIPPED and continuing" >&2
                    printf '%s\t%s\t%s\tNA\tNA\tNA\tNA\n' "$label" "$mode" "$rep" >> {output.tsv}
                    rm -f "${{OUT}}.bam.raw"
                    continue
                fi
                WALL=$(mawk 'NR==2{{print $1}}' "${{OUT}}.timing.tsv")
                RSS=$(mawk 'NR==2{{print $3}}' "${{OUT}}.timing.tsv")
                CPU=$(mawk 'NR==2{{print $10}}' "${{OUT}}.timing.tsv")
                PROC=$(grep -oE 'PROCESS\(\).*?:[[:space:]]*[0-9.]+' "${{OUT}}.stderr.log" \
                       | grep -oE '[0-9.]+$' | head -1 || true)
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$label" "$mode" "$rep" "$WALL" "$CPU" "$RSS" "${{PROC:-NA}}" >> {output.tsv}
                # Tee the same row to stderr (-> CloudWatch) so a result is
                # visible the moment an arm finishes, not just when the whole
                # job completes and arena.tsv finally reaches S3 -- Snakemake's
                # storage plugin uploads declared outputs on rule completion,
                # not incrementally, so without this line there is no way to
                # see a single number until the entire multi-hour job is done.
                echo "RESULT: label=$label mode=$mode rep=$rep wall_s=$WALL cpu_s=$CPU max_rss_mb=$RSS process_s=${{PROC:-NA}}" >&2
                # Keep the LAST measured rep's BAM for the two arms the
                # correctness check compares -- default mode only (--fast
                # prunes the candidate set on purpose, so its BAM is not a
                # meaningful fgumi comparator -- see CLAUDE.md's vs_default note).
                if [ "$rep" -eq {params.reps} ] && [ "$mode" = "default" ] \
                   && {{ [ "$label" = "fg-labs-default" ] || [ "$label" = "{params.prior_label}" ]; }}; then
                    cp "${{OUT}}.bam.raw" "$OUTDIR/keep.${{label}}.bam"
                fi
                rm -f "${{OUT}}.bam.raw"
            done
        done

        emit-host-probe post {params.probe_seconds} >> {output.host_probe}

        # Correctness spot-check: today's candidate vs the immediately prior
        # blessed release, default mode, boolean full-content identity. Never
        # gates the job -- see the module docstring's "Correctness, narrowly
        # scoped" for why this is the one pairwise check the arena runs.
        KEEP_FG="$OUTDIR/keep.fg-labs-default.bam"
        KEEP_PRIOR="$OUTDIR/keep.{params.prior_label}.bam"
        if [ -s "$KEEP_FG" ] && [ -s "$KEEP_PRIOR" ]; then
            set +e
            fgumi compare bams "$KEEP_FG" "$KEEP_PRIOR" --threads 4 --max-diffs 20 \
                > {output.fgumi_compare} 2>&1
            set -e
        else
            echo "fgumi correctness check skipped: fg-labs-default or {params.prior_label} BAM missing (see arena.tsv for which rep failed)" \
                > {output.fgumi_compare}
        fi
        rm -f "$KEEP_FG" "$KEEP_PRIOR"

        tar -czf {output.profile} -C "$OUTDIR" runs
        """
