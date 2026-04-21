# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-29

Initial public release.

### Added

- End-to-end Snakemake workflow comparing **bwa-mem3** (developed in
  [fg-labs/bwa-mem2](https://github.com/fg-labs/bwa-mem2)) against upstream
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
