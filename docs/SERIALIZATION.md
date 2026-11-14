# Serialization benchmark

Measures bwa-mem3's native BAM writer (`--bam`) against the classic route, where the aligner
writes SAM to stdout and `samtools view` converts it. Rules: `workflow/rules/serialization.smk`;
arms and commands: `bwa_mem3_bench/serialization_arms.py`.

## Targets

| Target | What it runs |
|---|---|
| `serialization_fidelity` | One on-demand arena job per (sha, arch, sample, mode). |
| `serialization_timing` | One on-demand arena job per (sha, arch, sample, endpoint). |
| `serialization` | Both of the above. |
| `serialization_smoke` | `smoke-1M` on m8a: fidelity in both modes, timing at every endpoint with one measured round. |

**Fidelity** aligns the sample three times with the same flags, `-K` and output levels as the
timing (one shared argv builder; the sample's `mem_flags` then `fg_labs_flags`, `--fast` in fast
mode, stock index), on `fidelity_threads` threads: SAM piped into `samtools view -u`, native
`--bam=0`, and native `--bam=1 --bam-threads <helpers>`.
Each BAM must hold exactly 2 × FASTQ pairs primary records (`samtools view -c -F 0x900`), and
`fgumi compare bams --mode content` must report each native BAM IDENTICAL to the SAM-route BAM.
The count catches a read lost by every route alike, which a pairwise compare cannot see. BAMs
stay in local scratch and are deleted; outputs are written only when every check passed. On a
failure the job prints the failing step's log tail, or the fgumi report, to stderr.

**Timing** runs every arm in one job on the whole arena host (`arena.threads` CPUs): bwa-mem3
native and SAM route, in each mode, on the stock and the arena's dense (`re-sa`) index; bwa,
minibwa and upstream bwa-mem2 (x86 only) through the SAM route, stock index, default mode. The
run order comes from the arena's scheduler (`arena_arms.build_schedule`): arms are grouped into
blocks by the index files they read (bwa, minibwa, stock bwa-mem2-format, dense), the block order
is shuffled, and each block runs a warmup round 0 and then `reps` measured rounds. Each round is
a fresh permutation of the block's arms, seeded by the SHA, in which every bwa-mem3 native/SAM
pair runs back to back, alternating which of the two leads. Before every run, an untimed `cat`
of the arm's index files and the reads runs `arena.prewarm_passes` times. Each run is timed with
`tricorder --interval 0.1`, as in the arena.

| Endpoint | Output | Threads | CPUs |
|---|---|---|---|
| `pinned` | level-1 BAM to a file | aligner `budget - helpers`, BAM compression `helpers` | `taskset -c 0-(budget-1)` |
| `unpinned` | same as pinned | same as pinned | whole host |
| `practical` | bwa-mem3 `--bam=0`, others SAM, into `/dev/null` | aligner `budget` | `taskset -c 0-(budget-1)` |

`pinned` compares the two routes on equal CPUs, `unpinned` shows what each does with a free
host, and `practical` shows the cost when a downstream tool consumes the stream. The two routes'
compression budgets, `--bam-threads N` and `samtools view -@ N`, are only approximately equal:
the two tools split work across their threads differently. After each pinned or unpinned run, an
untimed `samtools view -c -F 0x900 -@ <budget>` counts the primary records; a short BAM marks the
run `failed`. A practical run's output is discarded, so nothing checks it: its rows are
`unchecked`, and downstream analysis should trust them only through that binary's fidelity result
and its pinned rows.

Every file-destination arm writes through `| cat > file`, never to the file directly: htslib's
`bgzf_close` fdatasyncs a regular-file output, which would time the disk instead of the writer
(see "Why the final writer is `cat`" in `workflow/rules/arena.smk`).

## Settings

Defaults live in `config/serialization.yaml`. Any key can be overridden with
`--config serialization_<key>=<value>`; list keys take a comma-separated string.

| Key | Default | Meaning |
|---|---|---|
| `samples` | `[]` | Samples for the full targets (paired-end, non-methylation). |
| `modes` | `default, fast` | bwa-mem3 modes. |
| `endpoints` | `pinned, unpinned, practical` | Timing endpoints. |
| `tools` | `bwa-mem3, bwa, bwa-mem2, minibwa` | Timed tools. |
| `indexes` | `stock, dense` | Indexes for the bwa-mem3 arms. |
| `archs` | `m8a, m8g` | Archs for both fidelity and timing, so results pair. |
| `budget` | `8` | CPUs for pinned and practical. |
| `helpers` | `2` | BAM compression threads. |
| `reps` | `5` | Measured rounds per timing job. |
| `batch_k` | `batch_bases` | `-K` for bwa-mem3, bwa and upstream bwa-mem2. |
| `fidelity_threads` | `16` | `-t` for the fidelity alignments, at most `arena.threads`. |
| `aligner` | `bwa-mem2.fg-labs` | The bwa-mem3 binary in the image. |

Every rejection names the fix. At parse time, for any target, the workflow rejects a bad
`serialization_*` value on its own:

- an unknown `serialization_*` key, a value outside these vocabularies, an empty or repeated
  list entry, and a non-positive or boolean integer;
- a `budget` below `helpers + 1`;
- bwa, minibwa or bwa-mem2 in `tools` without `default` in `modes` and `stock` in `indexes`.

Checks against the rest of the config run only when a serialization target or job is requested,
and only against what that target runs, so a setting such as `arena.dense_sa_shift: 3` never
breaks `all`, `smoke` or `arena`, and `serialization_smoke` checks its own sample and arch rather
than the full target's (the full targets never need m8a or `smoke-1M`):

- a sample or arch not in `samples` or `arena.archs`, or, for `serialization_smoke`, the smoke
  arch (m8a) missing from `arena.archs`;
- a `budget` above `arena.threads`, or not below `arena.threads` when pinned and unpinned both
  run (always so for `serialization_smoke`), and a `fidelity_threads` above `arena.threads`;
- `dense` when `arena.dense_sa_shift` is the stock shift (pass `indexes=stock`), or a sample with
  no dense reference;
- a selection that leaves an arch with no arm (upstream bwa-mem2 runs only on x86);
- a single-end or methylation sample; a sample whose flags set an option the benchmark
  controls (`-t`, `-K`, `-o`, `-f` anywhere in a getopt bundle such as `-5St4`, and `--fast`,
  `--bam`, `--bam-threads` or abbreviations); a `--compat` sample when fast mode runs (the smoke
  sample is checked against both modes); and, when minibwa is timed, a `mem_flags` entry with no
  minibwa equivalent. A `{sample}` in a requested path passes the same checks.

`fg_labs_sha` must be 7-40 hex characters, optionally followed by `-<make target>` as
`submit --make-target` produces. Snakemake reads a digits-only value as a number, so pass a SHA
that contains a letter or quote it. A requested output path whose `<sha>` differs from
`fg_labs_sha` is rejected, since the job would run the `fg_labs_sha` image. Each job also fails if
the FASTQs are empty or their line counts do not match.

## Running on biffix

Register the workflow as in [Running the bless on biffix](RELEASE.md#running-the-bless-on-biffix).
Both fidelity and timing run on the arena queue classes (`arch-m8a-arena`, `arch-m8g-arena`).
Params for the smoke:

```json
{
  "batch_queue_prefix": "<deployment queue prefix>",
  "ecr_repo_uri": "<ecr-uri>",
  "fg_labs_sha": "<sha>",
  "s3_bucket": "<bucket>",
  "shared_root": "s3://<bucket>/"
}
```

Submit `--target serialization_smoke`. For the full run add
`"serialization_samples": "<sample>[,<sample>...]"` and submit `--target serialization`.

Run the smoke first and extrapolate from its `timings.tsv`: a timing job costs about
`(reps + 1)` times one round's summed walls, plus the prewarm `cat` before each run. Keep each
job under the Batch timeout (14,400 s); narrow `tools`, `modes` or `indexes`, or lower `reps`,
if a sample's estimate does not fit.

## Outputs

Under `serialization/<sha>/<arch>/<sample>/`:

- `fidelity/<mode>/result.tsv`: one row per native compression level, with columns
  `compression`, `expected_primary`, `sam_primary`, `native_primary`, `fgumi_exit`,
  `fgumi_result_line`.
- `fidelity/<mode>/versions.json`: bwa-mem3, samtools and fgumi versions, the aligner's
  sha256, `batch_k`, the flag tokens and `helpers`.
- `fidelity/<mode>/compare_bam{0,1}.txt`: the fgumi reports.
- `timing/<endpoint>/timings.tsv` (`timing-smoke/` for the smoke target): one row per run, with
  columns `label tool mode index endpoint route block round position status exit_code
  primary_count wall_s cpu_s max_rss_mb process_s index_read_s`. `block` is the index family and
  round 0 is each block's warmup. `status` is `ok` (counted, complete), `unchecked` (practical,
  not counted) or `failed` (non-zero exit or a short BAM); a failed row has `NA` for every
  metric, and `primary_count` is `NA` where nothing was counted.
- `timing/<endpoint>/meta.json` (host identity from `emit-host-meta`) and `settings.json`
  (budget, helpers, `batch_k`, flags, endpoint, `dense_sa_shift`, `prewarm_passes`, the bwa-mem3
  and samtools versions, the timed comparators' versions, the aligner's sha256).

A timing job fails only when no measured run (round 1 and up) completed (`ok` or `unchecked`).
It prints, on stderr, the status counts and every (label, route) with no completed measured
run, and the stderr tail of each failed run; failing the job for one bad arm would make
Snakemake delete the good measurements with it.

The per-run stderr and tricorder files (`timing/<endpoint>/runs/`) and the fidelity alignment
logs (`fidelity/<mode>/logs/`) are not declared outputs: they stay on the worker and are not
uploaded on Batch. Fetch the declared outputs with
`aws s3 sync s3://<bucket>/serialization/<sha>/ serialization/<sha>/`; `cli sync-local` does not
cover this tree.

The bench only emits these files. Correctness gating across runs, matching timing rows to their
fidelity results by aligner sha256 and flags, and tables and plots are left to downstream
analysis code.
