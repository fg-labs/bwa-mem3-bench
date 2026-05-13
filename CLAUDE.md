# CLAUDE.md

Guidance for Claude Code working in `bwa-mem3-bench`.

## Project

This repo benchmarks **bwa-mem3** — the next-generation bwa-mem2 successor
in development at [fg-labs/bwa-mem3](https://github.com/fg-labs/bwa-mem3) —
against upstream `bwa-mem2/bwa-mem2 v2.2.1`. See `README.md` for the public
design overview and `docs/data-setup.md` for input data sources.

## Repo conventions

- **Python** — Fulcrum style, 100-char lines, ruff + mypy strict, defopt for
  CLI tools. `pixi run check` must pass.
- **Rust** — latest stable pinned via `rust-toolchain.toml`, `forbid(unsafe_code)`
  at the workspace level, `cargo clippy --all-features --all-targets -- -D warnings`
  and `cargo fmt` must pass.
- **Commits** — conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`, `ci:`). No AI attribution. Stage files by explicit path
  (no `git add -A`).
- **Branches** — short, kebab-case, descriptive (e.g. `fix-coordinator-timeout`).
- **GitHub Actions** — pin by commit SHA with a version comment.

## Running a benchmark end-to-end

All commands are `pixi run python -m bwa_mem3_bench.cli <subcommand>`.

1. **Deploy infrastructure (once)**: `cd cdk && cdk deploy --all`.
2. **Upload inputs (once per sample)**: `upload-data --what all` — references +
   fastqs to `s3://<your-bucket>/{references,data}/`. See `docs/data-setup.md`
   for how to obtain the inputs.
3. **Render the AWS Batch profile** (once per checkout, or whenever
   `cdk/outputs.json` changes): `pixi run render-profile`. Reads the rendered
   ECR URI / bucket / region from `cdk/outputs.json` (or env vars) and writes
   `workflow/profiles/aws-batch/config.yaml` (gitignored).
4. **Build + push image**: `build --fg-labs-sha <sha> --image-name <ecr-uri>
   --push`. **Always required** when `workflow/`, `config/`, `docker/`, or the
   `bwa_mem3_bench` package changes — those are `COPY`ed into the image.
5. **Submit coordinator**: `submit --fg-labs-sha <sha> --target smoke|all`.
   The coordinator is itself a Batch job (queue `bwa-mem3-bench-coordinator`,
   instance type `c6a.large`/`c6a.xlarge`); it runs snakemake and submits
   per-rule worker jobs to arch-specific queues.
6. **Watch**: `watch` (live), or poll via `aws jobs` / `aws logs <id>`.
7. **Collect + ingest**: `collect --fg-labs-sha <sha>` pulls `runs/<sha>/`
   from S3 and populates `benchmark.db` (SQLite).
8. **Promote baseline/golden** (when blessing): `bless-baseline`, `bless-golden`.
9. **Report**: `bench summary`, `bench regression`, `bench full-report`.

## AWS ops quick reference

- `aws jobs` — active coordinator + worker jobs.
- `aws describe <job-id>` — details for one job.
- `aws logs <job-id>` — CloudWatch logs for the coordinator or any worker.
- `aws kill <job-id>` / `aws kill-all` — terminate; `kill-all` also terminates
  child workers submitted by a running coordinator.
- `aws cost` — approximate spot spend per run.
- `aws cleanup` — deregister orphaned `snakejob-def-*` job definitions.

## Queue routing

- Each arch has a dedicated Batch queue `bwa-mem3-bench-<arch>` wired to a
  compute environment with `instance_type` from `config/archs.yaml`.
- Rules pick their queue via `resources.batch_queue = CONFIG.archs[arch].batch_queue`.
- Our fork of `snakemake-executor-plugin-aws-batch` (see below) reads that
  resource. Upstream ignores it and sends every job to the profile default.

## Arch-specific baseline support

- `BASELINE_ARCHS` in `workflow/Snakefile` = archs with `platform: linux/amd64`.
- Upstream `bwa-mem2 v2.2.1` has no ARM SIMD, so `align_baseline` and
  `compare_vs_baseline` only run on x86 archs. Arm archs run fg-labs only.
- The `smoke` rule is pinned to `c6a` (cheapest x86) because it needs baseline.

## Docker image caveats

- `--provenance=false` on `docker buildx build` — OCI attestations confuse the
  ECS agent on Graviton and it ends up pulling the wrong-arch variant.
- **No `ENTRYPOINT`** — snakemake-registered worker jobs run
  `/bin/bash -c "..."`; an `ENTRYPOINT ["/bin/bash"]` would make that
  `/bin/bash /bin/bash -c ...` and bash can't exec itself as a script.
- `python` → `python3` symlink required; Debian only ships `python3`.
- `snakemake-storage-plugin-s3==0.3.1` is the last PyPI version whose
  metadata is compatible with snakemake 8.x. Newer requires
  `snakemake-interface-storage-plugins>=4.2.3` which needs snakemake 9.
- `aws-batch-task-timeout: 7200` in the profile — plugin default is 300 s
  which kills real alignments.

## Our `snakemake-executor-plugin-aws-batch` fork

Lives at <https://github.com/nh13/snakemake-executor-plugin-aws-batch>.
Pinned in `docker/Dockerfile` and `pixi.toml` by SHA. Carries four
non-upstream changes:
1. `resources.batch_queue` per-rule queue override.
2. `auto_deploy_default_storage_provider=False` — we install the storage
   plugin in the image instead of on every worker.
3. `resources.shared_memory_size_mb` → `linuxParameters.sharedMemorySize`
   on the worker job definition. Needed for `bwa-mem2 shm` to stage the
   in-memory index in /dev/shm (default 64 MB is too small).
4. `resources.container_image` per-rule image override → SubmitJob's
   `containerProperties.image`. Used by per-arch image selection (see
   next section); falls back to the profile's `container-image` when
   unset.
Bump the pin when we add more fixes.

## Per-rule (per-arch) image selection

Each Batch worker job can pull a different ECR image, derived per-arch
from `arch.baseline_arch` in `config/archs.yaml`:

- `Arch.image_uri(ecr_repo_uri, fg_labs_sha)` returns `<ECR>:<sha>` when
  `baseline_arch == ""` (portable manifest list) or
  `<ECR>:<sha>-<baseline_arch>` otherwise (host-locked variant).
- `workflow/Snakefile` exposes a thin `image_for_arch(arch_name)` helper
  bound to `FG_LABS_SHA` + `aws_config.load().ecr_repo_uri`.
- `workflow/rules/{align,compare}.smk` rules set
  `resources.container_image = lambda wc: image_for_arch(wc.arch)`.
- The plumbing is wired and tested but **every arch is currently parked
  at `baseline_arch=""`** — empirical data on this workload shows the
  fg-labs/bwa-mem3 `BASELINE_ARCH=avx512bw` build is consistently
  slower on Zen 4 (c7a +12-17%) and only mixed/wash on Sapphire Rapids
  (c7i / m7i), against PR #84's claimed +10-15% gain. See
  `~/work/git/bwa-mem3/avx512-baseline-build/PHASE_C_REPORT.md` for the
  full numbers + likely root causes.
- Build side: `cli build --baseline-arch <tier>` produces the matching
  ECR tag (`<sha>-<tier>`). When upstream lands an AVX-512 fix, flip
  the relevant arch's `baseline_arch` field, build that variant via
  `cli build --baseline-arch avx512bw --push`, re-submit. No further
  workflow / plugin changes needed.

## Data locations

- See `docs/data-setup.md` for how to obtain the reference genome and
  benchmark FASTQs and stage them in your own S3 bucket.
- Local S3 mirror (for offline reproducers):
  `sync-local [--what references,data|all]` mirrors the configured S3 bucket
  to the directory at `bwa_mem3_bench.LOCAL_MIRROR_ROOT` (override via the
  `BWA_MEM3_BENCH_LOCAL_MIRROR` env var).

## Gotchas learned the hard way

- **`docker buildx`'s empty `ARG` is set-but-empty, not unset, and that
  silently breaks Makefile `?=` defaults.** Pre-PR #6 the Dockerfile passed
  `BASELINE_ARCH="$BASELINE_ARCH"` to `make` only when non-empty and
  expected upstream's `BASELINE_ARCH ?= avx2` to fire otherwise. It
  didn't — BuildKit exports `ARG BASELINE_ARCH=""` to the RUN shell as
  the env var `BASELINE_ARCH=` (set-empty), GNU make's `?=` only fires
  when unset (not when set-to-empty), so `arch=$(BASELINE_ARCH)`
  resolved to `arch=` and the build fell through every Makefile branch
  to the file-level `-msse … -msse4.1` fallback. Every bench x86 image
  shipped sse41-baselined non-kernel TUs on every AVX2-capable host.
  Per fg-labs/bwa-mem3 PR #95's multi-arch-deployment doc this costs
  10-15 % wall on AVX2 hosts; the bench's empirical multi-rep numbers
  showed -17 to -30 % wall and -22 to -36 % `process_seconds` once
  fixed (PR #6, c.f. `runs/dc7fcfe…/regression.md` vs `runs/ec67b09…`).
  Fix: always pass an explicit non-empty value, e.g.
  `make BASELINE_ARCH="${BASELINE_ARCH:-avx2}"`. The runtime banner
  added in fg-labs/bwa-mem3 PR #95 (`bwa-mem3 version` → `SIMD floor:
  <X> / SIMD runtime: <Y>`) makes this trivially diagnosable going
  forward; grep CloudWatch / `docker run … bwa-mem3 version` and
  compare the two lines. ARM is unaffected — `arch=arm64` is a separate
  Makefile path that doesn't use `BASELINE_ARCH`.
- **A "regression" between two bench SHAs may be a bench-substrate
  artifact, not a codegen change.** fg-labs/bwa-mem3#92 hypothesised
  that PR #88's `always_inline` on `FMI_search::backwardExt` regressed
  Zen 3 by +14–19 % on wgs-5M. Phase 0 reproduction with the
  byte-identical binary extracted from the bench's own ECR image,
  run on bare-metal `c6a.4xlarge` with cold I/O, showed flat wall and
  flat CPU between the suspect pair of SHAs — the regression doesn't
  exist outside AWS Batch's spot substrate. Before opening a perf
  issue against fg-labs/bwa-mem3, repro on a bare-metal EC2 host with
  the exact ECR-extracted binary (see "AWS / EC2 Investigation Hosts"
  in user CLAUDE.md). If it doesn't repro there, the bug is in our
  measurement, not in the binary.
- **Never submit a Batch coordinator before the ECR push has fully settled.**
  Workers pull `:latest` at container start and cache it per host; if ECR's
  tag pointer update is still propagating, they'll pull the previous image
  and fail with something that looks unrelated to the build. Verify with
  `aws ecr describe-images --repository-name bwa-mem3-bench
  --image-ids imageTag=latest` and match the `imageDigest` to the digest
  printed at the end of the `docker buildx ... --push` output **before**
  running `submit`. A "background build completed exit 0" notification is
  not a push-settled signal.
- **ECS `:latest` caching has no staleness protection.** Once a worker host
  has pulled `:latest`, it won't re-pull unless the image pull policy is
  explicitly `Always`. If you push a new `:latest` and reuse a warm host,
  it runs the old image. Either terminate Batch EC2 instances between
  builds, or give each build a unique tag and reference that tag in the
  rendered `workflow/profiles/aws-batch/config.yaml`.
- **`rule all` fails the coordinator even with `keep-going: true`** when any
  one sample's outputs can't be produced. Non-failing samples still
  complete successfully and their outputs land in S3 — always grep
  `runs/<sha>/` in S3 before concluding a "FAILED" coordinator was a
  total loss. The FAILED status just means rule all's aggregate input
  set was unsatisfiable at the end.
- **compare-bams does NOT require name-sorted BAMs — only matching record
  order.** `bwa-mem2` emits records in FASTQ input order, so two runs
  over the same FASTQ produce records in the same order. The align rules
  use `samtools view -b` (no sort) and compare-bams walks both streams
  in lockstep. The old `name_sort` rule was pure waste (~15-25% of
  per-worker wall). See `tools/compare-bams/src/pair_reader.rs`.
- **`bwa-mem2 index --meth` peaks at ~130 GB RSS / 148 GB virt** on the
  human genome. The doubled C→T/G→A reference (~6.4 GB) drives the FMI
  build memory up. 64 GB and 128 GB hosts both OOM; budget for a 256 GB
  instance (~30 min build). The upstream README's "~10 GB for human
  genome" is the **alignment** memory, not the **index build** peak.
- **bwameth.py invokes literal `bwa-mem2`** from PATH, and the bwa-mem2
  dispatcher then invokes `bwa-mem2.{avx512bw,avx2,avx,sse41}` (no
  `.upstream` suffix). Our Docker image installs the suffixed variants
  only; the Dockerfile creates symlinks `bwa-mem2`, `bwa-mem2.avx2`,
  `bwa-mem2.avx512bw`, etc. → their `.upstream.*` counterparts so both
  the bwameth.py shell-out and the dispatcher's stat() find them.
- **Meth alignment needs `mem_mb >= 48000` and host RAM >= 64 GB.** Loading
  the 20 GB doubled `.bwameth.c2t.bwt.2bit.64` FMI plus the 6 GB packed
  reference into bwa-mem2 pushes peak RSS past 30 GB before mapping even
  starts. Meth samples are pinned to a high-memory arch via
  `_archs_for_sample()` in `workflow/Snakefile`; `_mem_mb_for()` sets the
  Batch cgroup to 48000.
- **Wait for all jobs to reach a terminal state (SUCCEEDED/FAILED) before
  deleting S3 outputs.** `aws batch terminate-job` (and `aws kill-all`)
  send SIGTERM to the container — it does NOT synchronously kill the task.
  A RUNNING meth alignment is a 20-30 min job; once it gets far enough to
  be producing output, it will continue uploading after you've hit kill.
  If you wipe `runs/<sha>/` while a doomed worker is mid-upload, the worker
  will (moments later) write `aligned.bam` into the cleaned prefix, and
  the next coordinator will see that file and skip re-alignment — silently
  serving stale output from a killed run. Before any `aws s3 rm`, poll
  `aws batch list-jobs --job-status {SUBMITTED,PENDING,RUNNABLE,STARTING,RUNNING}`
  across every project queue and wait for zero.
- **`aws kill-all` only kills jobs in queues listed in `bwa_mem3_bench.aws_config`.**
  The `archs` tuple there must stay in sync with the `ARCHS` tuple in
  `cdk/stacks/batch_stack.py`. If you add or rename an arch queue in CDK,
  update both; otherwise `kill-all` will silently skip the new queue.
- **Count pairs from the FASTQ, not from `samtools view -c -f 64` on an
  aligned BAM.** The aligned BAM's first-of-pair count is inflated by
  supplementary alignments. `twist-umi.aligned.bam` shows 31.1M but the
  source twist-umi_1.fastq.gz is actually 7.9M pairs.
- **Spot capacity varies per AZ.** `InsufficientInstanceCapacity` on
  `ec2 run-instances` → iterate subnets/AZs in your VPC rather than
  retrying in the same AZ.
- **`aws batch list-jobs --query 'jobSummaryList | sort_by(@, &stoppedAt)'`
  fails with "invalid type: None"** when any listed job has a null
  `stoppedAt` (e.g., still running). Filter with `[?stoppedAt!=`null`]`
  before sorting, or skip the sort.
- **Sapphire Rapids (c7i, m7i) spot pools are ~10-15× noisier than other
  archs.** Across 56 (run × sample) cells with 3+ reps each, median
  wall-time CV is c6a 1.4%, c7a 0.8%, c7g 0.8%, c8g 0.9%, c7i 11.1%,
  m7i 11.5%. Within the same coordinator run, m7i can swing ±20-40%
  rep-to-rep on the same input; c7i swings ±10-15%. Implication: if
  you need confident m7i / c7i numbers (e.g. cross-SHA regression
  detection), bump `--reps` to 5 or higher; 3 reps masks 11% CV with
  too few samples. AMD (c6a, c7a) and Graviton (c7g, c8g) at 3 reps
  is fine. Likely cause: AWS spot host quality varies more in those
  pools, plus m7i specifically shows lower mean_load (840 vs 1119-1293
  elsewhere), suggesting it also has worse multi-tenant noise.

## Known issues

- fg-labs bwa-mem3 @ `690914f`: `mem_reg2aln` assertion on `avx512bw` variant
  (`src/bwamem.cpp:1795`). Affects c7a/c7i; c6a/c7g/c8g are fine. Tracked at
  fg-labs/bwa-mem3#25.
- **c6a / AVX2 MAPQ regression**: 4 of 64,763 reads in smoke-1M differ in
  MAPQ between fg-labs (AVX2) and upstream v2.2.1 — same position, same
  CIGAR. Pre-dates PR #26; reproduces on `690914f` too. c7i/c7a/c6a with
  AVX-512 show 100% concordance. Only PR #17 (`fix(mem-sam-pe):` proper-pair
  flag) is known to change alignments, and it changes FLAG not MAPQ.

## Running checks

```bash
pixi run check            # all: rust fmt/clippy/test + python fmt/lint/type/test
pixi run cargo-test       # rust only
pixi run py-test          # python only
```
