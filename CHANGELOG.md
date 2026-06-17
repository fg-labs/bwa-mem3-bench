# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Per-arch Docker image selection. Each Batch worker can now pull a
  different ECR image based on its arch's `baseline_arch` field in
  `config/archs.yaml`, derived via `Arch.image_uri(...)` on the
  workflow-config dataclass and threaded through a new
  `resources.container_image` per-rule resource. Falls back to the
  profile-level `container-image` when unset, so existing single-image
  workflows are unaffected. `cli build --baseline-arch <tier>`
  produces matching tier-suffixed ECR tags (e.g. `<sha>-avx512bw`).
  Plumbing is parked at `baseline_arch=""` for every arch by default
  (see "Changed" below for rationale); flipping any arch on is a
  one-line config change plus one `cli build --baseline-arch` invocation.
- `bench summary` and `bench speedup` now expose `process_seconds`
  (kernel-only `PROCESS()` time from bwa stderr) and
  `index_read_seconds` alongside wall-time, with `compute_speedup` as
  the headline metric so smoke-1M-style index-load-dominated
  comparisons no longer inflate the speedup tables.
- `cli build --baseline-arch <tier>` build flag for per-tier image
  variants on x86 (`avx2`, `avx512bw`, etc.). Forwards `BASELINE_ARCH`
  to `make` in the fg-labs builder stage; refuses to also-tag-`:latest`
  to prevent host-locked variants from clobbering the portable tag.

### Changed

- Snakemake AWS Batch executor plugin pin bumped to a fork commit
  (`727782c`) that adds `resources.container_image` override support.
- ECR lifecycle policy split into two rules: rule 1 expires untagged
  manifests after 7 days (per-platform sub-manifests of multi-arch
  pushes, ~2 per release); rule 2 keeps the last 30 tagged images
  (~30 fg-labs SHAs of history including tier-suffixed variants).
  Replaces the prior single "max 50 (any)" rule that conflated tagged
  and untagged retention budgets.
- Adapted to fg-labs/bwa-mem3 PR #83 (single-binary in-process SIMD
  dispatch): Dockerfile now installs the single `bwa-mem3` binary at
  `/usr/local/bin/bwa-mem2.fg-labs`; the per-SIMD install loop and the
  `make multi` invocation are gone. Upstream `bwa-mem2 v2.2.1` build
  stage is unchanged.
- `config/archs.yaml`: every arch parked at `baseline_arch=""` (use
  the portable `:<sha>` tag) pending an upstream fix for the
  `BASELINE_ARCH=avx512bw` perf regression on Zen 4 (c7a +12-17%
  slower across all 5M-pair samples than the AVX2 default; c7i / m7i
  mixed/wash). PR #84's claimed +10-15% gain doesn't materialize on
  this workload — see `docs/superpowers/specs/2026-05-07-per-rule-image-design.md`
  and the fg-labs/bwa-mem3 AVX-512 baseline-build Phase C benchmarking.
- `samtools` bumped 1.22.1 → 1.23.1 (new ref stats, `--min-depth`,
  UMI support in `samtools fastq`/`import`, fixes); `tricord` bumped
  0.1.0 → 0.1.2 (per-tick TSV trace via `--trace <PATH>`).
- Renamed `fg-labs/bwa-mem2` → `fg-labs/bwa-mem3` references throughout
  (the upstream repo was renamed on GitHub; redirects still work, but
  the stale name in build scripts and docs was misleading).

### Fixed

- AVX-512BW assertion crash on `c7a` / `c7i` was an upstream bug
  (`mem_reg2aln` on the `avx512bw` per-tier binary at fg-labs/bwa-mem3
  `690914f`); resolved upstream by the single-binary refactor (PR #83)
  and the matesw shm-ref scratch fix (PR #85, `316dba62`). Bench-side
  this is a no-op — just bumping the SHA we benchmark.
- NEON `panel-twist-5M c8g` 0% concordance + ~5%-CPU-efficiency slowdown
  observed on `b9e0b66` was an upstream bug (`mem_matesw_batch_post`
  writing through the read-only shm-attached reference slice);
  resolved upstream by PR #85. Validated on `316dba62` with 100%
  concordance and ~1500 mean_load on c8g across all 3 reps.

## [0.1.0] - 2026-04-29

Initial public release.

### Added

- End-to-end Snakemake workflow comparing **bwa-mem3** (developed in
  [fg-labs/bwa-mem3](https://github.com/fg-labs/bwa-mem3)) against upstream
  `bwa-mem2 v2.2.1` across WGS, WES, panel, and methylation samples on hg38.
- AWS infrastructure as CDK (S3, ECR, IAM, six Batch compute environments
  + queues for `c6a` / `c7a` / `c7g` / `c7i` / `c8g` / `m7i` plus a
  coordinator queue).
- `bwa_mem3_bench` Python CLI (`build`, `submit`, `collect`, `bench`,
  `aws`, `upload-data`, `sync-local`, `render-profile`, `bless-baseline`,
  `bless-golden`, `watch`).
- `compare-bams` Rust crate — compares two BAMs in lockstep at
  position / CIGAR / MAPQ level without requiring name sorting.
- Multi-arch (`linux/amd64` + `linux/arm64`) Docker image with bwa-mem2
  (upstream + fg-labs builds), samtools, bwameth, and the project CLI.
- SQLite-backed result ingestion (`benchmark.db`) and reporting
  (`bench summary`, `bench regression`, `bench full-report`,
  `bench speedup`, `bench trend`).
- AWS Batch profile templating: `pixi run render-profile` reads
  `cdk/outputs.json` (or env vars) and produces the Snakemake profile so
  no account-specific values are committed.
- Optional CDK cost-center tag, opt-in via
  `cdk deploy -c cost_center="..."`.
- `docs/data-setup.md` with verified URLs for the Broad hg38 reference
  bundle and the 1000 Genomes HG00096 / HG00100 alignment files.

[Unreleased]: https://github.com/fg-labs/bwa-mem3-bench/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fg-labs/bwa-mem3-bench/releases/tag/v0.1.0
