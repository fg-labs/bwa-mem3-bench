"""Thread-scaling ladder: measure strong-scaling efficiency on a fixed host.

The entire ladder runs as ONE Batch job on ONE instance. That is the whole point
of the rule, for two reasons:

1. **Validity.** Strong-scaling efficiency ``E(n) = T(1) / (n * T(n))`` is only
   meaningful when every point is measured on the same machine. Right-sizing
   each thread count to its own instance (a 4xlarge for 16 threads, a 16xlarge
   for 64) would give each point a different share of memory bandwidth and L3 —
   exactly what bounds bwa-mem's scaling — so the curve would bend from the
   instances rather than from the aligner. It would also be impossible below 16
   threads: hg38 needs ~16.5 GB resident and c8g is 2 GiB/vCPU, so c8g.4xlarge
   (32 GB) is already the smallest c8g that can run the aligner at all.

2. **Noise.** Host-to-host variance is the dominant noise source in this
   benchmark (see the c7i/m7i notes in CLAUDE.md). Running every rung on one
   host removes it from the comparison entirely, and acquires one spot instance
   instead of seven.

The job reserves ``threads: max_threads`` so AWS Batch gives it the whole
instance — see `tests/test_thread_packing.py` and the `align_fg_labs` note for
why the ``threads:`` directive (not a param) is what drives the vCPU request.

Index load is staged into /dev/shm via ``bwa-mem2 shm`` and therefore excluded
from every timed region, exactly as `align_fg_labs` does. This matters far more
here than in the normal sweep: a serial ~25 s index load would swamp the
64-thread run (~30 s of actual work) and manufacture a scaling cliff that has
nothing to do with the aligner.
"""


def _ladder_spec(cfg) -> str:
    """The ladder as `threads:reps` tokens for the shell loop, e.g. `1:1 2:1 4:2`."""
    return " ".join(f"{step.threads}:{step.reps}" for step in cfg.thread_scaling.ladder)


rule align_thread_scaling:
    input:
        ref = lambda wc: _ref_inputs(wc, meth_index="d3"),
        fastqs = _query_fastqs,
    output:
        tsv = "scaling/{sha}/{sample}/{arch}/scaling.tsv",
    # Reserve the whole instance. Anything less and Batch could co-schedule a
    # second job onto the host, which would corrupt every point on the curve.
    threads: CONFIG.thread_scaling.max_threads
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        # Index in /dev/shm (~17 GB) plus per-thread batch buffers, which grow
        # with the thread count — at 64 threads the working set is several times
        # the 16-thread case. 64 GB leaves wide margin on the 128 GB host.
        mem_mb = 64000,
        shared_memory_size_mb = 20480,
        container_image = lambda wc: image_for_arch(wc.arch),
        # The ladder is long by construction: the 1-thread rung alone is ~16x a
        # 16-thread run. The profile default (7200 s) is too tight.
        runtime = 14400,
    params:
        ladder = lambda wc: _ladder_spec(CONFIG),
        extra = lambda wc: _fg_labs_flags(wc.sample),
        mem_flags = lambda wc: _mem_flags(wc.sample),
    shell:
        r"""
        set -euo pipefail
        OUTDIR=$(dirname {output.tsv})
        mkdir -p "$OUTDIR/runs"

        # Stage the index once, for the whole ladder — pinned in /dev/shm so no
        # rung pays an index load and the cgroup cannot evict it mid-run.
        bwa-mem2.fg-labs shm {input.ref[0]}
        trap 'bwa-mem2.fg-labs shm -d || true' EXIT

        printf 'threads\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\n' > {output.tsv}

        for spec in {params.ladder}; do
            T=${{spec%%:*}}
            R=${{spec##*:}}
            for rep in $(seq 1 "$R"); do
                TS="$OUTDIR/runs/timing.t${{T}}.rep${{rep}}.tsv"
                ERR="$OUTDIR/runs/bwa.t${{T}}.rep${{rep}}.log"
                tricorder --out "$TS" -- \
                    bash -c "set -o pipefail; bwa-mem2.fg-labs mem -t ${{T}} {params.mem_flags} {params.extra} \
                        --bam=0 -o '$OUTDIR/runs/o.bam' \
                        {input.ref[0]} {input.fastqs} 2>'$ERR'"
                # Reject a silently-truncated run before it becomes a data point:
                # an OOM-killed aligner still leaves a timing row.
                if [ "$(samtools view -c "$OUTDIR/runs/o.bam")" -eq 0 ]; then
                    echo "ERROR: t=${{T}} rep=${{rep}} produced 0 records" >&2
                    exit 1
                fi
                # tricorder TSV: header + one row -> s, h:m:s, max_rss, max_vms,
                # max_uss, max_pss, io_in, io_out, mean_load, cpu_time.
                WALL=$(mawk 'NR==2{{print $1}}' "$TS")
                RSS=$(mawk 'NR==2{{print $3}}' "$TS")
                CPU=$(mawk 'NR==2{{print $10}}' "$TS")
                # Same PROCESS() field the SQLite ingest parses, so these numbers
                # are directly comparable to trials.process_seconds.
                PROC=$(grep -oE 'PROCESS\(\).*?:[[:space:]]*[0-9.]+' "$ERR" \
                       | grep -oE '[0-9.]+$' | head -1 || true)
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$T" "$rep" "$WALL" "$CPU" "$RSS" "${{PROC:-NA}}" >> {output.tsv}
                rm -f "$OUTDIR/runs/o.bam"
            done
        done
        """
