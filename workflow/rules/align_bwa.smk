"""Rule to run lh3/bwa and emit an unsorted BAM.

The third upstream arm. `align_baseline` covers bwa-mem2 v2.2.1, which is
frozen at bwa 0.7.17; this covers bwa itself, so `--compat=bwa-mem` has
something to be byte-identical *to*.

Why a separate arm and not a `baseline_tool` value. `baseline_tool` is
exclusive -- a sample picks bwameth OR bwa-mem2 -- but a `--compat=bwa-mem`
sibling wants its own upstream in ADDITION to whatever its base compares
against. So bwa follows the minibwa pattern: its own rule, its own cache
prefix, keyed on its own version.

Caching model -- keyed on `bwa_version`, not the fg-labs SHA. bwa is an
independent tool whose output depends only on its own source pin, so its BAMs
live under `bwa/<version>/` exactly as the upstream baseline lives under
`baseline/bwa-mem2-<tag>/`. A re-run for a new fg-labs SHA reuses the cached
bwa BAMs via the S3 storage plugin; only bumping `bwa_version` invalidates them.

Runs natively on arm64, which the bwa-mem2 baseline cannot. Upstream bwa-mem2
v2.2.1 is x86-only, so ARM concordance today is transitive (`compare_vs_x86`:
ARM fg-labs == x86 fg-labs == upstream). bwa gained a NEON path in 0.7.18
(lh3/bwa#359) and the runtime image builds it unconditionally, so this arm
compares ARM fg-labs against upstream DIRECTLY -- which matters because the
one known cross-architecture defect in this codebase (the phantom `XS` from
unzeroed query padding in the 8-bit kswv mate-rescue kernels) lived on exactly
the NEON/AVX-512BW side that transitivity cannot see into.

Index format. bwa reads `.amb`/`.ann`/`.bwt`/`.pac`/`.sa`; bwa-mem2 and
bwa-mem3 read `.amb`/`.ann`/`.0123`/`.bwt.2bit.64`/`.pac`. Three of those are
shared, so the reference tree carries both families side by side and only
`.bwt`/`.sa` are bwa-specific. Neither tool can read the other's, so this is a
genuinely separate index artifact, not a symlink.

Warm-symmetric timing -- the timed region must exclude index load, or the
number reflects cold mmap fault-in rather than mapping work. `align_fg_labs`
excludes it via `bwa-mem2 shm`; bwa has no shm command (bwa-mem3's `shm` is a
port of bwa's v1 one, but upstream 0.7.19 does not ship it), so we mirror
`align_baseline`'s `cat`-into-page-cache prewarm over the bwa index family.

`-K` is pinned here exactly as it is for the other two aligners. bwa computes
its default batch as `chunk_size * n_threads` (fastmap.c) like both others, and
`mem_pestat()` reads whatever lands in a batch -- so an unpinned bwa run would
be a function of `-t` and could not be compared against a pinned bwa-mem3 run.
"""


def _bwa_ref_inputs(wc) -> list[str]:
    """S3-relative paths to the bwa-format reference sidecars.

    Returned relative to the snakemake default-storage-prefix so the S3 storage
    plugin stages them before the shell body runs. The first entry is always the
    plain .fasta; the shell references it via ``{input.ref[0]}``, and bwa
    auto-appends its own suffixes to that prefix.

    No meth branch: `--compat` and `--meth` are mutually exclusive in bwa-mem3
    (and bwa has no bisulfite mode at all), so a meth sample can never reach
    this rule. `Sample.__post_init__` rejects the combination at config load
    (mirroring the guard `bwa-mem3 mem` enforces at runtime), and
    `COMPAT_BWA_SAMPLES` in the Snakefile only ever selects compat siblings.
    """
    sample = CONFIG.samples[wc.sample]
    fasta_name = CONFIG.references[sample.reference]["fasta_name"]
    base = f"references/{sample.reference}/{fasta_name}"
    files = [
        base,  # plain .fasta — must be index 0 (the path passed to bwa)
        f"{base}.amb",
        f"{base}.ann",
        f"{base}.bwt",
        f"{base}.pac",
        f"{base}.sa",
    ]
    # Same `.alt` contract as the bwa-mem2/bwa-mem3 arm (see `_ref_inputs`): the
    # sidecar is shared across index families -- it names contigs, not offsets --
    # so both arms must stage it or neither, or the comparison would put an
    # ALT-aware aligner against an alt-naive one and blame bwa-mem3.
    if sample.alt_aware:
        files.append(f"{base}.alt")
    return files


rule align_bwa:
    """Align with lh3/bwa v{bwa_version} and emit an unsorted BAM.

    Shape mirrors `align_baseline`: untimed page-cache prewarm, tricord-timed
    subprocess emitting UNCOMPRESSED BAM via `samtools view -u`, then an untimed
    `samtools view -b` compress to the final BAM. Peak transient disk is the
    uncompressed `.raw` plus the final BAM held at once -- see `align_baseline`
    for the sizing note.
    """
    input:
        ref = _bwa_ref_inputs,
        fastqs = _query_fastqs,
    output:
        bam        = "bwa/{tool_version}/{sample}/{arch}/rep-{rep}/aligned.bam",
        timing     = "bwa/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/timing.tsv",
        bwa_stderr = "bwa/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/bwa.stderr.log",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
        container_image = lambda wc: image_for_arch(wc.arch),
    # `threads:` (not a param) so the executor plugin reserves the vCPUs the
    # aligner actually uses, and so this arm is timed under the same contention
    # as the other two. See align_fg_labs.
    threads: CONFIG.threads
    params:
        # The SAME `mem_flags` the other two arms get (e.g. Hi-C's `-5 -S -P`).
        # Asymmetric flags would make the comparison measure the flags.
        mem_flags = lambda wc: _mem_flags(wc.sample),
        batch_flag = _batch_flag(),
    shell:
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.bam}) $(dirname {output.timing})
        # Untimed page-cache prewarm over the files bwa mmaps. `|| true` keeps
        # the rule alive if one is missing (worst case: cold cache, still
        # correct) -- same contract as align_baseline's prewarm.
        cat {input.ref[0]}.bwt \
            {input.ref[0]}.sa \
            {input.ref[0]}.pac \
            > /dev/null 2>/dev/null || true
        # `set -o pipefail`: a SIGKILL'd aligner must fail the pipeline, not let
        # samtools exit 0 on a partial header stream (see align_fg_labs).
        tricorder --out {output.timing} -- \
            bash -c 'set -o pipefail; bwa mem -t {threads} {params.batch_flag} {params.mem_flags} \
                {input.ref[0]} {input.fastqs} 2>"{output.bwa_stderr}" \
              | samtools view -@4 -u -o {output.bam}.raw -'
        # Defense in depth: reject a header-only BAM even if the aligner exited 0.
        if [ "$(samtools view -c {output.bam}.raw)" -eq 0 ]; then
            echo "ERROR: {output.bam}.raw has 0 alignment records (aligner crashed/OOM?)" >&2
            exit 1
        fi
        # UNTIMED: compress to the final BAM for the compare + S3 upload.
        samtools view -@4 -b -o {output.bam} {output.bam}.raw
        rm -f {output.bam}.raw
        """
