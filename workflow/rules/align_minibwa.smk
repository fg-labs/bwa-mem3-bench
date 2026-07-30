"""Rule to run lh3/minibwa and emit an unsorted BAM.

Mirrors `align_baseline` in shape (untimed page-cache prewarm, tricord-timed
subprocess emitting UNCOMPRESSED BAM via `samtools view -u`, then an untimed
`samtools view -b` compress to the final BAM) but uses minibwa's index format
(`.l2b` + `.mbw` sidecars) instead of the bwa-mem2 FMI. Peak transient disk is
the uncompressed `.raw` (~3-5x the compressed size) plus the final BAM held at
once during the compress step — see `align_baseline` for the sizing note.

Caching model — keyed on the minibwa SHA, not the fg-labs SHA. minibwa is an
independent third tool whose output depends only on its own source pin, so its
results live under a dedicated `minibwa/<minibwa_sha>/` cache prefix (exactly
as the upstream baseline lives under `baseline/bwa-mem2-<tag>/`). A re-run for
a new fg-labs SHA reuses the cached minibwa BAMs/timings via the S3 storage
plugin; only bumping `MINIBWA_SHA` (i.e. the vendored submodule) invalidates
them and forces a re-alignment.

Warm-symmetric timing — the timed region must exclude index load so the
minibwa-vs-bwa-mem3 number reflects mapping work, not cold mmap fault-in.
`align_fg_labs` excludes load via `bwa-mem2 shm`; `align_baseline` via a
`cat`-into-page-cache prewarm. minibwa has no shm command and mmaps its index
directly, so we mirror the baseline's prewarm: `cat` the `.l2b` + `.mbw`
sidecars to /dev/null (untimed, before tricorder) so their pages are resident
when the timed `minibwa map` run faults them in. Without this, minibwa would
eat its cold index load inside the timed region while bwa-mem3 does not —
apples-to-oranges, biased against minibwa, and sensitive to EBS throughput.

Output equivalency vs bwa-mem2 is intentionally not measured (no compare-bams
against these BAMs) — we are after wall time only.

Used only on the local-only `minibwa-bench` branch.
"""


def _minibwa_ref_inputs(wc) -> list[str]:
    """S3-relative paths to the minibwa-format reference sidecars.

    Returned relative to the snakemake default-storage-prefix so the S3
    storage plugin stages them before the shell body runs. The first entry
    is always the plain .fasta; the shell references it via ``{input.ref[0]}``.
    minibwa auto-appends ``.l2b``/``.mbw`` to the prefix it is given.
    """
    sample = CONFIG.samples[wc.sample]
    # The minibwa index sidecars live under the plain DNA reference for BOTH
    # modes — minibwa's BS-seq index (`index --meth`) writes `.l2b`, `.mbw` and
    # an additional `.meth.mbw` (the C->T/G->A converted FM-index) alongside the
    # DNA FASTA, not under a separate `hg38-meth` tree. Strip the `-meth` suffix
    # from the (meth) sample's reference to reach it.
    ref = sample.reference.removesuffix("-meth")
    fasta_name = CONFIG.references[ref]["fasta_name"]
    base = f"references/{ref}/{fasta_name}"
    # `map --meth` loads `.l2b` + `.meth.mbw`; plain `map` loads `.l2b` + `.mbw`
    # (see mb_idx_load). Stage exactly the BWT the run will use so the prewarm
    # and the timed region match.
    bwt = f"{base}.meth.mbw" if _is_meth_sample(wc.sample) else f"{base}.mbw"
    return [
        base,  # plain .fasta — must be index 0 (path passed to minibwa)
        f"{base}.l2b",
        bwt,
    ]


rule align_minibwa:
    input:
        ref = _minibwa_ref_inputs,
        fastqs = _query_fastqs,
    output:
        bam            = "minibwa/{minibwa_sha}/{sample}/{arch}/rep-{rep}/aligned.minibwa.bam",
        timing         = "minibwa/{minibwa_sha}/{sample}/{arch}/rep-{rep}/benchmarks/timing.minibwa.tsv",
        minibwa_stderr = "minibwa/{minibwa_sha}/{sample}/{arch}/rep-{rep}/benchmarks/minibwa.stderr.log",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        # Meth runs the bisulfite `.meth.mbw` index (~12.8 GB) + `.l2b` (~0.8 GB)
        # mmapped, plus the page-cache prewarm of both — give it the same 48 GB
        # cgroup the bwa-mem3 meth arm uses (m7i has 64 GB). Non-meth (~6.4 GB
        # `.mbw`) is fine at 28 GB.
        mem_mb = lambda wc: 48000 if _is_meth_sample(wc.sample) else 28000,
        container_image = lambda wc: image_for_arch(wc.arch),
    # See align_fg_labs: a `threads:` directive (not a param) so the executor
    # plugin reserves the vCPUs minibwa actually uses. minibwa is the wall-time
    # probe the bwa-mem3 numbers are quoted against, so it must be scheduled
    # under the same one-alignment-per-host contention as the other two arms.
    threads: CONFIG.threads
    params:
        # Sample `mem_flags` translated to minibwa's CLI (see
        # Sample.minibwa_flags). Empty for most samples; for hic-1M this is
        # `-5 -P --rescue=0` — the minibwa equivalent of the bwa `-5 -S -P` the
        # bwa-mem3/bwa-mem2 arms use, so minibwa also skips the
        # Hi-C-inappropriate mate rescue and the comparison stays
        # apples-to-apples.
        flags = lambda wc: " ".join(CONFIG.samples[wc.sample].minibwa_flags),
        # `--meth` for methylation samples: minibwa maps the EM-seq reads
        # bisulfite-aware against the `.meth.mbw` BS-seq index (directional:
        # read1 C->T, read2 G->A), exactly the supported way to run it on meth
        # data. Empty for non-meth samples. (minibwa emits no XM tags even in
        # --meth mode, so methylation-level correlation is NA; it is scored on
        # placement + variant representation.)
        meth_flag = lambda wc: "--meth" if _is_meth_sample(wc.sample) else "",
    shell:
        # minibwa `map` emits SAM by default (since lh3/minibwa r387; `-f`
        # selects PAF). The index prefix is the plain .fasta path
        # (input.ref[0]); minibwa auto-appends .l2b/.mbw to find the sidecars.
        # `{input.fastqs}` expands to [r1, r2] for paired samples or [r1] for
        # single-end (e.g. SBX) — minibwa's CLI accepts either.
        #
        # Untimed page-cache prewarm: cat the .l2b/.mbw index sidecars to
        # /dev/null so their pages are resident before the timed run — keeps
        # the timed region symmetric with align_fg_labs (shm) / align_baseline
        # (cat prewarm). `|| true` keeps the rule alive on a missing file
        # (worst case: cold cache, still correct).
        #
        # Stderr is tee'd to BOTH the rule's output file (uploaded to S3 on
        # success) AND the worker's stderr (captured by CloudWatch). The
        # CloudWatch copy survives even when snakemake removes the rule's
        # outputs on failure — essential for diagnosing failed runs.
        # `set -o pipefail` inside the inner bash -c so a SIGKILL'd aligner
        # (e.g. OOM, exit 137) fails the pipeline instead of samtools exiting
        # 0 on a partial header stream and caching a header-only BAM.
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.bam}) $(dirname {output.timing})
        cat {input.ref[1]} {input.ref[2]} > /dev/null 2>/dev/null || true
        # minibwa emits SAM text, so `samtools view -u` materializes
        # UNCOMPRESSED BAM in the timed region — symmetric with the fg-labs /
        # baseline rules so the wall-time comparison isn't skewed by a
        # compression step the aligner's real downstream (sort/zipper) discards.
        tricorder --out {output.timing} -- \
            bash -c 'set -o pipefail; minibwa map -t {threads} {params.flags} {params.meth_flag} \
                {input.ref[0]} {input.fastqs} \
                2> >(tee "{output.minibwa_stderr}" >&2) \
              | samtools view -@4 -u -o {output.bam}.raw -'
        # Defense in depth: reject a header-only BAM even if minibwa exited 0.
        if [ "$(samtools view -c {output.bam}.raw)" -eq 0 ]; then
            echo "ERROR: {output.bam}.raw has 0 alignment records (minibwa crashed/OOM?)" >&2
            exit 1
        fi
        # UNTIMED: compress to the final BAM for S3 upload (no compare rule for
        # minibwa, but keep the on-disk artifact compressed for storage parity).
        samtools view -@4 -b -o {output.bam} {output.bam}.raw
        rm -f {output.bam}.raw
        """
