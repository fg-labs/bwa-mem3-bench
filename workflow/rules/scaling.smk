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
    """The ladder as `threads:reps` tokens for the shell loop, e.g. `1:1 2:1 4:2`.

    Overridable per-run with `--config ladder=16:3,32:3,64:3` for ad-hoc
    diagnostics — e.g. probing only the high thread counts without paying for
    the 1-thread rung, which alone is ~40% of the full ladder's wall time.

    The checked-in config is still validated to contain a 1-thread rung, because
    E(n) = T(1)/(n*T(n)) is undefined without it and Gate #3 would silently have
    nothing to gate. An override that omits it produces a ladder whose rows are
    still ingested and whose profile is still captured, but which yields no
    efficiency — `_scaling_efficiency` skips a ladder with no T(1), so the gate
    no-ops rather than reporting a wrong number.

    The override still goes through `parse_ladder_override`, which holds it to the
    same integer/positivity rules as the YAML ladder. These tokens are pasted into
    the rule's shell loop, so an unvalidated one would reach the worker and blow up
    an hour into a spot job — the failure mode the config validation exists to
    prevent.
    """
    override = config.get("ladder", "")
    steps = parse_ladder_override(override) if override else cfg.thread_scaling.ladder
    return " ".join(f"{step.threads}:{step.reps}" for step in steps)


# Resolved at parse time, not inside a params lambda, so a malformed override
# aborts the coordinator immediately instead of at job-build time.
LADDER_SPEC = _ladder_spec(CONFIG)


rule align_thread_scaling:
    input:
        ref = lambda wc: _ref_inputs(wc, meth_index="d3"),
        fastqs = _query_fastqs,
    output:
        tsv = "scaling/{sha}/{sample}/{arch}/scaling.tsv",
        # The aligner's own runtime profile, one tarball for the whole ladder.
        #
        # This is NOT optional detail. scaling.tsv reduces each rung to
        # wall/cpu/rss/PROCESS(), which is enough to compute efficiency but NOT
        # enough to explain it. bwa-mem3's stderr decomposes the run into
        # main_mem, read IO, SAM-write IO, index read, MEM_PROCESS_SEQ, and the
        # per-kernel breakdown (MEM_CHAIN / MEM_SA / BSW) — the only way to say
        # WHY a rung scaled the way it did.
        #
        # The first ladder shipped without this and immediately needed it: at 64
        # threads wall exceeded PROCESS() by 7.4 s (vs 2.2 s at 16), and nothing
        # in scaling.tsv could attribute that. Worse, PROCESS() *includes* read
        # IO (13.2 s of a 92 s run at 16 threads), so if FASTQ reading does not
        # scale it silently becomes a large fraction of PROCESS() at high thread
        # counts — making the efficiency number itself misleading. Keeping the
        # profile is what distinguishes an aligner limit from an IO limit.
        profile = "scaling/{sha}/{sample}/{arch}/runtime-profiles.tar.gz",
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
        ladder = LADDER_SPEC,
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

        printf 'threads\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\tmain_mem_s\tread_io_s\tsam_io_s\tkernel_s\n' > {output.tsv}

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
                # Additional phase timings from the aligner's own runtime
                # profile. These explain the efficiency number rather than just
                # reporting it — in particular `read_io`, which lives INSIDE
                # PROCESS() and does not necessarily scale with thread count.
                prof() {{ grep -oE "$1[^:]*:[[:space:]]*[0-9.]+" "$ERR" \
                          | grep -oE '[0-9.]+$' | head -1 || true; }}
                MAINMEM=$(prof 'Time taken for main_mem function')
                READIO=$(prof 'Reading IO time \(reads\) avg')
                SAMIO=$(prof 'Writing IO time \(SAM\) avg')
                KERNEL=$(prof 'Total kernel \(smem\+sal\+bsw\) time avg')
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$T" "$rep" "$WALL" "$CPU" "$RSS" "${{PROC:-NA}}" \
                    "${{MAINMEM:-NA}}" "${{READIO:-NA}}" "${{SAMIO:-NA}}" "${{KERNEL:-NA}}" \
                    >> {output.tsv}
                rm -f "$OUTDIR/runs/o.bam"
            done
        done
        # Keep every rung's raw stderr + tricorder TSV. Without this the profile
        # is unrecoverable once the worker exits, which is exactly what happened
        # to the first ladder.
        tar -czf {output.profile} -C "$OUTDIR" runs
        """
