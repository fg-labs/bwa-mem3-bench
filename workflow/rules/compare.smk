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
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.x86} --out {output.json} {params.ignore_tag_args}
        """


rule compare_vs_golden:
    input:
        query  = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        golden = lambda wc: (
            f"golden/fg-labs-{wc.sha}/{wc.sample}/{wc.arch}/aligned.bam"
        ),
        meta   = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    output:
        json = "runs/{sha}/{sample}/{arch}/rep-{rep}/compare/vs-golden.json",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
    params:
        ignore_tag_args = lambda wc: _ignore_tag_args(wc.sample),
    shell:
        r"""
        mkdir -p $(dirname {output.json})
        compare-bams --query {input.query} --baseline {input.golden} --out {output.json} {params.ignore_tag_args}
        """
