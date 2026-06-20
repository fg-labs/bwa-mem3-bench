"""Rules to run bwa-mem2 (fg-labs or upstream) and emit unsorted BAMs.

Inputs (reference sidecars + fastqs) are declared via `input:` directives so
snakemake's s3 storage plugin stages them to `.snakemake/storage/s3/...` before
the shell body runs. The shell body then `bwa-mem2 shm`-loads the index into
a POSIX shared-memory segment (lives in /dev/shm; backed by tmpfs); the timed
`bwa-mem2 mem` step auto-attaches and skips disk reads entirely. shm pages
are pinned (not page-cache reclaimable) so the cgroup memory.max budget can't
evict them mid-run, which is what the cat-into-page-cache prewarm tripped on.
After alignment the segment is dropped via `bwa-mem2 shm -d`. Worker job
definitions need a /dev/shm large enough to hold the index — see the
`shared_memory_size_mb` resource (plumbed through our
snakemake-executor-plugin-aws-batch fork into linuxParameters.sharedMemorySize).

The alignment subprocess is wrapped by `tricorder` (fg-labs/tricord), which
polls the process tree on an interval and writes a snakemake-format
benchmark TSV — replacing snakemake's rule-level `benchmark:` directive so
the shm load/drop steps stay outside the timed region. Tricord forwards
SIGTERM/SIGINT/SIGHUP to the child's process group so spot interruptions
tear the alignment down cleanly instead of leaking children.

Alignment output is streamed through `samtools view -b` (no sort), so the
BAM preserves bwa-mem2's FASTQ-input order. `compare-bams` consumes this in
lockstep order — see tools/compare-bams/src/template_reader.rs — which avoids
the ~15-25% overhead of a post-alignment sort.
"""

import os

# Baseline binary name; overridable for local smoke runs that want to point at
# a different upstream build. `align_baseline` only runs on baseline-supported
# archs (x86) — see BASELINE_ARCHS in workflow/Snakefile.
BASELINE_BINARY = os.environ.get("BASELINE_BINARY", "bwa-mem2.upstream")


def _fg_labs_flags(sample_name: str) -> str:
    return " ".join(CONFIG.samples[sample_name].fg_labs_flags)


def _mem_flags(sample_name: str) -> str:
    """`mem` flags applied to BOTH fg-labs and upstream baseline (not bwameth).

    Comparison-neutral knobs (e.g. `-K` for Hi-C, to cap peak RSS) that must be
    symmetric across the two aligners so concordance stays apples-to-apples.
    """
    return " ".join(CONFIG.samples[sample_name].mem_flags)


def _baseline_bwa_bin(sample_name: str) -> str:
    """Which baseline aligner to invoke. `bwa-mem2.upstream` by default; `bwameth.py` for meth."""
    if CONFIG.samples[sample_name].baseline_tool == "bwameth":
        return "bwameth.py"
    return BASELINE_BINARY


def _is_meth(sample_name: str) -> bool:
    sample = CONFIG.samples[sample_name]
    return sample.baseline_tool == "bwameth" or "--meth" in sample.fg_labs_flags


def _mem_mb_for(sample_name: str) -> int:
    """Batch container memory. Bisulfite runs load the ~20 GB doubled .bwameth.c2t
    FMI + ~6 GB packed reference into bwa-mem2; observed peak RSS is ~46 GB.
    Meth samples run on m7i.4xlarge (64 GB host, ~62 GB container budget); 52 GB
    leaves ~10 GB headroom for the ECS agent and page cache. Non-meth 28 GB is
    plenty for standard hg38 (~10 GB FMI + 6 GB packed).

    Note: when `bwa-mem2 shm` is used, the staged segment lives in /dev/shm
    (tmpfs) and is accounted against this cgroup limit. The segment is the
    bwa working set's memory, not on top of it — the FMI/.pac/.0123 buffers
    that would have been heap-allocated by `bwa mem` are aliased into shm
    instead. Total cgroup usage stays ~the same as the no-shm case.
    """
    return 52000 if _is_meth(sample_name) else 28000


def _shm_size_mb_for(sample_name: str) -> int:
    """/dev/shm sizing for `bwa-mem2 shm` to stage the in-memory index.

    The packed segment holds the FMI buffers (from .bwt.2bit.64, ~10 GB for
    hg38), .pac (~0.75 GB), and .0123 (~6.4 GB) — total ~17 GB for hg38, and
    ~2x that for the doubled .bwameth.c2t meth reference. We size /dev/shm
    with a few GB of headroom so the segment header / section table and any
    minor bookkeeping have room. Plumbed into the worker job definition's
    linuxParameters.sharedMemorySize via our snakemake-executor-plugin-aws-batch
    fork (default ECS /dev/shm is only 64 MB).
    """
    return 40960 if _is_meth(sample_name) else 20480


def _ref_inputs(wc) -> list[str]:
    """S3-relative paths to every reference-sidecar file the aligner needs.

    Paths are returned relative to the snakemake default-storage-prefix
    (``s3://<bucket>``, set in the rendered AWS Batch profile) so the S3
    storage plugin stages them to local disk before the shell body runs —
    S3 sync then does not count against ``benchmark:`` wall time. The first
    entry is always the plain .fasta; the shell references it via
    ``{input.ref[0]}``.
    """
    sample = CONFIG.samples[wc.sample]
    ref = sample.reference
    fasta_name = CONFIG.references[ref]["fasta_name"]
    base = f"references/{ref}/{fasta_name}"
    if ref == "hg38-meth":
        # For bisulfite, only the doubled `.bwameth.c2t` reference + its
        # bwa-mem2 sidecars are uploaded. bwa-mem2.fg-labs --meth and bwameth.py
        # both auto-append ".bwameth.c2t" to the .fasta path they're given.
        meth_base = f"{base}.bwameth.c2t"
        return [
            base,  # plain .fasta — must be index 0 (the path passed to aligner)
            f"{base}.fai",
            base.replace(".fasta", ".dict"),
            meth_base,
            f"{meth_base}.0123",
            f"{meth_base}.amb",
            f"{meth_base}.ann",
            f"{meth_base}.bwt.2bit.64",
            f"{meth_base}.pac",
        ]
    # Non-meth: plain bwa-mem2 index sidecars.
    return [
        base,  # plain .fasta — must be index 0
        f"{base}.fai",
        f"{base}.0123",
        f"{base}.amb",
        f"{base}.ann",
        f"{base}.bwt.2bit.64",
        f"{base}.pac",
        base.replace(".fasta", ".dict"),
    ]


def _query_fastqs(wc):
    """Ordered list of query-FASTQ input paths for the sample's layout.

    Paired samples -> [r1, r2]; single-end (e.g. SBX) -> [r1]. Paths are
    bucket-relative so the S3 storage plugin stages them. snakemake expands a
    list input space-separated in the shell (order preserved), so the same
    `bwa-mem2 mem ... {input.fastqs}` body serves both layouts.
    """
    sample = CONFIG.samples[wc.sample]
    return [f"{sample.source}{name}" for name in sample.fastq_names]




rule align_fg_labs:
    input:
        ref = _ref_inputs,
        fastqs = _query_fastqs,
    output:
        bam        = "runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam",
        timing     = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/timing.tsv",
        # bwa-mem2 stderr captured for PROCESS()/Index-read-time parsing in
        # the SQLite ingest. Both upstream and fg-labs print these to stderr
        # in the same format; we redirect bwa's stderr only (samtools' stderr
        # still goes to the worker log).
        bwa_stderr = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/bwa.stderr.log",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
        shared_memory_size_mb = lambda wc: _shm_size_mb_for(wc.sample),
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        threads = CONFIG.threads,
        extra   = lambda wc: _fg_labs_flags(wc.sample),
        mem_flags = lambda wc: _mem_flags(wc.sample),
        # `bwa-mem2 mem --meth` auto-appends `.bwameth.c2t` to the given prefix
        # to find its index sidecars. `bwa-mem2 shm` does not — it loads the
        # literal prefix's index. So for meth runs the shm prefix has to be
        # the doubled-c2t prefix; mem still gets the plain prefix and
        # auto-appends. For non-meth they're the same.
        shm_prefix = lambda wc, input: (
            f"{input.ref[0]}.bwameth.c2t" if _is_meth(wc.sample) else input.ref[0]
        ),
    shell:
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.bam}) $(dirname {output.timing})
        bwa-mem2.fg-labs shm {params.shm_prefix}
        trap 'bwa-mem2.fg-labs shm -d || true' EXIT
        # `set -o pipefail` inside the inner bash -c so an aligner that dies
        # (e.g. OOM-killed -> SIGKILL, exit 137) fails the pipeline instead of
        # samtools silently exiting 0 on the partial header stream and caching a
        # header-only BAM as a "successful" alignment.
        tricorder --out {output.timing} -- \
            bash -c 'set -o pipefail; bwa-mem2.fg-labs mem -t {params.threads} {params.mem_flags} {params.extra} \
                {input.ref[0]} {input.fastqs} 2>"{output.bwa_stderr}" \
              | samtools view -@4 -b -o {output.bam} -'
        # Defense in depth: reject a header-only BAM even if the aligner exited 0.
        if [ "$(samtools view -c {output.bam})" -eq 0 ]; then
            echo "ERROR: {output.bam} has 0 alignment records (aligner crashed/OOM?)" >&2
            exit 1
        fi
        """


rule align_baseline:
    """Baseline alignment (bwameth for methylation samples; upstream bwa-mem2 otherwise).

    The aligner is chosen per sample via `baseline_tool` in config/samples.yaml.
    Writes to the baseline/ cache so the run is not repeated for the same upstream tag.

    Page-cache prewarm (untimed) cats the bwa-mem2 index files through
    /dev/null so all FMI/.0123/.pac pages land in the kernel page cache
    before the timed run. Upstream v2.2.1 has no `shm` command, so without
    this the timed run pays a cold-cache index load (~25-30 s on hg38)
    that `align_fg_labs` skips via shm — biasing the speedup comparison.
    `cat` is preferred over a dummy alignment because (a) a 100-pair dummy
    only mmaps a tiny fraction of FMI pages — actual 5M-pair alignments
    page-fault on the rest — whereas cat forces the full sequential read,
    and (b) the cat happens BEFORE `tricorder` so its disk-read cost is
    fully outside the timed region. Eviction concern at 28 GB cgroup +
    17 GB index + ~10 GB bwa heap: fits with margin (fg-labs's shm path
    has the same total working set and doesn't OOM).

    For meth samples the .bwameth.c2t-suffixed files are warmed instead.
    """
    input:
        ref = _ref_inputs,
        fastqs = _query_fastqs,
    output:
        bam        = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/aligned.bam",
        timing     = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/timing.tsv",
        bwa_stderr = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/bwa.stderr.log",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
        container_image = lambda wc: image_for_arch(wc.arch),
    params:
        threads = CONFIG.threads,
        binary  = lambda wc: _baseline_bwa_bin(wc.sample),
        mem_flags = lambda wc: _mem_flags(wc.sample),
        # Index sidecar prefix to warm. Meth samples use the doubled-c2t
        # prefix so the c2t-suffixed index files (which `mem --meth` and
        # `bwameth.py` actually read) get cached.
        prewarm_prefix = lambda wc, input: (
            f"{input.ref[0]}.bwameth.c2t" if _is_meth(wc.sample) else input.ref[0]
        ),
    shell:
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.bam}) $(dirname {output.timing})
        # Untimed page-cache prewarm: read the three index files bwa mmaps
        # into /dev/null so the kernel's file-cache holds them before the
        # timed run. `|| true` keeps the rule alive if a file is missing
        # (worst case: cold cache, still correct).
        cat {params.prewarm_prefix}.bwt.2bit.64 \
            {params.prewarm_prefix}.0123 \
            {params.prewarm_prefix}.pac \
            > /dev/null 2>/dev/null || true
        if [ "{params.binary}" = "bwameth.py" ]; then
            # bwameth.py sanity-checks that `.bwameth.c2t*` files are NEWER than
            # the source `.fasta`; snakemake's storage plugin stages files
            # without preserving source mtimes, so bump the c2t mtimes to now.
            # (`true` keeps the rule's `set -e` semantics intact if the glob
            # expands to nothing — it never should, but defensive.)
            touch {input.ref[0]}.bwameth.c2t* || true
            # bwameth.py wraps bwa-mem2; bwa's stderr passes through unchanged
            # so the same PROCESS()/Index read time parser works.
            # `set -o pipefail`: a SIGKILL'd aligner must fail the pipeline, not
            # let samtools exit 0 on a partial header stream (see align_fg_labs).
            tricorder --out {output.timing} -- \
                bash -c 'set -o pipefail; bwameth.py --threads {params.threads} --reference {input.ref[0]} \
                    {input.fastqs} 2>"{output.bwa_stderr}" \
                  | samtools view -@4 -b -o {output.bam} -'
        else
            # `mem_flags` (e.g. -K for Hi-C) applied here too so the baseline
            # matches the fg-labs invocation and concordance stays symmetric.
            tricorder --out {output.timing} -- \
                bash -c 'set -o pipefail; {params.binary} mem -t {params.threads} {params.mem_flags} \
                    {input.ref[0]} {input.fastqs} 2>"{output.bwa_stderr}" \
                  | samtools view -@4 -b -o {output.bam} -'
        fi
        # Defense in depth: reject a header-only BAM even if the aligner exited 0.
        if [ "$(samtools view -c {output.bam})" -eq 0 ]; then
            echo "ERROR: {output.bam} has 0 alignment records (aligner crashed/OOM?)" >&2
            exit 1
        fi
        """
