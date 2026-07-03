# bwa-mem3-bench

Benchmarking suite comparing **bwa-mem3** against upstream `bwa-mem2 v2.2.1`.
bwa-mem3 is the next-generation successor to
[bwa-mem2](https://github.com/bwa-mem2/bwa-mem2), in active development in
the [fg-labs/bwa-mem3](https://github.com/fg-labs/bwa-mem3) repository.
Runs on AWS spot across WGS, WES, panel, and methylation datasets on hg38,
on ARM Neon / x86 AVX2 / x86 AVX-512 instances.

## Quick start

```bash
pixi install -e dev
pixi run check
```

## Layout

- `bwa_mem3_bench/` — Python CLI + library (reports, storage, orchestration)
- `tools/compare-bams/` — Rust crate that compares two BAMs in lockstep
- `workflow/` — Snakemake pipeline
- `cdk/` — AWS infrastructure (S3, ECR, IAM, Batch)
- `docker/` — Multi-arch Dockerfile baking bwa-mem2 + samtools + bwameth
- `config/` — sample / arch / default YAML

## AWS quickstart

Prerequisites:
- An AWS account in `us-east-1` with permissions for Batch + spot + ECR + S3.
  Configure credentials via `AWS_PROFILE`, IAM role, or the standard AWS chain.
- Local Docker + buildx working for multi-arch pushes.
- Node.js + `npm install -g aws-cdk` for `cdk deploy` (synth via `pixi run cdk-synth` does NOT need Node).
- `pixi install` already run.
- Source data staged — see [`docs/data-setup.md`](docs/data-setup.md) for how
  to obtain the reference genome and benchmark FASTQs.

One-time setup:

```bash
pixi run cdk-synth                                            # verify CDK templates synth clean
(cd cdk && cdk deploy --all --require-approval never)         # provision S3, ECR, 5 Batch queues
pixi run render-profile                                       # render the AWS Batch Snakemake profile
REF_ROOT=/path/to/Homo_sapiens_assembly38 \
    pixi run python -m bwa_mem3_bench.cli upload-data --what references
pixi run python -m bwa_mem3_bench.cli upload-data --what data
```

Bless the upstream baseline (once per upstream tag, ~45 min, ~$10 spot):

```bash
pixi run python -m bwa_mem3_bench.cli bless-baseline --upstream-tag v2.2.1
```

Submit a benchmark run (fire-and-forget coordinator on AWS):

```bash
pixi run python -m bwa_mem3_bench.cli build --fg-labs-sha <sha> --push  # builds + pushes to ECR
pixi run python -m bwa_mem3_bench.cli submit --fg-labs-sha <sha>         # smoke by default
# or: --target all / --target baseline_all / --target bless_release (full release matrix)
```

The `submit` command fires a small coordinator Batch job (c6a.large spot) that runs
snakemake inside the Docker image. The coordinator in turn registers and submits
child Batch jobs for each alignment rule. The developer only needs `batch:SubmitJob`
— no `iam:PassRole` required on the developer's credentials.

Watch:

```bash
aws batch list-jobs --job-queue bwa-mem3-bench-coordinator --job-status RUNNING
aws logs tail /aws/batch/job --follow
```

Collect + report locally:

```bash
pixi run python -m bwa_mem3_bench.cli collect --fg-labs-sha <sha>
pixi run python -m bwa_mem3_bench.cli bench summary --fg-labs-sha <sha>
```

Every CLI subcommand accepts `--dry-run` to print the underlying command
without executing.

## Pre-AWS sanity check

`scripts/local-smoke.sh <fg-labs-sha>` runs the Snakemake DAG locally against
a pre-built native Docker image (arm64 on Mac). The `bwa-mem2.upstream` binary
is a shim on arm64, so both "query" and "baseline" actually use `bwa-mem2.fg-labs`
(self-concordance = 100% by construction). This validates rule wiring before
touching AWS; it does **not** exercise the real upstream-vs-fg-labs comparison
(that's what the AWS `smoke` target is for).

## Known limitations

- **Upstream bwa-mem2 v2.2.1 does not support ARM64.** The Docker image builds
  `bwa-mem2.upstream` only on `linux/amd64`; on `linux/arm64` that binary is a
  shim that errors out. fg-labs bwa-mem3 supports both architectures. The
  arm64 archs (c7g, c8g) therefore run fg-labs only — there is no
  upstream-vs-fork comparison on arm64. This is an ecosystem constraint, not
  a bug in this repo.

- **Batch `spot_fleet_role` is scheduled for deprecation.** The current CDK
  stack uses `SPOT_CAPACITY_OPTIMIZED` + `spot_fleet_role`; AWS is moving Batch
  to EC2 Fleet. Low urgency; revisit when the CDK lib surfaces the new mode.

- **`compare-bams --ignore-tag` is a no-op today.** The flag is plumbed through
  to `CompareOptions.ignore_tags` and the config supports per-sample tags, but
  `classify()` does not inspect aux tags at all. This will be addressed in a
  future release; until then, tag differences do not affect concordance.

- **`Pair::QueryOnly` / `Pair::BaselineOnly` report as `MappedOnly*`.** When a
  read name is absent from one BAM entirely (rather than present-but-unmapped),
  the current classifier reports it under the same `MappedOnly*` bucket. For the
  target use case (both BAMs produced from the same FASTQ) this path should never
  fire; if it does, the diagnosis may be misleading.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and
the PR workflow. Recent changes are tracked in [CHANGELOG.md](CHANGELOG.md).
File bugs and feature requests via the
[issue tracker](https://github.com/fg-labs/bwa-mem3-bench/issues/new/choose).

## License

Licensed under the [MIT License](LICENSE) © 2026 Fulcrum Genomics LLC.
