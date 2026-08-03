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
