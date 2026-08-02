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
- `tools/compare-bams/` — Rust crate that compares two BAMs in lockstep.
  Also ships `tag-census`, an offline instrument that reports which aux tags
  differ per read (see below)
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

## Aux-tag comparison

`compare-bams` compares placement (position / CIGAR / MAPQ / placement flags)
**and every aux tag**, treating each non-ignored tag difference as a first-class
discordance: a read counts as concordant only when no placement or non-ignored
tag difference remains. An ignored tag may differ freely without costing
concordance — the difference is still tallied under `by_tag`, so it stays
diagnosable. The exceptions are listed
per comparison kind in `config/samples.yaml` under `compare_defaults`, because
the tag policy is a property of the comparison rather than the sample:

| kind | compared against | skipped |
|---|---|---|
| `vs_baseline` | upstream bwa-mem2 (or bwameth, meth samples) | `MQ`, `HN` — plus, on meth, `NM` `MD` `XA` `SA` `XM` `XG` `XR` `YD` `YC` `RG` |
| `vs_golden` / `vs_x86` | bwa-mem3, same search settings | nothing — except `MQ`, `HN` on meth `vs_golden`, temporarily (below) |
| `vs_default` | bwa-mem3 `--fast` vs default | `XS` `HN` `XA` `SA` `MQ` |

Two rules generate those lists. **Cross-tool**: exclude any tag one side never
writes, or that *is* (or embeds) a reference-relative edit distance — bwameth
computes `NM`/`MD` against a C→T/G→A converted genome, and `XA`/`SA` carry both
that edit distance and doubled-reference contig names like `fchr1`.
**`--fast` vs default**: same binary, but not the same *behaviour* — the preset
prunes the candidate set on purpose, so the tags describing that set diverge
mechanically (`XS` 18.8%, `XA` 17.2%, `SA` 39.8%, `HN` 7.1% of reads on wgs-5M)
while carrying no placement information. Tags describing the *chosen* alignment
(`AS`, `MD`, `NM`, `MC`) stay strict everywhere, all under 1% divergent.

`vs_golden` and `vs_x86` skip nothing deliberately: they compare identical search
behaviour, so they are where a tag-only regression is detectable at essentially
zero cost — full strictness costs 0.0000 pp on `vs_x86` and at most 0.094 pp on
`vs_golden`.

The one exception is **temporary** and scoped to meth `vs_golden`.
fg-labs/bwa-mem3#304 makes `--meth` emit `MQ`/`HN` for the first time, so a build
carrying it puts a query that has both tags against a golden blessed from a build
that does not: 100% `query_only` on two tags, on every meth cell, against a Gate #2
that wants ≥ 99.999% — a hard failure produced by an upstream *fix*.
`METH_GOLDEN_TRANSITION_TAGS` (`workflow_config.py`) ignores the two for exactly
that span, and is **deleted** once the meth golden is re-blessed from a post-#304
build; leaving it would stop guarding two real tags on the one comparison meant to
be strict. `vs_baseline`'s `MQ`/`HN` are the opposite — permanent, because bwameth
emits neither and never will.

A skipped tag is still counted in the report's `by_tag` block with
`ignored: true`, so a wrong entry is diagnosable rather than silent.

### Guarding the policy

The lists above are load-bearing on the headline number, so `compare-bams` fails
the run — exit code **3**, distinct from I/O (1) and usage (2) errors — when the
tags it actually observes are not the ones the config anticipated:

- **Unexpected tag.** A tag on neither `expect_tags` nor `ignore_tags` is named
  in the error. Such a tag lands in the strict set and diverges on ~100% of
  reads, so the score already collapses; without the guard it collapses without
  saying which tag did it.
- **Dead ignore entry.** An `ignore_tags` entry matching no record on either side
  suppresses nothing, so no field in the report moves at all. This is the silent
  half, and it is the shape of the bug the whole tag policy came out of
  (bench #34): config that reads as though it filters tags while doing nothing.

`expect_tags` means a tag **may** appear, not that it must. A listed tag that
never shows up is a harmless no-op, which is what lets one per-kind list serve
samples whose tag sets legitimately differ. Two such differences are derived
rather than declared (`workflow_config.py`): single-end samples carry no mate
tags, and methylation samples add `XM`/`XG`/`XR`/`YD`/`YC`/`RG` while emitting no
`MQ`/`HN` on builds predating fg-labs/bwa-mem3#304 (which closed #296). Both are
excused from the dead-entry audit via `--absent-ok-tag`, which exempts an entry
from the *check* without un-ignoring it.

That exemption is a third thing, distinct from either `MQ`/`HN` ignore above: it
is emitted wherever the two are ignored on a meth sample — `vs_baseline`,
`vs_default`, and, for now, `vs_golden` — because `absent_ok_tags()` intersects
the *derived* ignore list rather than naming kinds. It is needed only while a
pre-#304 build is still benched —
an old-golden re-run or a bisect, where the entries genuinely match no record. On
a post-#304 build it is redundant rather than wrong, since an `--absent-ok-tag`
whose tag *does* appear is a no-op. So the three have three different lifetimes:
`vs_baseline`'s ignore is permanent, `vs_golden`'s ignore dies at the next meth
re-bless, and the audit exemption dies when no pre-#304 build is in rotation.

The report is written **before** the non-zero exit and carries the violations in
a `tag_guard_violations` block, so a failed run is still diagnosable from its own
JSON. `by_tag` now records `query_present` / `baseline_present` for every tag
observed — not only those that diverged — which is what the checks read.

`--expect-tag` is required unless `--no-tag-guard` is given. An empty allowlist
is indistinguishable from an unconfigured one, so rather than skip the check
silently the CLI makes the choice explicit — pass `--no-tag-guard` for
exploratory comparisons against an unfamiliar BAM pair, where the tag set is
what you are trying to find out.

**Coverage.** Sixteen samples — 33 (sample, kind) pairs — ever reach `compare-bams`:
`SWEEP_SAMPLES` (which excludes `truth` samples, so no `sim-*` dataset runs a
comparison at all), the six `FAST_REAL_BASES` siblings for `vs_default`, and the
hard-coded `fast_smoke` targets. The allowlist is the measured union over a
46-cell `tag-census` sweep covering every one of them except `smoke-1M-fast` and
`smoke-meth-fast`, which have never been run. Those two are covered by
measurement rather than assumption: across all six `vs_default` cells — paired,
single-end and meth — the fast arm and its default sibling emit an **identical**
tag vocabulary, so each derives from its measured default arm.

Statically, bwa-mem3's three writers can emit `AS HN MC MD MQ NM pa RG SA XA XG
XM XR XS`. Two of those — `pa` and non-meth `RG` — are deliberately excluded from
the allowlist so that their appearance fails the run. There is no production
constant for them precisely because nothing in production reads one; the
exclusion is a pinned decision, and
`test_known_but_unemitted_tags_are_deliberately_not_allowlisted` in
`tests/test_workflow_config.py` is what pins it.

### Measuring the policy (`tag-census`)

`tag-census` answers the prior question — *which tags would it be safe to
compare, and what would comparing them cost?* — so the lists above can be set
from data instead of guessed:

```bash
cargo build --release --bin tag-census
./target/release/tag-census --query <fg-labs.bam> --baseline <other.bam> \
    --out census.json --label "vs_baseline wgs-5M c6a"
```

It reuses `compare-bams`' template-grouped walk and classifier, so its
`core_concordance_pct` matches `compare-bams`' `concordance_pct` exactly on the
same pair given the same policy — pass the `--ignore-tag` / `--mapq-tolerance`
flags the corresponding `compare-bams` invocation passes (both default to the
workflow's current settings: no ignored tags, zero tolerance). The report is
policy-free: per-tag presence / value-difference counts, plus a histogram over the
*set* of tags differing on each read, split by whether that read was already
discordant on core fields. Because the histogram is over sets, the concordance
implied by any candidate strict-tag set is computable after the fact — it is the
count of core-concordant reads whose differing-tag set misses that candidate —
so evaluating a policy is arithmetic on the JSON, not a re-run.

Two divergence modes it distinguishes, which matter for choosing a policy:

- **Presence** — a tag one side never writes (`MQ`/`HN` vs upstream bwa-mem2).
  Systematic and all-or-nothing; comparing one strictly zeroes concordance.
- **Value** — both sides write it, values disagree. It can be sporadic (real signal)
  or near-universal: `NM`/`MD` differ on 99.7% of reads against bwameth, because
  bisulfite mode computes them against a differently-converted reference. A
  presence-only survey cannot see that — both sides emit them at equal counts.

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
