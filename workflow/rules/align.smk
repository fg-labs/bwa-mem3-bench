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

The timed region emits UNCOMPRESSED BAM (no sort): fg-labs bwa-mem2 writes it
directly (`--bam=0 -o`), while upstream bwa-mem2 / bwameth (SAM-only) pipe
through `samtools view -u`. This measures the realistic aligner cost — a real
pipeline feeds the aligner's BAM straight into sort/zipper, so compressing it
in the aligner step is wasted work. A second, UNTIMED `samtools view -b` step
compresses the `.raw` output to the final BAM for the compare + S3 upload, then
deletes the `.raw`. Peak transient disk is `.raw` + the final BAM held at once
during that compress: the uncompressed `.raw` is ~3-5x the compressed size, so
the largest 5M-pair sample (~1.3 GB compressed) peaks around ~5-7 GB of worker
ephemeral storage per rep (confirm the Batch job definition's storage covers
this peak before a full run). The final
BAM preserves bwa-mem2's FASTQ-input order, so `compare-bams` consumes it in
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
    # Single meth predicate — delegates to Sample.is_meth (config-validated to
    # agree with the sample's reference; see Sample.__post_init__).
    return CONFIG.samples[sample_name].is_meth


def _mem_mb_for(sample_name: str) -> int:
    """Batch container memory — a hard cgroup OOM-kill line, sized to ~1.5x the
    measured peak RSS (not to the host ceiling). An OOM kill yields a silent
    truncated BAM that exits 0, so the cap wants margin above worst-case peak.

    The bwa-mem3 D3 `mem --meth` path seeds against the doubled `.meth` seed FMI
    (~21 GB) and scores against the original reference by pac-fetching bases from
    `.pac` (~1 GB) on demand (fg-labs/bwa-mem3#177) — so it loads NEITHER the
    original `.0123` (~6.4 GB) NOR the seed `.0123`/`.pac` (~13/1.6 GB), and
    `index --meth` doesn't even emit the original `.0123`. The resident index is
    therefore ~22 GB (bwa-mem3 `memory-and-data-types` docs); on a 5M sample at
    `-t 16` the per-batch working set adds ~5-10 GB, so peak RSS is ~30-32 GB
    worst-case. (The smoke_meth `9c4bbf2` tricorder max_rss of ~21.7 GB is just
    the resident index — smoke's batch is tiny. An earlier ~52-55 GB figure was
    the pre-#177 `feat/meth-d3-seeding` branch, which still loaded the original
    `.0123` plus both seed files.) 48 GB ~= 1.5x the ~32 GB worst-case peak and
    leaves headroom under the ~62 GB container budget on m7i.4xlarge (64 GB host); meth
    is pinned there because its working set exceeds the 32 GB RAM of the non-meth
    *.4xlarge hosts. Non-meth 28 GB is plenty for standard hg38 (~15 GB FMI/.pac
    resident + per-batch).

    Note: for non-meth, `bwa-mem2 shm` stages the segment in /dev/shm (tmpfs),
    accounted against this cgroup limit — but it aliases the FMI/.pac buffers
    `bwa mem` would otherwise heap-allocate, so total usage is ~the same as the
    no-shm case. Meth (D3) uses `shm --meth` to stage the seed-only `.meth`
    FM-index, then pac-fetches the original `.pac` from page cache.
    """
    return 48000 if _is_meth(sample_name) else 28000


def _shm_size_mb_for(sample_name: str) -> int:
    """/dev/shm sizing for `bwa-mem2 shm` to stage the in-memory index.

    Non-meth: the segment holds the FMI buffers (from .bwt.2bit.64, ~10 GB for
    hg38), .pac (~0.75 GB), and .0123 (~6.4 GB) — total ~17 GB for hg38.

    Meth (D3) uses `shm --meth`, which stages the SEED-only `.meth` FM-index +
    contig metadata (~21 GB on hg38; the seed PAC/.0123 are omitted, and the
    original `.pac` is pac-fetched from disk, not staged). So meth needs the
    larger /dev/shm.

    Either way we size /dev/shm with a few GB of headroom for the segment header
    / section table. Plumbed into the worker job definition's
    linuxParameters.sharedMemorySize via our snakemake-executor-plugin-aws-batch
    fork (default ECS /dev/shm is 64 MB).
    """
    return 40960 if _is_meth(sample_name) else 20480


def _ref_inputs(wc, *, meth_index: str) -> list[str]:
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
    # Key the meth-sidecar branch on the same predicate as `params.is_meth`
    # (Sample.is_meth), not on the reference string — config validation
    # guarantees `is_meth == reference.endswith("-meth")`, so staging and the
    # `--meth` exec flag stay in lockstep.
    if sample.is_meth:
        # Two bisulfite index families coexist in references/hg38-meth/; which
        # one is staged depends on the aligner (`meth_index`):
        #   - "c2t" (bwameth.py baseline): the legacy doubled C->T/G->A
        #     reference + upstream bwa-mem2 sidecars (incl. .0123). This is the
        #     D1 contract — bwameth.py wraps upstream bwa-mem2 and aligns
        #     pre-converted reads against the collapsed `.bwameth.c2t` index.
        #   - "d3" (bwa-mem3 `mem --meth`): the *original* 4-letter index
        #     (scored against, pac-fetched — no .0123) PLUS the `.meth.*`
        #     converted seed index. fg-labs/bwa-mem3#174 (D3) seeds in 3-letter
        #     space but scores against the original reference, so it needs the
        #     original index + the `<ref>.meth.*` seed index and must be given
        #     the *original* prefix (passing a `.bwameth.c2t` path now errors).
        common = [
            base,  # plain .fasta — must be index 0 (the path passed to aligner)
            f"{base}.fai",
            base.replace(".fasta", ".dict"),
        ]
        if meth_index == "c2t":
            c2t = f"{base}.bwameth.c2t"
            return common + [
                c2t,
                f"{c2t}.0123",
                f"{c2t}.amb",
                f"{c2t}.ann",
                f"{c2t}.bwt.2bit.64",
                f"{c2t}.pac",
            ]
        return common + [
            f"{base}.amb",
            f"{base}.ann",
            f"{base}.bwt.2bit.64",
            f"{base}.pac",
            f"{base}.meth.fa",
            f"{base}.meth.amb",
            f"{base}.meth.ann",
            f"{base}.meth.bwt.2bit.64",
            f"{base}.meth.pac",
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
        ref = lambda wc: _ref_inputs(wc, meth_index="d3"),
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
    # Declared as a snakemake `threads:` directive, NOT a param, because our
    # snakemake-executor-plugin-aws-batch fork derives the Batch job's VCPU
    # resourceRequirement from it (batch_job_builder.py: `vcpu = max(1,
    # job.threads ...)`). As a bare param the aligner still ran `-t 16` while
    # Batch was told the job needed ONE vCPU, so Batch packed by memory alone.
    # On the 32 GB *.4xlarge archs the 28 GB request incidentally kept that to
    # one job per host, but m7i.4xlarge has 64 GB — so TWO non-meth alignments
    # landed on one 16-vCPU host and each ran 16 threads against 8 effective
    # CPUs. Measured on the 394f8f8 sweep: m7i mean_load fell 1494 -> 792 and
    # parallel efficiency 93% -> 50% on wgs-5M/wes-5M, inflating wall by
    # 63-96% while cpu_time FELL. meth was immune (its 48 GB request cannot
    # double-pack), which is what identified the mechanism.
    #
    # NOTE: snakemake clamps `threads` to `--cores` when that flag is given.
    # coordinator-entrypoint.sh deliberately passes only `--jobs`, so the value
    # survives; adding `cores:` to the profile would silently clamp vCPU here
    # and reintroduce the bug. tests/test_thread_packing.py guards that.
    threads: CONFIG.threads
    params:
        extra   = lambda wc: _fg_labs_flags(wc.sample),
        mem_flags = lambda wc: _mem_flags(wc.sample),
        is_meth = lambda wc: "1" if _is_meth(wc.sample) else "0",
    shell:
        # Both paths stage the index into /dev/shm via `bwa-mem2 shm` so the
        # FMI load is pinned and excluded from the timed region; `mem`
        # auto-attaches. They differ only in the `--meth` flag:
        #
        #   Non-meth — `shm <ref>` stages the single hg38 index; `mem <ref>`.
        #
        #   Meth (D3) — `shm --meth <ref>` stages the seed-only `.meth` FM-index
        #   (~21 GB; the seed PAC/.0123 are never staged — mem extends against the
        #   ORIGINAL reference, not the seed). `mem --meth <ref>` (`--meth` comes
        #   from {params.extra}) auto-attaches and pac-fetches the original bases
        #   from `<ref>.pac`. That `.pac` is NOT in the shm segment, so we warm it
        #   into page cache (untimed) to keep the timed region load-free. mem is
        #   given the plain `<ref>` prefix; a `.bwameth.c2t` path would now error
        #   (fg-labs/bwa-mem3#174). `shm --meth`: fg-labs/bwa-mem3 shm.md.
        #
        # The aligner writes BAM directly (`--bam=0 -o`), so there is no pipe to
        # fail; an aligner that dies (e.g. OOM-killed -> SIGKILL, exit 137)
        # leaves a truncated/empty `.raw` that the record-count check rejects.
        # `set -o pipefail` is retained defensively (harmless without a pipe).
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.bam}) $(dirname {output.timing})
        if [ "{params.is_meth}" = "1" ]; then
            bwa-mem2.fg-labs shm --meth {input.ref[0]}
            trap 'bwa-mem2.fg-labs shm -d || true' EXIT
            cat {input.ref[0]}.pac > /dev/null 2>/dev/null || true
        else
            bwa-mem2.fg-labs shm {input.ref[0]}
            trap 'bwa-mem2.fg-labs shm -d || true' EXIT
        fi
        # Timed region emits UNCOMPRESSED BAM straight from bwa-mem2 (`--bam=0
        # -o`): no SAM-text serialization and no separate samtools process, and
        # no wasted zlib work — realistic, since a real pipeline feeds the
        # aligner's BAM straight into sort/zipper, which re-read it anyway.
        # `--meth` (from {params.extra}) already implies BAM output and honors
        # `-o`, so the one invocation covers both meth and non-meth. bash -c
        # scopes the `2>` redirect to the aligner so tricorder's own stderr is
        # untouched; a crashed aligner leaves a truncated/empty `.raw` that the
        # record-count check below rejects.
        tricorder --out {output.timing} -- \
            bash -c 'set -o pipefail; bwa-mem2.fg-labs mem -t {threads} {params.mem_flags} {params.extra} \
                --bam=0 -o "{output.bam}.raw" \
                {input.ref[0]} {input.fastqs} 2>"{output.bwa_stderr}"'
        # Defense in depth: reject a header-only BAM even if the aligner exited 0.
        if [ "$(samtools view -c {output.bam}.raw)" -eq 0 ]; then
            echo "ERROR: {output.bam}.raw has 0 alignment records (aligner crashed/OOM?)" >&2
            exit 1
        fi
        # UNTIMED: compress the uncompressed timed output to the final BAM for
        # the downstream compare + S3 upload. Records are byte-identical to the
        # `.raw`, so concordance is unaffected.
        samtools view -@4 -b -o {output.bam} {output.bam}.raw
        rm -f {output.bam}.raw
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
        # bwameth.py is a D1 (3-letter) aligner wrapping upstream bwa-mem2, so
        # the baseline always stages the legacy `.bwameth.c2t` index family —
        # NOT the bwa-mem3 D3 original+`.meth.*` set (see `_ref_inputs`).
        ref = lambda wc: _ref_inputs(wc, meth_index="c2t"),
        fastqs = _query_fastqs,
    output:
        bam        = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/aligned.bam",
        timing     = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/timing.tsv",
        bwa_stderr = "baseline/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/bwa.stderr.log",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
        container_image = lambda wc: image_for_arch(wc.arch),
    # See align_fg_labs: `threads:` (not a param) so the executor plugin
    # reserves the vCPUs the aligner actually uses. The baseline must match the
    # fg-labs rule exactly or the two arms would be timed under different
    # contention, breaking the speedup comparison.
    threads: CONFIG.threads
    params:
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
                bash -c 'set -o pipefail; bwameth.py --threads {threads} --reference {input.ref[0]} \
                    {input.fastqs} 2>"{output.bwa_stderr}" \
                  | samtools view -@4 -u -o {output.bam}.raw -'
        else
            # `mem_flags` (e.g. -K for Hi-C) applied here too so the baseline
            # matches the fg-labs invocation and concordance stays symmetric.
            tricorder --out {output.timing} -- \
                bash -c 'set -o pipefail; {params.binary} mem -t {threads} {params.mem_flags} \
                    {input.ref[0]} {input.fastqs} 2>"{output.bwa_stderr}" \
                  | samtools view -@4 -u -o {output.bam}.raw -'
        fi
        # Upstream bwa-mem2 / bwameth emit SAM text, so `samtools view -u`
        # materializes UNCOMPRESSED BAM in the timed region — symmetric with the
        # fg-labs rule's uncompressed output so the comparison isn't skewed by a
        # compression step. (bwa-mem2 can't self-emit BAM; that extra samtools
        # parse is a genuine part of its cost.)
        # Defense in depth: reject a header-only BAM even if the aligner exited 0.
        if [ "$(samtools view -c {output.bam}.raw)" -eq 0 ]; then
            echo "ERROR: {output.bam}.raw has 0 alignment records (aligner crashed/OOM?)" >&2
            exit 1
        fi
        # UNTIMED: compress to the final BAM for the compare + S3 upload.
        samtools view -@4 -b -o {output.bam} {output.bam}.raw
        rm -f {output.bam}.raw
        """
