# Serialization benchmark

Measures bwa-mem3's native BAM writer (`--bam`) against the classic route, where the aligner
writes SAM to stdout and `samtools view` converts it. Rules: `workflow/rules/serialization.smk`;
arms and commands: `bwa_mem3_bench/serialization_arms.py`.

## Targets

| Target | What it runs |
|---|---|
| `serialization_fidelity` | One job per (sha, arch, sample, mode) on the arch's spot queue. |
| `serialization_timing` | One on-demand arena job per (sha, arch, sample, endpoint). |
| `serialization` | Both of the above. |
| `serialization_smoke` | `smoke-1M` on m8a: fidelity in both modes, timing at every endpoint with one measured round. |

**Fidelity** aligns the sample three times with the exact bwa-mem3 command the timing uses
(same `-K`, the sample's `mem_flags` then `fg_labs_flags`, `--fast` in fast mode, stock index):
SAM piped into `samtools view -u`, native `--bam=0`, and native `--bam=1 --bam-threads <helpers>`.
Each BAM must hold exactly 2 × FASTQ pairs primary records (`samtools view -c -F 0x900`), and
`fgumi compare bams --mode content` must report each native BAM IDENTICAL to the SAM-route BAM.
The count catches a read lost by every route alike, which a pairwise compare cannot see. BAMs
stay in local scratch and are deleted; outputs are written only when every check passed.

**Timing** runs every arm in one job on the whole 16-vCPU arena host: bwa-mem3 native and SAM
route, in each mode, on the stock and the arena's dense (`re-sa`) index; bwa, minibwa and
upstream bwa-mem2 (x86 only) through the SAM route, stock index, default mode. Each job runs a
warmup round and `reps` measured rounds; the order is shuffled per round, seeded by the SHA, with
each bwa-mem3 native/SAM pair kept adjacent. Before every run an untimed `cat` warms the arm's
index files. Each run is timed with `tricorder --interval 0.1`, as in the arena.

| Endpoint | Output | Threads | CPUs |
|---|---|---|---|
| `pinned` | level-1 BAM to a file | aligner `budget - helpers`, BAM compression `helpers` | `taskset -c 0-(budget-1)` |
| `unpinned` | same as pinned | same as pinned | whole host |
| `practical` | bwa-mem3 `--bam=0`, others SAM, into `/dev/null` | aligner `budget` | `taskset -c 0-(budget-1)` |

`pinned` compares the two routes on equal CPUs, `unpinned` shows what each does with a free
host, and `practical` shows the cost when a downstream tool consumes the stream. After each
file-destination run, an untimed count checks the primary records; a short BAM marks the run
`failed`.

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
| `batch_k` | `160000000` | `-K` for bwa-mem3, bwa and upstream bwa-mem2. |
| `fidelity_threads` | `16` | `-t` for the fidelity alignments. |
| `aligner` | `bwa-mem2.fg-labs` | The bwa-mem3 binary in the image. |

The workflow rejects at parse time any setting outside these vocabularies, a non-positive or
boolean integer, a `budget` below `helpers + 1` (or not below the host's 16 CPUs when pinned and
unpinned are both selected), a selection that leaves an arch with no arm, a sample whose flags
set an option the benchmark controls (`--fast`, `-K`, `-t`, `-o`, `-f`, `--bam*`, bundled or
abbreviated forms), and a `--compat` sample when fast mode is selected. `fg_labs_sha` must be
7-40 hex characters; Snakemake reads a digits-only value as a number, so pass a SHA that
contains a letter or quote it.

## Running on biffix

Register the workflow as in [Running the bless on biffix](RELEASE.md#running-the-bless-on-biffix)
(the arena queue classes are required). Params for the smoke:

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
  columns `label tool mode index endpoint route round position status wall_s cpu_s max_rss_mb
  process_s index_read_s`. Round 0 is the warmup. `status` is `ok` or `failed`; unknown values
  are `NA`. A job fails if every run failed.
- `timing/<endpoint>/meta.json` (host identity from `emit-host-meta`) and `settings.json`
  (budget, helpers, `batch_k`, flags, endpoint, bwa-mem3 version, aligner sha256).

The bench only emits these files. Correctness gating across runs, matching timing rows to their
fidelity results by aligner sha256 and flags, and tables and plots are left to downstream
analysis code.
