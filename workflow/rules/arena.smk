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
`arena.sample`, wgs-5M), TWO archs (m8a, m8g) -- see the CDK on-demand queues
below for why archs are capped, and AskUserQuestion scoping in the PR that
added this rule for why samples are capped to one. This is a
correctness-anchored progression view ALONGSIDE the cross-arch sweep, not a
replacement for it.

Why m8a / m8g (64 GB), not c8a/c8g (32 GB) or c7i. `aws ec2
describe-instance-types` on `.4xlarge`: c7i (Intel Sapphire Rapids) and m7i
report `DefaultThreadsPerCore: 2` -- 16 vCPUs is 8 PHYSICAL cores under 2-way
SMT. c8a/c8g AND their general-purpose siblings m8a (AMD, same family as c8a) /
m8g (Graviton4, same family as c8g) all report `ValidThreadsPerCore: [1]` --
SMT isn't even an option, so 16 vCPUs IS 16 physical cores. `-t 16` on the old
c7i leg was therefore 16 software threads contending for 8 cores' worth of
execution ports, while a no-SMT leg gets 16 independent cores for the same
thread count. The arena moved off c7i to c8a/c8g for that reason, and then off
c8a/c8g (32 GB) to m8a/m8g (64 GB) for a second: the `re-sa -u 1` stride-2 arm
(see "SA densification" below) needs ~27-28 GB resident, which does not fit
under a 32 GB host / 28 GB cgroup. m8a/m8g change ONLY RAM-per-core (2 -> 4
GiB/vCPU) versus c8a/c8g -- same core count, same no-SMT, same CPU family -- and
the extra RAM also lets the `cat` prewarm finally hold the heavy arms fully warm
(upstream's ~21 GB index needs ~42 GB of cache+RSS, which never fit on 32 GB).
TRADE-OFF: unlike c8g, m8a/m8g are NOT cross-arch sweep archs, so the arena no
longer runs on the exact hardware the spot sweep uses -- accepted to fit the
dense-SA arm.

SA densification (fg-labs/bwa-mem3#510). Arms newer than v0.12.0 align against
a stride-2 `re-sa -u 1` copy of the index (`hg38-u1`), staged
SEPARATELY from the stock stride-8 index so older fg-labs releases, upstream
bwa-mem2, lh3/bwa and minibwa -- which may not parse a densified on-disk SA
table -- keep the stock one. The candidate is always a dense arm; historical
release arms cross the threshold as they age past v0.12.0. Byte-identical
alignment output (only SA-resolve speed changes), so the fgumi correctness
check below is unaffected. See `_arm_uses_dense_sa` / `_arena_dense_ref_inputs`;
set `arena.dense_sa_shift: 3` to turn it off (all arms on the stock index).

WHAT THE CANDIDATE-VS-PRIOR RATIO MEANS with this on. The candidate (dense) is
compared against the prior release (v0.12.0, stock stride-8), so its headline
`vs_prior_release` speedup folds in the ~4% SA-resolve win from the denser
index ON TOP OF any codegen delta -- it is a SHIPPED-CONFIGURATION comparison,
not the pure-codegen, identical-conditions delta the arena gives between two
stock arms. This is deliberate: `> v0.12.0` releases are recommended to run
`-u 1`, so the arena measures them as they will run. Two consequences to keep
straight: (1) once the prior release is itself `> v0.12.0` (both arms dense),
the ratio is codegen-only again; (2) to isolate codegen for a candidate whose
prior is stock, read the STOCK sibling arms, or set `dense_sa_shift: 3`.

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
                            no ARM build (`_has_upstream_baseline`), so m8g's
                            arm list is one shorter than m8a's.
  - `minibwa`            -- lh3/minibwa (timing only, matches `align_minibwa`).
  - `v021` .. latest     -- every prior BLESSED bwa-mem3 release, one per
                            `ARENA_RELEASES` entry
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

Schedule: paired rounds inside index-family blocks. The arena's headline
number is a candidate-vs-prior-release ratio, and it is only sound if whatever
slowly-varying host state exists at a given moment hits BOTH arms equally. The
schedule (`bwa_mem3_bench.arena_arms.build_schedule`, emitted as this rule's
`arm_spec` param, seeded by the fg-labs SHA) is built for that:

  - Arms are grouped into BLOCKS by index family (densified bwa-mem3, stock
    bwa-mem3 + upstream bwa-mem2, bwa, minibwa), in shuffled block order. Only
    arms reading the same index can alternate without each run evicting the
    next one's page cache; round-robin across families would make every run a
    cold first rep.
  - Each block runs one discarded WARMUP round (round 0) and then N measured
    ROUNDS: `arena.reps` for the candidate's block, `arena.context_reps` for
    every other block. Every arm runs once per round, in a fresh seeded order.
  - The candidate and the prior release (and their `--fast` siblings) always
    run BACK-TO-BACK within a round, alternating which goes first, so each
    round yields one paired ratio. The report's `vs_prior_release` is the
    median of those per-round ratios (`report/arena.py`).

History, because each of these replaced a design that looked sound. A fixed
rep-outer / arm-inner order always measured the newest release last, under the
worst accumulated drift: a c8a run showed `process_s` flat between v0.9.0 and
v0.10.0's `--fast` arms while their `wall_s` "outside-process" gap grew from
~1.3 s to ~13.5 s over the job -- a false regression. Its replacement ran every
rep of one label back-to-back and shuffled only the label ORDER, so all of a
label's reps shared one moment of host state. The 0a65bec mock bless (2026-11)
caught that: on m8a the candidate's three reps all ran with 1.4-1.9 GiB of heap
on 4 KB pages and BSW ~17% slower, while the SAME binary's pin-probe run earlier
in the same job measured PROCESS() 16.80 s against 18.27-18.76 s. The reported
1.02x was a position effect; the prior release's reps 2-3 had huge pages.

Warmup: per-run cat, not per-label alignment. An untimed page-cache top-off
(`prewarm_arm`: `cat` of that arm's own index files + the fastqs,
`arena.prewarm_passes` times) runs immediately before EVERY run. bwa-mem2 and
every bwa-mem3 release `_mm_malloc` a fresh heap buffer and `fread` the index
into it (`FMI_search.cpp`'s `load_index()`), so index load transiently needs
close to 2x the index size, and on a memory-constrained host one untimed pass
does not converge (a real 32 GB c8a run's `io_in` took 3-4 passes). A `cat` has
near-zero RSS, so it can spend the whole page-cache budget on the file it is
warming. The second pass is there because pages touched once sit on the
kernel's inactive LRU and are the first evicted when the next run's ~27 GB heap
forces reclaim; in the 0a65bec run every label's first rep after an
index-family change read the index cold (mean index-read 2.87 s vs 0.52 s).

Block boundaries: balloon. At the start of every block the rule allocates,
touches and frees `arena.balloon_mb` of anonymous memory (`dd ... bs=<N>M`),
which evicts the previous family's page cache and leaves the next block
contiguous free memory for transparent huge pages. It needs no privileges,
unlike `drop_caches` / `compact_memory`.

Instrumentation, per run. Every `arena.tsv` row carries its schedule position
(`rep` = round, `block`, `position`), the output `writer`, and the host state it
ran under: the binary's own index-read time, tricorder's `io_in` and starting
page-cache size, `/proc/vmstat` deltas over the run (`thp_fault_fallback`,
`compact_stall`, `pgmajfault`) and `MemFree` / `AnonHugePages` at its start.
These come from the kernel, not the binary, so they are recorded for every
release, old or new. The report flags a COLD run (index read far above its
block's median, or more than 1 GB of disk input) and drops that round's pair,
so a confound becomes visible instead of being folded into a median. A
per-round tachyon probe (phase `round-<block>-<n>`) and `/proc/meminfo`
snapshot are appended to `host-probe.jsonl` / `meminfo.jsonl`.

The SMEM-lockstep pin (below) comes from a dedicated probe invocation, not any
arm's measured run, so it is set before the first block whatever the schedule.
The probe is a full candidate alignment, so it is skipped -- and every arm runs
unpinned -- when `--config arena_labels=` leaves no candidate arm.

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
region via `bwa-mem2 shm`, but `shm` support is not uniform across every arm
here: `bwa` and every tracked bwa-mem3 release support it, but `minibwa` and
upstream bwa-mem2 do not (confirmed directly against their sources -- no
`shm`-related symbol exists in either). Using it selectively would change
what the cross-tool ratios (`vs bwa`, `vs bwa-mem2`, `vs minibwa`) actually
measure, so every arm is warmed the SAME way `align_baseline`/`align_bwa`/
`align_minibwa` already are: an untimed `cat` of that arm's own index family
into `/dev/null` before every run (see "Warmup" above).

Output: every arm produces uncompressed BAM, by the cheapest route the tool
has, streamed through a pipe into `cat > file`. Every bwa-mem3 build writes it
natively (`--bam=0 -o -`), as the sweep (`align.smk`) and the thread-scaling
ladder (`scaling.smk`) also time it, through the same `cat`;
`--bam` predates the oldest tracked release (fg-labs/bwa-mem3#12, before
v0.2.1). bwa, upstream bwa-mem2 and minibwa only emit SAM, so their route is
SAM piped through `samtools view -u`, and that cost is part of what using them
costs. Until 2026-11 every arm, bwa-mem3 included, went through the SAM pipe:
bwa-mem3 paid SAM formatting it never pays in real use (WORKER_SAM was 5.5 of
18.7 s PROCESS() on m8a), and output-path changes such as fg-labs/bwa-mem3#652
were invisible here. Arena rows before that change are a different method; do
not compare their absolute walls with later rows. `arena.sam_writer_control`
adds `<label>-sam` control arms that run the candidate and prior release
through the SAM pipe, to publish the difference.

Why the final writer is `cat`. htslib's `bgzf_close` fdatasync()s an output
that is a regular file. `samtools view -@4` uses htslib's multithreaded BGZF
writer, which syncs every 512 blocks DURING the run, but bwa-mem3's default
single-threaded writer leaves the whole ~3.5 GB to close. Measured on m8a with
a 500 MB/s gp3 volume (2026-11): native `--bam=0 -o file` sat idle ~6 s after
PROCESS() ended and lost to the SAM pipe end to end (24.1 vs 19.1 s) despite a
faster PROCESS() -- the arena was timing the disk. Writing through a pipe
removes the sync for every arm alike.
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
    ("v100", "371a1819802c2962b768c3b165f0d1319a6a75b3"),
    ("v110", "3c355b854e3180b1d8a0745a91a8b72716009ef5"),
    ("v120", "2e6627615ae5c50c5d12829ce9a3900f05dd58ff"),
    ("v130", "64420af4d1672350b734b9de49cae3933fb0cb8c"),
    ("v140", "1a6a9d56c5d2acf186071d90a2084e59743152c7"),
    ("v141", "e974f2666bae714afd942faf58e1cb27078b3fef"),
    ("v150", "cbe2db325cf057eff03a7ffb3ebd629453d0fd49"),
    ("v151", "a6b2ba1eb5cda2efbd45d22e2010b8afc87f928e"),
    ("v160", "0a65bec1f47f8b773cf40b85d16833de010c8107"),
    ("v170", "1eaeb8ee31f997a14c82c888efc0b43329a6b9c4"),
]

# The release immediately preceding today's candidate -- the one arm the
# fgumi correctness check runs against (see the module docstring). Always the
# LAST entry: ARENA_RELEASES is oldest-first.
ARENA_PRIOR_RELEASE_LABEL = ARENA_RELEASES[-1][0]

# The release ledger, read only for the prior release's `aliases` (its tag
# commit and any other SHA with the same code), so the candidate guard in
# `_arena_schedule` also refuses that release under another name. The image ships
# docs/release-allowances.yaml for this (see docker/Dockerfile).
_ARENA_LEDGER = load_allowances(DEFAULT_ALLOWANCES_PATH)

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
# `test_arena_queues_are_on_demand` pins the CDK side of it. This native
# name / CDK-match invariant applies only to the empty-`BATCH_QUEUE_PREFIX`
# case: `arena_queue_for` (just below) wraps this dict to honour a control
# plane's prefix, and those queues are that control plane's responsibility.
ARENA_QUEUES = {arch: f"bwa-mem3-bench-{arch}-arena" for arch in CONFIG.arena.archs}


def arena_queue_for(arch_name: str) -> str:
    """The on-demand Batch queue an arena arm runs on, honouring ``BATCH_QUEUE_PREFIX``.

    The arena counterpart of ``batch_queue_for`` (align.smk): an external control
    plane that provisions its OWN queues under a different namespace redirects the
    arena the same way it redirects the spot sweep, by setting
    ``--config batch_queue_prefix=<prefix>``. The ``-arena`` suffix is preserved,
    so arch ``m8a`` routes to ``<prefix>m8a-arena``. Empty (the default) keeps the
    native ``bwa-mem3-bench-<arch>-arena`` name from ``ARENA_QUEUES``, so behaviour
    under this repo's own profile is unchanged. Whatever queue this resolves to
    must be ON-DEMAND (a spot reclaim mid-run corrupts the interleaved timing);
    that is the control plane's responsibility, exactly as it is the CDK stack's
    for the native queues.
    """
    if BATCH_QUEUE_PREFIX:
        return f"{BATCH_QUEUE_PREFIX}{arch_name}-arena"
    return ARENA_QUEUES[arch_name]


# Matches `wildcard_constraints: arch = ...` below, so an arch config adds
# without a second hand-edit -- the same reasoning as ARENA_QUEUES above.
ARENA_ARCH_PATTERN = "|".join(CONFIG.arena.archs)

ARENA_SAMPLE = CONFIG.arena.sample
_arena_sample_cfg = CONFIG.samples[ARENA_SAMPLE]
ARENA_FG_LABS_FLAGS = _fg_labs_flags(ARENA_SAMPLE)
ARENA_MEM_FLAGS = _mem_flags(ARENA_SAMPLE)
ARENA_MINIBWA_FLAGS = " ".join(_arena_sample_cfg.minibwa_flags)
ARENA_BATCH_FLAG = _batch_flag()


# --- SA densification (fg-labs/bwa-mem3#510) ---------------------------------
#
# Arms NEWER than v0.12.0 align against a stride-2 `re-sa -u 1` copy of the
# index (`arena.dense_sa_shift`), which trades ~+12 GB resident RAM for ~-4%
# CPU. Older fg-labs releases, upstream bwa-mem2, lh3/bwa and minibwa keep the
# stock stride-8 index -- partly by choice ("prior versions don't use it") and
# partly because a pre-#510 / foreign reader may not parse a densified on-disk
# SA table at all. That split is why the dense table is a SEPARATE index copy
# (`hg38-u1`), not an in-place `re-sa` of the one shared index.
#
# `STOCK_SA_SHIFT` (3 = stride-8, imported from workflow_config so the loader's
# validation bound and this feature-off gate cannot drift) is the sentinel for
# "feature off": at that shift `_arm_uses_dense_sa` is False for every arm,
# `_arena_dense_ref_inputs` falls back to the stock index, and no separate copy
# is staged. Must come after `_arena_sample_cfg` (it reads `.reference`).
ARENA_DENSE_SA_SHIFT = CONFIG.arena.dense_sa_shift

# The dense index's reference NAME, derived from the shift so it cannot drift
# from the staged copy: shift 1 -> "hg38-u1" (config load has already verified
# that reference exists -- see `_arena_from` in workflow_config.py), shift 3 ->
# the stock reference (feature off).
_ARENA_STOCK_REFERENCE = _arena_sample_cfg.reference
ARENA_DENSE_REFERENCE = (
    _ARENA_STOCK_REFERENCE
    if ARENA_DENSE_SA_SHIFT >= STOCK_SA_SHIFT
    else f"{_ARENA_STOCK_REFERENCE}-u{ARENA_DENSE_SA_SHIFT}"
)

# The arena runs on 64 GB m8a/m8g hosts (config/archs.yaml), NOT the sweep's
# 32 GB c-series, specifically so the stride-2 arm's ~27-28 GB peak RSS fits.
# A dedicated ceiling rather than `_mem_mb_for` (28000, sized for the sweep's
# 32 GB hosts): 56 GB is ~2x the dense arm's RSS and leaves the 64 GB host ~8 GB
# for the OS, with the rest of the cgroup available as page cache for the `cat`
# prewarm of the heavy arms (upstream's ~21 GB index, the stride-2 index's
# ~27 GB) that never fit alongside their RSS on 32 GB.
ARENA_MEM_MB = 56000

# The block-boundary balloon allocates `arena.balloon_mb` inside this job's
# cgroup; at or above its limit the job is OOM-killed at the first boundary.
if CONFIG.arena.balloon_mb >= ARENA_MEM_MB:
    raise ValueError(
        f"arena.balloon_mb ({CONFIG.arena.balloon_mb}) must be below align_arena's "
        f"{ARENA_MEM_MB} MB memory limit"
    )

# ARENA_RELEASES is oldest-first, so an arm's release is "newer than v0.12.0"
# iff its label sits after v120's in the list. Keyed on list position, not a
# parse of the ambiguous `vNNN` label (v120 = v0.12.0 but v100 = v0.10.0, not
# v1.0.0), so appending a future release automatically makes it a dense arm.
ARENA_DENSE_SA_MIN_LABEL = "v120"


# Bound once to this module's release list + config knobs; unit-tested via
# `bwa_mem3_bench.arena_arms.DenseSaPolicy`. See that class for the full gate;
# in short: the candidate is always dense, a historical `bwa-mem3.<release>`
# arm is dense iff it is newer than `ARENA_DENSE_SA_MIN_LABEL`,
# bwa/minibwa/upstream never are, and nothing is dense when the feature is off
# (`ARENA_DENSE_SA_SHIFT == STOCK_SA_SHIFT`).
_DENSE_SA_POLICY = DenseSaPolicy(
    release_labels=tuple(lbl for lbl, _ in ARENA_RELEASES),
    min_label=ARENA_DENSE_SA_MIN_LABEL,
    dense_shift=ARENA_DENSE_SA_SHIFT,
    stock_shift=STOCK_SA_SHIFT,
)


def _arm_uses_dense_sa(label: str, binary: str) -> bool:
    """True if this arm aligns against the stride-2 `re-sa` index (see
    `_DENSE_SA_POLICY` / `DenseSaPolicy`)."""
    return _DENSE_SA_POLICY.uses_dense_sa(label, binary)


# The prior release's `--fast` sibling, paired with `fg-labs-fast` the way the
# prior release is paired with `fg-labs-default` (see `ARENA_PAIRS`).
ARENA_PRIOR_FAST_LABEL = f"{ARENA_PRIOR_RELEASE_LABEL}-fast"

# `--config arena_labels=a,b,...` restricts the arena to the named arm labels
# (e.g. `v151,v151-fast,fg-labs-default,fg-labs-fast` for a two-release
# check). Empty, the default, runs every arm. Only the index families the
# remaining arms read are staged, so a narrowed run downloads less.
# Validated in `_arena_arms`: a typo would otherwise silently drop arms from a
# paid on-demand job.
ARENA_ONLY_LABELS = frozenset(
    label.strip() for label in str(config.get("arena_labels", "")).split(",") if label.strip()
)


def _parse_arena_sam_control(raw) -> bool:
    """`--config arena_sam_control=true|false` (any case), else the config default.

    Anything else raises rather than silently meaning "off".
    """
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text not in ("true", "false"):
        raise ValueError(f"--config arena_sam_control must be true or false; got {raw!r}")
    return text == "true"


ARENA_SAM_CONTROL = _parse_arena_sam_control(
    config.get("arena_sam_control", CONFIG.arena.sam_writer_control)
)

# Arms that must run back-to-back in every round, so each round yields one
# paired candidate-vs-prior ratio (see `build_schedule`).
ARENA_PAIRS = (
    ("fg-labs-default", ARENA_PRIOR_RELEASE_LABEL),
    ("fg-labs-fast", ARENA_PRIOR_FAST_LABEL),
    (f"fg-labs-default{SAM_CONTROL_SUFFIX}", f"{ARENA_PRIOR_RELEASE_LABEL}{SAM_CONTROL_SUFFIX}"),
)


def _arena_arms(arch: str) -> list[tuple[str, str, str]]:
    """Return (label, binary, mode) for every arm `arch` runs, unordered.

    `mode` is 'default' or 'fast'. Every historical release gets BOTH a
    default-mode arm (label `<release>`) and a `--fast` arm (label
    `<release>-fast`) on the same binary -- see the module docstring for why
    this needs no hand-maintained "which releases support --fast" list: a
    release that predates the flag just SKIPs its `-fast` arm via the same
    "never hard-fail on an old binary" wrapper the default-mode arms already
    rely on. bwa-mem2-upstream is dropped on ARM (upstream v2.2.1 has no ARM
    build), matching `_has_upstream_baseline`.

    With `arena.sam_writer_control` on, two control arms run the candidate and
    the prior release through the SAM-text writer. `--config arena_labels=...`
    filters the result. Run order is `_arena_schedule`'s job.
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
    if ARENA_SAM_CONTROL:
        arms += [
            (f"fg-labs-default{SAM_CONTROL_SUFFIX}", "bwa-mem2.fg-labs", "default"),
            (
                f"{ARENA_PRIOR_RELEASE_LABEL}{SAM_CONTROL_SUFFIX}",
                f"bwa-mem3.{ARENA_PRIOR_RELEASE_LABEL}",
                "default",
            ),
        ]
    if ARENA_ONLY_LABELS:
        _check_arena_labels(arch, {label for label, _, _ in arms})
        arms = [arm for arm in arms if arm[0] in ARENA_ONLY_LABELS]
    return arms


def _check_arena_labels(arch: str, available: set[str]) -> None:
    """Reject a `--config arena_labels=` entry that names no arm.

    A label is accepted if ANY arena arch runs it (`bwa-mem2-upstream` exists on
    x86 only), so one list can narrow every arch; a label no arch runs is a typo.
    An arch left with no arm at all is also an error, since the job would run
    nothing and still succeed.
    """
    unknown = ARENA_ONLY_LABELS - _ALL_ARENA_LABELS
    if unknown:
        raise ValueError(
            f"--config arena_labels names no arena arm: {sorted(unknown)}; "
            f"valid labels: {sorted(_ALL_ARENA_LABELS)}"
        )
    if not ARENA_ONLY_LABELS & available:
        raise ValueError(
            f"--config arena_labels={','.join(sorted(ARENA_ONLY_LABELS))} leaves arch "
            f"{arch} with no arm to run"
        )


def _unfiltered_arena_labels() -> frozenset[str]:
    """Every label any arena arch runs, before `--config arena_labels=` narrowing."""
    labels = {"bwa", "minibwa", "bwa-mem2-upstream", "fg-labs-default", "fg-labs-fast"}
    for label, _ in ARENA_RELEASES:
        labels |= {label, f"{label}-fast"}
    if ARENA_SAM_CONTROL:
        labels |= {
            f"fg-labs-default{SAM_CONTROL_SUFFIX}",
            f"{ARENA_PRIOR_RELEASE_LABEL}{SAM_CONTROL_SUFFIX}",
        }
    return frozenset(labels)


_ALL_ARENA_LABELS = _unfiltered_arena_labels()


def _arena_schedule(arch: str, *, seed: str):
    """Every run `arch` makes, in execution order (`build_schedule`).

    :param seed: the fg-labs SHA under measurement, so a different candidate
        gets a different (but reproducible) order. When the run has a
        candidate arm, it must not already be on the ladder (under its SHA, or a
        ledger alias of the prior release), or the candidate would be paired with
        its own build. A narrowed run with no candidate arm (e.g.
        `arena_labels=v151,v160`) only compares releases, so it is allowed.
    """
    if _arena_runs_candidate(arch):
        check_candidate_not_on_ladder(seed, ARENA_RELEASES, _ARENA_LEDGER)
    arms = _arena_arms(arch)
    return build_schedule(
        arms,
        dense_labels=frozenset(
            label for label, binary, _ in arms if _arm_uses_dense_sa(label, binary)
        ),
        pairs=ARENA_PAIRS,
        candidate_label="fg-labs-default",
        seed=seed,
        headline_rounds=CONFIG.arena.reps,
        context_rounds=CONFIG.arena.context_reps,
    )


def _arena_arm_spec(arch: str, *, seed: str) -> str:
    """`_arena_schedule` as space-separated `ScheduledRun.to_token` strings,
    `block|round|position|label|binary|mode|dense|writer`, for the shell loop.

    Density and writer are decided in Python (unit-tested) so the shell only
    picks which staged idxbase and which output path to use.
    """
    return " ".join(run.to_token() for run in _arena_schedule(arch, seed=seed))


# `_ref_inputs` / `_bwa_ref_inputs` / `_minibwa_ref_inputs` each take a
# wildcards object and read ONLY `.sample` from it. `align_arena`'s own
# wildcards carry `sha` and `arch` but no `sample` -- the arena pins ONE
# fixed sample (`CONFIG.arena.sample`), it is not a per-run selection -- so
# this fixed stand-in satisfies those helpers without adding a real `sample`
# wildcard the rule has no other use for.
_ArenaSampleWildcards = namedtuple("_ArenaSampleWildcards", ["sample"])
_ARENA_WC = _ArenaSampleWildcards(sample=ARENA_SAMPLE)


def _arena_runs_candidate(arch: str) -> bool:
    """Whether `arch` runs either candidate arm (and so needs the pin probe)."""
    return any(binary == "bwa-mem2.fg-labs" for _, binary, _ in _arena_arms(arch))


def _arena_ref_inputs(wc):
    """Every reference sidecar the selected arms read, outside the dense copy.

    Delegates to each family's own helper -- `_ref_inputs` (bwa-mem2/bwa-mem3,
    from align.smk), `_bwa_ref_inputs` (align_bwa.smk), `_minibwa_ref_inputs`
    (align_minibwa.smk) -- rather than restating the file lists, so a future
    change to any one family's sidecars (e.g. a new bwa-mem3 index file)
    reaches the arena automatically. Each family is staged only when a selected
    arm reads it: a `--config arena_labels=` run may drop any of them, and the
    bwa-mem2-format sets are ~20-30 GB each. The plain `.fasta` is always index
    0 (every family's helper lists it first), which the shell body relies on
    via `{input.ref[0]}`. `wc.arch` picks the arm list; the sample is fixed --
    see `_ARENA_WC` above.
    """
    arms = _arena_arms(wc.arch)
    binaries = {binary for _, binary, _ in arms}
    reads_stock_bwa_mem2 = any(
        binary not in ("bwa", "minibwa") and not _arm_uses_dense_sa(label, binary)
        for label, binary, _ in arms
    )
    stock = _ref_inputs(_ARENA_WC, meth_index="d3")
    paths = list(stock) if reads_stock_bwa_mem2 else [stock[0]]
    if "bwa" in binaries:
        paths += _bwa_ref_inputs(_ARENA_WC)
    if "minibwa" in binaries:
        paths += _minibwa_ref_inputs(_ARENA_WC)
    seen: list[str] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


def _arena_dense_ref_inputs(wc):
    """bwa-mem3 sidecars for the stride-2 `re-sa` index the dense arms use.

    Reuses `_ref_inputs` with a reference override (`ARENA_DENSE_REFERENCE`,
    e.g. `hg38-u1`) so the sidecar list is never restated. When no selected arm
    is dense -- the feature is off (`ARENA_DENSE_SA_SHIFT == STOCK_SA_SHIFT`) or
    a `--config arena_labels=` run kept none -- only the stock plain `.fasta`
    is returned: the list must stay non-empty because the shell formats
    `{input.ref_dense[0]}`, but no arm routes to it, so nothing more is staged.

    Only the bwa-mem2/bwa-mem3 family is ever dense (bwa/minibwa are not).
    """
    if not any(_arm_uses_dense_sa(label, binary) for label, binary, _ in _arena_arms(wc.arch)):
        return _ref_inputs(_ARENA_WC, meth_index="d3")[:1]
    return _ref_inputs(_ARENA_WC, meth_index="d3", reference=ARENA_DENSE_REFERENCE)


rule align_arena:
    input:
        ref = _arena_ref_inputs,
        ref_dense = _arena_dense_ref_inputs,
        # Shared read-only FASTQs (see `_shared`): under a per-run
        # default-storage-prefix they resolve to the shared root, not the run
        # prefix -- the same routing `_query_fastqs` gives the sweep rules.
        fastqs = [
            _shared(f"{_arena_sample_cfg.source}{name}")
            for name in _arena_sample_cfg.fastq_names
        ],
    output:
        tsv            = "arena/{sha}/{arch}/arena.tsv",
        profile        = "arena/{sha}/{arch}/runtime-profiles.tar.gz",
        meta           = "arena/{sha}/{arch}/meta.json",
        host_probe     = "arena/{sha}/{arch}/host-probe.jsonl",
        meminfo        = "arena/{sha}/{arch}/meminfo.jsonl",
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
        batch_queue = lambda wc: arena_queue_for(wc.arch),
        mem_mb = ARENA_MEM_MB,
        aws_batch_container_image = lambda wc: image_for_arch(wc.arch),
        # The arena is long by construction: every block runs a warmup round
        # plus `reps` (candidate block) or `context_reps` measured rounds across
        # ~40 arms -- about 2.5 h on m8g. The profile default (7200 s) is too
        # tight; match the ladder's own bump.
        runtime = 14400,
    params:
        # Seeded by the fg-labs SHA under measurement -- see `_arena_schedule`
        # for why the order must vary per candidate rather than being fixed.
        arm_spec = lambda wc: _arena_arm_spec(wc.arch, seed=wc.sha),
        prior_label = ARENA_PRIOR_RELEASE_LABEL,
        # Whether the candidate's dedicated SMEM-lockstep pin probe reads the
        # stride-2 `re-sa` index -- the candidate is always a dense arm when the
        # feature is on, so the probe must load the same idxbase its measured
        # runs do (1) or the stock one (0). See `_arm_uses_dense_sa`.
        fg_labs_dense = int(_arm_uses_dense_sa("fg-labs-default", "bwa-mem2.fg-labs")),
        pin_probe = lambda wc: int(_arena_runs_candidate(wc.arch)),
        probe_seconds = CONFIG.arena.host_probe_seconds,
        label_probe_seconds = CONFIG.arena.label_probe_seconds,
        prewarm_passes = CONFIG.arena.prewarm_passes,
        balloon_mb = CONFIG.arena.balloon_mb,
        # Non-empty only for a `--config arena_labels=` run; recorded in the
        # job's outputs so a narrowed arena is never mistaken for a full one.
        only_labels = ",".join(sorted(ARENA_ONLY_LABELS)),
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
        if [ -n "{params.only_labels}" ]; then
            echo "{params.only_labels}" > "$OUTDIR/runs/NARROWED-arena_labels.txt"
            echo "WARNING: narrowed arena (arena_labels={params.only_labels}); its outputs are not a full arena" >&2
        fi

        emit-host-probe pre {params.probe_seconds} > {output.host_probe}
        : > {output.meminfo}

        # Columns 1-7 keep their pre-2026-11 order so a reader of the old
        # format still finds them; `rep` is now the schedule ROUND (0 = the
        # block's discarded warmup). The rest are per-run schedule position and
        # host state -- see the module docstring's "Instrumentation, per run".
        printf 'label\tmode\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\tblock\tposition\twriter\tindex_read_s\tio_in_mb\tpage_cache_start_mb\tthp_fault_fallback\tcompact_stall\tpgmajfault\tmemfree_mb\tanon_huge_mb\n' > {output.tsv}

        # Diagnostic-only page-cache snapshot -- Cached/Dirty/Buffers from
        # /proc/meminfo, one JSON line per call, taken at the start of every
        # round. `|| echo null` on each lookup: a missing/unparseable field
        # degrades that one field to `null` rather than aborting a multi-hour
        # on-demand job over a proc-fs read.
        emit_meminfo_snapshot() {{
            local phase="$1"
            local cached dirty buffers
            cached=$(mawk '/^Cached:/{{print $2; exit}}' /proc/meminfo || echo null)
            dirty=$(mawk '/^Dirty:/{{print $2; exit}}' /proc/meminfo || echo null)
            buffers=$(mawk '/^Buffers:/{{print $2; exit}}' /proc/meminfo || echo null)
            printf '{{"phase": "%s", "cached_kb": %s, "dirty_kb": %s, "buffers_kb": %s}}\n' \
                "$phase" "${{cached:-null}}" "${{dirty:-null}}" "${{buffers:-null}}" >> {output.meminfo}
        }}
        emit_meminfo_snapshot pre

        # Per-run host-state readers. All print `NA` rather than fail: they are
        # diagnostics recorded beside the timing, never a reason to lose a run.
        # /proc/vmstat counters are host-wide, which is what we want on a host
        # this job has to itself.
        vmstat_counter() {{
            mawk -v key="$1" '$1 == key {{ print $2; found = 1 }} END {{ if (!found) print "NA" }}' \
                /proc/vmstat 2>/dev/null || echo NA
        }}
        meminfo_mb() {{
            mawk -v key="$1:" '$1 == key {{ printf "%.0f\n", $2 / 1024; found = 1 }} END {{ if (!found) print "NA" }}' \
                /proc/meminfo 2>/dev/null || echo NA
        }}
        counter_delta() {{
            if [ "$1" = NA ] || [ "$2" = NA ]; then echo NA; else echo $(( $2 - $1 )); fi
        }}
        # One column of tricorder's two-line timing.tsv, by header name.
        timing_column() {{
            mawk -F '\t' -v name="$2" \
                'NR == 1 {{ for (i = 1; i <= NF; i++) if ($i == name) col = i }}
                 NR == 2 {{ print (col ? $col : "NA") }}' "$1" 2>/dev/null || echo NA
        }}

        run_arm() {{
            # Runs one (label, binary, mode), writing "${{out}}.timing.tsv" /
            # "${{out}}.stderr.log" / "${{out}}.bam.raw". Returns 1 on a
            # 0-record (or missing) BAM; the caller reads it under `set +e`,
            # deliberately suspended around this call -- see the module
            # docstring's "Never hard-fail on an old binary".
            #
            # `writer` (decided in Python, `arena_arms.output_writer`) picks the
            # output path: `bam` is the binary's own `--bam=0 -o` (every bwa-mem3
            # build), `sam` pipes SAM text through `samtools view -u` (bwa,
            # minibwa, upstream bwa-mem2, and the `-sam` control arms). Either
            # way the run ends with an uncompressed BAM on disk.
            local label="$1" binary="$2" mode="$3" rep="$4" out="$5" dense="$6" writer="$7"
            local cmd
            # Arms newer than v0.12.0 (`dense=1`, decided by `_arm_uses_dense_sa`
            # in Python) align against the stride-2 `re-sa` index staged under
            # `{input.ref_dense[0]}`; everything else uses the stock stride-8
            # `{input.ref[0]}`. Only bwa-mem2.fg-labs / historical bwa-mem3 arms
            # are ever dense, so bwa/minibwa always fall through to the stock ref.
            local ref="{input.ref[0]}"
            [ "$dense" = "1" ] && ref="{input.ref_dense[0]}"
            # `-o -` streams the BAM to stdout; see "Output" in the module
            # docstring for why no arm writes its output file directly.
            local out_flags=""
            [ "$writer" = "bam" ] && out_flags="--bam=0 -o -"
            case "$binary" in
                bwa)
                    cmd="bwa mem -t {threads} {params.batch_flag} {params.mem_flags} $ref {input.fastqs}"
                    ;;
                minibwa)
                    cmd="minibwa map -t {threads} {params.minibwa_flags} $ref {input.fastqs}"
                    ;;
                bwa-mem2.fg-labs)
                    local fast_flag=""
                    [ "$mode" = "fast" ] && fast_flag="--fast"
                    # -v 3 is needed ONLY on the pin probe (rep=probe), so it
                    # prints its resolved "phase-2 SMEM lockstep width" line
                    # (see the BWA3_SMEM_LOCKSTEP_N pin below). No scheduled run,
                    # warmup included, gets it: the verbose stderr writes happen
                    # inside the tricorder-timed region.
                    local verbosity_flag=""
                    [ "$rep" = "probe" ] && verbosity_flag="-v 3"
                    cmd="$binary mem -t {threads} $verbosity_flag {params.batch_flag} {params.mem_flags} {params.fg_labs_flags} $fast_flag $out_flags $ref {input.fastqs}"
                    ;;
                *)
                    # Historical bwa-mem3 releases and bwa-mem2-upstream: NEVER
                    # today's candidate's fg_labs_flags -- those are scoped to
                    # the CURRENT candidate's CLI surface (align.smk's
                    # _fg_labs_flags), not to an arbitrary older/foreign binary.
                    # bwa-mem2-upstream never gets a `mode = fast` arm, and its
                    # writer is always `sam` (empty $out_flags) -- see
                    # `_arena_arms` / `output_writer`. A release that predates a
                    # flag exits non-zero here, which `run_arm`'s caller turns
                    # into a SKIPPED row, not a job failure.
                    local fast_flag=""
                    [ "$mode" = "fast" ] && fast_flag="--fast"
                    cmd="$binary mem -t {threads} {params.batch_flag} {params.mem_flags} $fast_flag $out_flags $ref {input.fastqs}"
                    ;;
            esac
            # Every arm's final writer is `cat` into the file, never htslib:
            # htslib's `bgzf_close` fdatasync()s a regular-file output, and a
            # single-threaded BGZF writer (bwa-mem3's default) defers the whole
            # ~3.5 GB sync to close -- ~6 s of idle disk wait on a 500 MB/s
            # volume, timing the disk instead of the aligner. Through a pipe
            # there is no sync, which is also how a real pipeline consumes it.
            #
            # `--interval 0.1`: tricorder's wall time resolves to its sampling
            # tick (0.5 s by default), which on a ~20 s run is a 2.5% step --
            # coarser than the release-over-release deltas the arena measures.
            # --trace: per-tick RSS/IO/page-fault samples, folded into
            # runtime-profiles.tar.gz below; diagnostic only.
            local to_bam=""
            [ "$writer" = "sam" ] && to_bam="samtools view -@4 -u -o - - |"
            tricorder --interval 0.1 --out "${{out}}.timing.tsv" --trace "${{out}}.trace.tsv" -- \
                bash -c "set -o pipefail; $cmd 2>'${{out}}.stderr.log' | $to_bam cat > '${{out}}.bam.raw'"
            if [ "$(samtools view -c "${{out}}.bam.raw" 2>/dev/null || echo 0)" -eq 0 ]; then
                return 1
            fi
        }}

        # Untimed page-cache top-off of ONE arm's own index family + the
        # fastqs, run `prewarm_passes` times immediately before EVERY run --
        # see the module docstring's "Warmup: per-run cat". `|| true`: a
        # missing sidecar must not fail the whole job over a warm-cache nicety.
        prewarm_arm() {{
            local binary="$1" dense="$2"
            # Warm the SAME idxbase the matching run_arm will read: dense
            # bwa-mem2/bwa-mem3 arms use the stride-2 `re-sa` copy, so its
            # (larger) .bwt.2bit.64 is what must be resident, not the stock one.
            local ref="{input.ref[0]}"
            [ "$dense" = "1" ] && ref="{input.ref_dense[0]}"
            case "$binary" in
                bwa)
                    cat {input.ref[0]}.bwt {input.ref[0]}.sa {input.ref[0]}.pac
                    ;;
                minibwa)
                    cat {input.ref[0]}.l2b {input.ref[0]}.mbw
                    ;;
                *)
                    cat "$ref".0123 "$ref".bwt.2bit.64 "$ref".pac
                    ;;
            esac > /dev/null 2>/dev/null || true
            cat {input.fastqs} > /dev/null 2>/dev/null || true
        }}

        # Allocate, touch and free `balloon_mb` of anonymous memory at a block
        # boundary: evicts the previous index family's page cache and leaves
        # contiguous free memory for the next block's huge pages. `dd` with
        # `bs=<N>M count=1 iflag=fullblock` reads <N> MB of zeros into ONE
        # buffer, touching every page of it, then exits. Never fatal.
        balloon() {{
            local block="$1"
            [ {params.balloon_mb} -gt 0 ] || return 0
            local t0=$SECONDS
            dd if=/dev/zero of=/dev/null bs={params.balloon_mb}M count=1 iflag=fullblock 2>/dev/null \
                || echo "WARNING: balloon before block=$block failed; continuing" >&2
            echo "balloon: block=$block mb={params.balloon_mb} s=$((SECONDS - t0))" >&2
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

        # Pin today's candidate's phase-2 SMEM lockstep width (fg-labs/bwa-mem3
        # #393) from a DEDICATED probe invocation run once here, instead of
        # re-probing on every measured invocation. The probe runs once per
        # process during index setup, before the binary's own PROCESS() timer
        # starts, so it is invisible to process_s -- but NOT to wall_s, and it
        # costs a much bigger fraction of a short --fast run's wall time than a
        # long stock run's, which otherwise makes v0.10.0+'s --fast arm look
        # artificially worse against an older release with no such probe.
        # Every release before #393 predates the feature entirely and simply
        # never reads this env var, so exporting it for the whole rest of the
        # script is a no-op for every other arm -- safe to set unconditionally,
        # not just around the fg-labs invocations.
        #
        # This is deliberately a STANDALONE probe. The per-rep prewarm
        # (`prewarm_arm`, above) is a plain `cat`, not an alignment, so it
        # never prints the "phase-2 SMEM lockstep width" line this pin
        # parses -- and no measured or warmup run passes `-v 3`, so this
        # probe is the ONLY invocation that prints it. Deriving the pin from
        # whichever candidate run happened to come first would let
        # `fg-labs-fast` runs scheduled before it go unpinned, reintroducing
        # the probe-cost inflation this pin exists to avoid.
        #
        # Clear any INHERITED BWA3_SMEM_LOCKSTEP_N before parsing the probe
        # log. Without this, a value already present in the job's environment
        # (e.g. a worker host that leaked one from a prior invocation) would
        # silently survive into the measured cycles below whenever THIS run's
        # probe log is missing or lacks a probe line -- masquerading as a pin
        # from a probe that never actually ran this time.
        unset BWA3_SMEM_LOCKSTEP_N
        # The probe is a full candidate alignment, so it runs only when the
        # candidate is among the selected arms (`_arena_runs_candidate`); a
        # narrowed arena without it leaves every arm unpinned.
        if [ "{params.pin_probe}" = "1" ]; then
            # As of fg-labs/bwa-mem3 #414 the startup MLP probe is OPT-IN (default
            # off); the pin probe must explicitly request it, or a #414+ candidate
            # would echo its compiled default width here instead of measuring one.
            # No-op on #393..#413 builds (probe on by default, this var unread) and
            # on pre-#393 builds (feature absent). Unset again after the probe (see
            # below) so the measured cycles never re-probe on the unpinned fallback.
            export BWA3_SMEM_LOCKSTEP_PROBE=1
            PIN_PROBE_OUT="$OUTDIR/runs/fg-labs-default.default.pinprobe"
            set +e
            run_arm "fg-labs-default" "bwa-mem2.fg-labs" "default" probe "$PIN_PROBE_OUT" {params.fg_labs_dense} bam
            status=$?
            set -e
            [ $status -ne 0 ] && echo "pin probe: fg-labs-default failed (exit=$status); a parsed width will be used if available, otherwise --fast rows will run unpinned (the build's own default width resolution)" >&2
            rm -f "${{PIN_PROBE_OUT}}.bam.raw"
            PIN_PROBE_LOG="${{PIN_PROBE_OUT}}.stderr.log"
            if [ -f "$PIN_PROBE_LOG" ]; then
                FG_LABS_LOCKSTEP_N=$(grep -oE 'phase-2 SMEM lockstep width: [0-9]+' "$PIN_PROBE_LOG" \
                    | grep -oE '[0-9]+$' | tail -1 || true)
                if [ -n "$FG_LABS_LOCKSTEP_N" ]; then
                    export BWA3_SMEM_LOCKSTEP_N="$FG_LABS_LOCKSTEP_N"
                    echo "pinned BWA3_SMEM_LOCKSTEP_N=$FG_LABS_LOCKSTEP_N from the pin probe" >&2
                else
                    echo "WARNING: could not find a phase-2 SMEM lockstep width line in $PIN_PROBE_LOG -- today's candidate's measured --fast rows will run unpinned (the build's own default width resolution)" >&2
                fi
            else
                echo "WARNING: $PIN_PROBE_LOG missing -- pin probe likely failed; today's candidate's measured --fast rows will run unpinned (the build's own default width resolution)" >&2
            fi
        else
            echo "pin probe skipped: no candidate arm selected; every arm runs unpinned" >&2
        fi
        # Probe requested only for the dedicated pin invocation above; clear it so
        # the measured cycles never re-probe -- on the unpinned fallback path a
        # lingering opt-in would make a #414+ candidate pay the probe cost on
        # every measured rep, the exact inflation this pin exists to avoid.
        unset BWA3_SMEM_LOCKSTEP_PROBE

        # The schedule: index-family blocks of paired rounds, built in Python
        # (`_arena_schedule` / `arena_arms.build_schedule`). Each token is
        # `block|round|position|label|binary|mode|dense|writer`; round 0 is the
        # block's discarded warmup. The balloon runs at every block change, and
        # a tachyon probe + meminfo snapshot at every round change.
        PREV_BLOCK=""
        PREV_ROUND_KEY=""
        for entry in $ARM_SPEC; do
            IFS='|' read -r block round position label binary mode dense writer <<< "$entry"

            if [ "$block" != "$PREV_BLOCK" ]; then
                balloon "$block"
                PREV_BLOCK="$block"
            fi
            if [ "$block-$round" != "$PREV_ROUND_KEY" ]; then
                emit-host-probe "round-${{block}}-${{round}}" {params.label_probe_seconds} >> {output.host_probe}
                emit_meminfo_snapshot "round-${{block}}-${{round}}"
                PREV_ROUND_KEY="$block-$round"
            fi

            CAT_T0=$SECONDS
            for pass in $(seq 1 {params.prewarm_passes}); do
                prewarm_arm "$binary" "$dense"
            done
            echo "prewarm: label=$label mode=$mode round=$round dense=$dense passes={params.prewarm_passes} cat_s=$((SECONDS - CAT_T0))" >&2

            OUT="$OUTDIR/runs/${{label}}.${{mode}}.round${{round}}"
            # Host state at the start of the run, after the prewarm, so the
            # deltas below cover the timed run alone.
            FALLBACK0=$(vmstat_counter thp_fault_fallback)
            STALL0=$(vmstat_counter compact_stall)
            MAJFLT0=$(vmstat_counter pgmajfault)
            MEMFREE=$(meminfo_mb MemFree)
            ANONHUGE=$(meminfo_mb AnonHugePages)
            set +e
            run_arm "$label" "$binary" "$mode" "$round" "$OUT" "$dense" "$writer"
            status=$?
            set -e
            FALLBACK=$(counter_delta "$FALLBACK0" "$(vmstat_counter thp_fault_fallback)")
            STALL=$(counter_delta "$STALL0" "$(vmstat_counter compact_stall)")
            MAJFLT=$(counter_delta "$MAJFLT0" "$(vmstat_counter pgmajfault)")
            if [ $status -ne 0 ]; then
                echo "WARNING: arm=$label mode=$mode round=$round FAILED (exit=$status) -- likely an unsupported flag/subcommand on an old binary; recording SKIPPED and continuing" >&2
                printf '%s\t%s\t%s\tNA\tNA\tNA\tNA\t%s\t%s\t%s\tNA\tNA\tNA\t%s\t%s\t%s\t%s\t%s\n' \
                    "$label" "$mode" "$round" "$block" "$position" "$writer" \
                    "$FALLBACK" "$STALL" "$MAJFLT" "$MEMFREE" "$ANONHUGE" >> {output.tsv}
                rm -f "${{OUT}}.bam.raw"
                continue
            fi
            WALL=$(mawk 'NR==2{{print $1}}' "${{OUT}}.timing.tsv")
            RSS=$(mawk 'NR==2{{print $3}}' "${{OUT}}.timing.tsv")
            CPU=$(mawk 'NR==2{{print $10}}' "${{OUT}}.timing.tsv")
            IO_IN=$(timing_column "${{OUT}}.timing.tsv" io_in)
            PCACHE=$(timing_column "${{OUT}}.timing.tsv" page_cache_start)
            PROC=$(grep -oE 'PROCESS\(\).*?:[[:space:]]*[0-9.]+' "${{OUT}}.stderr.log" \
                   | grep -oE '[0-9.]+$' | head -1 || true)
            # The binary's own index-load time (bwa-mem2 and every bwa-mem3
            # build print it); a cold page cache shows up here first.
            IDX=$(grep -oE 'Index read time avg: [0-9.]+' "${{OUT}}.stderr.log" \
                  | grep -oE '[0-9.]+$' | head -1 || true)
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$label" "$mode" "$round" "$WALL" "$CPU" "$RSS" "${{PROC:-NA}}" \
                "$block" "$position" "$writer" "${{IDX:-NA}}" "${{IO_IN:-NA}}" "${{PCACHE:-NA}}" \
                "$FALLBACK" "$STALL" "$MAJFLT" "$MEMFREE" "$ANONHUGE" >> {output.tsv}
            # Tee the same row to stderr (-> CloudWatch) so a result is
            # visible the moment a run finishes, not just when the whole job
            # completes and arena.tsv finally reaches S3 -- Snakemake's
            # storage plugin uploads declared outputs on rule completion,
            # not incrementally.
            echo "RESULT: label=$label mode=$mode round=$round wall_s=$WALL cpu_s=$CPU max_rss_mb=$RSS process_s=${{PROC:-NA}} index_read_s=${{IDX:-NA}} thp_fault_fallback=$FALLBACK" >&2
            # Keep the latest MEASURED round's BAM for the two arms the
            # correctness check compares -- default mode only (--fast prunes
            # the candidate set on purpose, so its BAM is not a meaningful
            # fgumi comparator -- see CLAUDE.md's vs_default note). `mv`
            # overwrites the previous round's copy, so the last round wins.
            if [ "$round" -ge 1 ] && [ "$mode" = "default" ] \
               && {{ [ "$label" = "fg-labs-default" ] || [ "$label" = "{params.prior_label}" ]; }}; then
                mv -f "${{OUT}}.bam.raw" "$OUTDIR/keep.${{label}}.bam"
            fi
            rm -f "${{OUT}}.bam.raw"
        done

        emit-host-probe post {params.probe_seconds} >> {output.host_probe}
        emit_meminfo_snapshot post

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
            echo "fgumi correctness check skipped: fg-labs-default or {params.prior_label} BAM missing (see arena.tsv for which round failed)" \
                > {output.fgumi_compare}
        fi
        rm -f "$KEEP_FG" "$KEEP_PRIOR"

        tar -czf {output.profile} -C "$OUTDIR" runs
        """
