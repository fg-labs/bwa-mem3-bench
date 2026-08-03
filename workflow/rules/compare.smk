"""Run compare-bams vs baseline and vs golden."""

# Reference x86 arch used by `compare_vs_x86` to give ARM archs a transitive
# concordance signal. Upstream bwa-mem2 v2.2.1 has no ARM build, so ARM archs
# can't be directly compared to upstream. Instead we compare ARM fg-labs
# against x86 fg-labs at this arch — and since the x86 arch has its own
# vs-baseline.json against upstream, the chain
#   ARM fg-labs == x86 fg-labs (vs-x86.json)
#   x86 fg-labs == upstream    (vs-baseline.json)
# gives us "ARM == upstream" by transitivity. c6a is chosen because it's the
# cheapest x86 SIMD path (AVX2) and is part of every standard arch set.
ARM_X86_REFERENCE_ARCH = "c6a"

COMPAT_SUFFIX = "-compat"

# The two thread counts `compat_thread_invariance` compares. The low rung is the
# bench's standard `threads:` so one side of the comparison is the same invocation
# every other cell uses. The high rung is 32 because it must clear 26 -- the point
# at which the DEFAULT batch (`chunk_size * n_threads` = 10M * 26 = 260M) crosses
# the 256M bound bwa-mem3 used to cap at (fg-labs/bwa-mem3#298), so it exercises
# the thread range where unpinned batching provably diverged. 32 also fits the
# 64-vCPU c8g64 host that runs this.
COMPAT_INVARIANCE_THREADS = (CONFIG.threads, 32)


def _baseline_sample(sample_name: str) -> str:
    """The sample whose baseline BAM a comparison should be scored against.

    A `--compat` sibling aligns the SAME FASTQs with the same `mem_flags` as its
    base, so upstream bwa-mem2 produces a byte-identical BAM for both. Aliasing
    the sibling onto its base reuses that BAM instead of paying for a second
    identical bwa-mem2 run per arch — on the five real datasets that is 25
    redundant 5M-read alignments.

    The aliasing is only sound while the sibling and its base agree on every
    input to the baseline; `_validate_compat_siblings` enforces that at config
    load so this cannot quietly start comparing against the wrong BAM.
    """
    if sample_name.endswith(COMPAT_SUFFIX):
        return sample_name[: -len(COMPAT_SUFFIX)]
    return sample_name

# Batch cgroup memory for the compare rules.
#
# `compare-bams` walks both BAMs in lockstep and holds only ONE template's
# records at a time (see tools/compare-bams/src/template_reader.rs), so its
# footprint is a few hundred MB and is independent of sample size — a 5M-pair
# wgs BAM costs no more than the 63K-read smoke.
#
# Without an explicit value these rules inherit the profile's
# `default-resources: mem_mb: 28000`, which is sized for the ALIGNERS (hg38 FMI
# + per-batch working set) and is ~70x what a compare needs. Because Batch packs
# by the cgroup request, a 28 GB reservation lets only ONE compare job land on a
# 32 GB *.4xlarge while 15 of its 16 vCPUs sit idle — so the compare tail
# serializes one-job-per-instance exactly when spot capacity is tight. Observed
# on c7i during the 394f8f8 sweep: 31 queued compares draining singly behind a
# single instance while every other queue had already emptied.
#
# 4 GB keeps a wide safety margin over actual usage and fits ~7 concurrent
# compares per *.4xlarge.
COMPARE_MEM_MB = 4000


def _tag_policy_args(sample_name: str, kind: str) -> str:
    """Full `compare-bams` tag-policy flags for one (sample, comparison kind).

    Three families, resolved together so a rule cannot pass one and forget
    another:

    * `--ignore-tag` — excluded from the score. The tag policy is a property of
      the COMPARISON, not of the sample:

      - `vs_baseline` compares bwa-mem3 against a different aligner and needs
        the largest exclusion list.
      - `vs_golden` / `vs_x86` are same-binary AND same-behaviour, so every tag
        is comparable and any difference is a real finding — passing a
        `vs_baseline` exclusion there would blind the comparisons best placed to
        catch a tag-only regression (fg-labs/bwa-mem3#290).
      - `vs_default` is the exception: same binary, but `--fast` prunes the
        candidate set by design, so the tags describing that set are excluded
        while the tags describing the chosen alignment stay strict.
    * `--expect-tag` — the may-appear allowlist. Anything observed outside it
      and the ignore list fails the run by name.
    * `--absent-ok-tag` — ignore entries known absent here, exempt from the
      dead-entry check.
    """
    flags = [f"--ignore-tag {tag}" for tag in CONFIG.ignore_tags(sample_name, kind)]
    flags += [f"--expect-tag {tag}" for tag in CONFIG.expect_tags(sample_name, kind)]
    flags += [f"--absent-ok-tag {tag}" for tag in CONFIG.absent_ok_tags(sample_name, kind)]
    return " ".join(flags)


def _default_arm_sample(fast_sample_name: str) -> str:
    """The default (no-`--fast`) sibling of a `<base>-fast` sample.

    `--fast` siblings share their base sample's `source`, so `align_fg_labs`
    over the same FASTQ emits primaries in identical input order on both arms —
    which is all `compare-bams` needs to walk the two streams in lockstep.
    """
    return fast_sample_name.removesuffix("-fast")


rule compare_vs_baseline:
    input:
        query    = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        # bwa-mem2 is deterministic — all baseline reps produce byte-identical
        # BAMs — so concordance is always measured against baseline rep-1.
        # Reps 2..N exist for timing only.
        #
        # ARM queries fall back to the x86 reference arch because upstream
        # v2.2.1 has no ARM build, so `baseline/.../c8g/` can never exist. Only
        # the `--compat` arms request vs-baseline on ARM (`rule all` sends ARM
        # to vs-x86 instead), and for them the x86 baseline is the correct
        # target anyway: `--compat=bwa-mem2` claims byte-identity to bwa-mem2's
        # output, which is one stream regardless of which host produced it.
        # That equivalence is not assumed — it is entailed by two facts this
        # bench already measures every run: fg-labs default is 100% concordant
        # with the baseline on every x86 arch, and fg-labs ARM is 100%
        # concordant with fg-labs x86 (vs-x86).
        baseline = lambda wc: (
            f"baseline/bwa-mem2-{CONFIG.upstream_tag}/{_baseline_sample(wc.sample)}/"
            f"{wc.arch if wc.arch in BASELINE_ARCHS else ARM_X86_REFERENCE_ARCH}/"
            f"rep-1/aligned.bam"
        ),
        meta     = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-baseline.json",
    # Explicit rather than inherited: our executor fork derives a Batch job's
    # VCPU from `threads`, and an undeclared value has silently resolved to
    # something other than intended before (see the thread-clamping gotcha in
    # CLAUDE.md). compare-bams walks the two streams single-threaded.
    threads: 1
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = COMPARE_MEM_MB,
    params:
        tag_policy_args = lambda wc: _tag_policy_args(wc.sample, "vs_baseline"),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.baseline} --out {output.json} {params.tag_policy_args}
        """


rule compare_compat_identity:
    """`--compat=bwa-mem2` arms only: full-content identity vs upstream bwa-mem2,
    via `fgumi compare bams`.

    This is the assertion the compat arm exists to make, and `compare-bams`
    cannot make it. `compare-bams` compares placement (ref / pos / CIGAR / MAPQ),
    four FLAG bits, and aux tags — it never reads QNAME, SEQ, QUAL, TLEN,
    RNEXT/PNEXT, the other twelve FLAG bits, or the header. So a compat arm
    scored only by compare-bams can report 100% while SEQ, TLEN, the proper-pair
    flag or the @SQ dictionary all differ.

    `fgumi compare bams` compares all eleven core SAM fields plus tags
    (order-independent, so BAM tag ordering is not a false difference) and
    enforces @HD(SO/GO)/@SQ/@RG compatibility as a hard precondition, with
    @PG/@CO excluded — exactly right here, since @PG differs by construction
    (`ID:bwa-mem3` vs `ID:bwa-mem2`, plus per-run command lines).

    Boolean by design: exit 0 = identical, exit 1 = differ, which fails the rule
    and so fails the run. There is no percentage to ingest, and none is wanted —
    "almost byte-identical" is not a meaningful state for this claim. The graded
    `vs-baseline.json` is still produced alongside for the DB and trend
    reporting; this rule is the gate.

    Both arms are validated: measured locally on wgs-5M at 4.1-6.6M records/s
    with ~77 MB RSS, so the check costs seconds against alignments costing
    minutes. `--threads` is tied to the rule's `threads` so the Batch vCPU
    reservation matches what fgumi actually uses.

    NOT usable for the other comparison kinds: default-mode bwa-mem3 vs bwa-mem2
    hard-errors in fgumi's header precondition (`@SQ reference dictionaries
    differ` — default mode emits M5/AS/UR/SP from the .hdr sidecar, upstream does
    not), and vs_golden / vs_x86 / vs_default all need a concordance percentage
    that fgumi does not report.
    """
    input:
        query = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        # Same baseline resolution as compare_vs_baseline: aliased to the base
        # sample, with ARM falling back to the x86 reference arch.
        baseline = lambda wc: (
            f"baseline/bwa-mem2-{CONFIG.upstream_tag}/{_baseline_sample(wc.sample)}/"
            f"{wc.arch if wc.arch in BASELINE_ARCHS else ARM_X86_REFERENCE_ARCH}/"
            f"rep-1/aligned.bam"
        ),
    output:
        report = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/compat-identity.txt",
    # Explicit for the same executor-fork reason as compare_vs_baseline. fgumi's
    # content engine parallelises BGZF decode + comparison, so unlike
    # compare-bams this genuinely uses the threads it is given.
    threads: 4
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = COMPARE_MEM_MB,
    shell:
        r"""
        mkdir -p $(dirname {output.report})
        # Write to a temp file and move on success: a DIFFER exit must not leave
        # a satisfied output behind, or the next run would treat the failure as
        # already-done and skip it.
        fgumi compare bams {input.query} {input.baseline} \
            --threads {threads} --max-diffs 20 > {output.report}.tmp 2>&1
        mv {output.report}.tmp {output.report}
        """


rule compat_thread_invariance:
    """Prove `--compat` output does not depend on `-t`, given a pinned `-K`.

    The property under test. bwa, bwa-mem2 and bwa-mem3 all default the batch to
    `chunk_size * n_threads`, and `mem_pestat()` derives the paired-end
    insert-size percentiles from whatever reads land in a batch, feeding pairing,
    mate rescue and MAPQ. So DEFAULT output is a function of thread count --
    measured, upstream bwa-mem2 disagrees with itself on 290 of 10,030,558
    records between -t 16 and -t 32. `-K` (bwa's own answer: "process INT input
    bases in each batch regardless of nThreads (for reproducibility)") removes
    that dependence, and `batch_bases` pins it for both aligners. This rule is
    what stops that from being a claim in a comment.

    Runs BOTH thread counts inside ONE Batch job on ONE host, for the same reason
    the scaling ladder does: it needs a machine with more vCPUs than the standard
    *.4xlarge queues have, and doing it in one job avoids requiring two.

    Deliberately a SELF-comparison, which is what makes it cheap and infra-free:
    no upstream baseline is involved, so it needs no x86 host and no new Batch
    queue -- it runs on the existing 64-vCPU `c8g64` arm queue that the ladder
    already uses. (A cross-aligner check at high `-t` would need a >=32 vCPU x86
    queue, which does not exist; that parity is covered per-arch at the standard
    thread count by `compare_compat_identity`.)

    Fails the run on any difference, via fgumi's exit status.
    """
    input:
        ref = lambda wc: _ref_inputs(wc, meth_index="none"),
        fastqs = lambda wc: _query_fastqs(wc),
    output:
        report = "runs/{sha}/compat-invariance/{sample}/{arch}/report.txt",
    threads: CONFIG.thread_scaling.max_threads
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
        shared_memory_size_mb = lambda wc: _shm_size_mb_for(wc.sample),
        runtime = 7200,
    params:
        batch_flag = _batch_flag(),
        mem_flags = lambda wc: _mem_flags(wc.sample),
        extra = lambda wc: _fg_labs_flags(wc.sample),
        lo = COMPAT_INVARIANCE_THREADS[0],
        hi = COMPAT_INVARIANCE_THREADS[1],
    shell:
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.report})
        REF={input.ref[0]}
        # Stage the index once and reuse it for both thread counts — the index is
        # identical, and loading it twice would double the job's wall for nothing.
        bwa-mem2.fg-labs shm "$REF"
        trap 'bwa-mem2.fg-labs shm -d || true' EXIT
        for T in {params.lo} {params.hi}; do
            bwa-mem2.fg-labs mem -t "$T" {params.batch_flag} {params.mem_flags} {params.extra} \
                --compat=bwa-mem2 --bam=0 -o "$(dirname {output.report})/t$T.bam" \
                "$REF" {input.fastqs} 2> "$(dirname {output.report})/t$T.stderr"
            if [ "$(samtools view -c "$(dirname {output.report})/t$T.bam")" -eq 0 ]; then
                echo "ERROR: t=$T produced 0 records (crash/OOM?)" >&2; exit 1
            fi
        done
        # Written to .tmp and moved on success: fgumi exits 1 on a difference, and a
        # complete output file left behind would let the next run skip the failure.
        fgumi compare bams \
            "$(dirname {output.report})/t{params.lo}.bam" \
            "$(dirname {output.report})/t{params.hi}.bam" \
            --threads 8 --max-diffs 20 > {output.report}.tmp 2>&1
        mv {output.report}.tmp {output.report}
        rm -f "$(dirname {output.report})"/t*.bam
        """


rule compare_vs_x86:
    """ARM-only: compare fg-labs ARM BAM against fg-labs x86 BAM (transitive
    concordance signal — see ARM_X86_REFERENCE_ARCH note above)."""
    input:
        query = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        x86   = lambda wc: (
            f"runs/{wc.sha}/{wc.sample}/{ARM_X86_REFERENCE_ARCH}/rep-{wc.rep}/aligned.bam"
        ),
        meta  = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-x86.json",
    # Explicit rather than inherited: our executor fork derives a Batch job's
    # VCPU from `threads`, and an undeclared value has silently resolved to
    # something other than intended before (see the thread-clamping gotcha in
    # CLAUDE.md). compare-bams walks the two streams single-threaded.
    threads: 1
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = COMPARE_MEM_MB,
    params:
        tag_policy_args = lambda wc: _tag_policy_args(wc.sample, "vs_x86"),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.x86} --out {output.json} {params.tag_policy_args}
        """


rule compare_vs_default:
    """`--fast`-preset concordance (fg-labs/bwa-mem3 PR #189): compare a
    `<base>-fast` arm against its default sibling, both fg-labs at the same
    SHA on the same arch + rep. This is the direct check of the PR's claim that
    `--fast` diverges only in low-confidence regions — the `by_class` /
    MAPQ-stratified breakdown in the emitted JSON is where the "85% of divergent
    reads are MAPQ<=29" number reproduces on our own data.

    Both arms align the SAME FASTQ, so primaries stream in identical input
    order; `--smem-dedup`'s supplementary differences surface as compare-bams'
    supplementary-disagreement metrics rather than desyncing the walk (same
    mechanism exercised by hic-1M's heavy supplementaries). Only requested by
    the opt-in `fast` target, and only for `*-fast` samples — for a non-fast
    sample `_default_arm_sample` is a no-op and the compare would be a vacuous
    self-comparison, so do not request it there."""
    input:
        query   = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        default = lambda wc: (
            f"runs/{wc.sha}/{_default_arm_sample(wc.sample)}/"
            f"{wc.arch}/rep-{wc.rep}/aligned.bam"
        ),
        meta    = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-default.json",
    # Explicit rather than inherited: our executor fork derives a Batch job's
    # VCPU from `threads`, and an undeclared value has silently resolved to
    # something other than intended before (see the thread-clamping gotcha in
    # CLAUDE.md). compare-bams walks the two streams single-threaded.
    threads: 1
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = COMPARE_MEM_MB,
    params:
        tag_policy_args = lambda wc: _tag_policy_args(wc.sample, "vs_default"),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.default} --out {output.json} {params.tag_policy_args}
        """


rule compare_vs_golden:
    # GOLDEN_REF_SHA is the pinned previous-release SHA (Gate #2), from the
    # `golden_ref_sha` config. The golden is a FIXED reference, not the run's own
    # SHA — comparing a run to itself would be a vacuous 100%. `rule all` only
    # requests these outputs when GOLDEN_REF_SHA is set and != the run's SHA.
    input:
        query  = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        golden = lambda wc: (
            f"golden/fg-labs-{GOLDEN_REF_SHA}/{wc.sample}/{wc.arch}/aligned.bam"
        ),
        meta   = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-golden.json",
    # Explicit rather than inherited: our executor fork derives a Batch job's
    # VCPU from `threads`, and an undeclared value has silently resolved to
    # something other than intended before (see the thread-clamping gotcha in
    # CLAUDE.md). compare-bams walks the two streams single-threaded.
    threads: 1
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = COMPARE_MEM_MB,
    params:
        tag_policy_args = lambda wc: _tag_policy_args(wc.sample, "vs_golden"),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.golden} --out {output.json} {params.tag_policy_args}
        """
