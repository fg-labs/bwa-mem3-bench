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


def _ignore_tag_args(sample_name: str) -> str:
    tags = CONFIG.samples[sample_name].compare_options.get("ignore_tags", [])
    return " ".join(f"--ignore-tag {tag}" for tag in tags)


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
        baseline = lambda wc: (
            f"baseline/bwa-mem2-{CONFIG.upstream_tag}/{wc.sample}/"
            f"{wc.arch}/rep-1/aligned.bam"
        ),
        meta     = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-baseline.json",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.baseline} --out {output.json} {params.ignore_tag_args}
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
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.x86} --out {output.json} {params.ignore_tag_args}
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
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.default} --out {output.json} {params.ignore_tag_args}
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
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.golden} --out {output.json} {params.ignore_tag_args}
        """
