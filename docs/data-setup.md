# Data setup

This benchmark needs three kinds of inputs staged in your S3 bucket before
you can run the AWS Batch workflow:

1. The hg38 reference genome (FASTA + bwa-mem2 indexes), under
   `s3://<your-bucket>/references/`.
2. Paired-end FASTQs for the benchmark samples, under
   `s3://<your-bucket>/data/`.
3. (Optional, for methylation) the bwameth doubled-strand index of the same
   reference, under `s3://<your-bucket>/references/hg38-meth/`.

All inputs are derived from public datasets — none of the source data is
included in this repository. This document explains where to get each piece
and how to upload it.

---

## 1. Reference genome (hg38)

We use the Broad Institute's `Homo_sapiens_assembly38.fasta` (the GRCh38
analysis set with decoy contigs and HLA alts), the same reference used by
GATK Best Practices. It is publicly hosted on both Google Cloud Storage and
AWS S3; download from whichever is closer to your compute.

### Download

```bash
mkdir -p ~/refs/Homo_sapiens_assembly38 && cd ~/refs/Homo_sapiens_assembly38

# Pick one bucket. AWS S3 is the same files, served from us-east-1.
BASE=https://storage.googleapis.com/gcp-public-data--broad-references/hg38/v0
# BASE=https://broad-references.s3.amazonaws.com/hg38/v0

# FASTA (~3 GB)
curl -O "$BASE/Homo_sapiens_assembly38.fasta"

# Index files
curl -O "$BASE/Homo_sapiens_assembly38.fasta.fai"
curl -O "$BASE/Homo_sapiens_assembly38.dict"
```

Both buckets are listed in the GATK [Resource Bundle docs][gatk-bundle]
(scroll to the "Bucket details" section); the file layout is identical and
files are byte-for-byte equal between mirrors.

[gatk-bundle]: https://gatk.broadinstitute.org/hc/en-us/articles/360035890811-Resource-bundle

### Build bwa-mem2 indexes

```bash
# Standard (DNA) index — ~1 hour, ~30 GB RAM
bwa-mem2 index Homo_sapiens_assembly38.fasta

# Methylation (doubled-strand) index — only needed for the meth samples.
# Peaks at ~150 GB RAM during FMI construction; budget for a 256 GB host
# (e.g. r7i.8xlarge), ~30 minutes.
bwa-mem2 index --meth Homo_sapiens_assembly38.fasta
```

The DNA index produces six files alongside the FASTA: `.0123`, `.amb`,
`.ann`, `.bwt.2bit.64`, `.pac`, plus the existing `.fai` and `.dict`. The
meth index adds `.bwameth.c2t*` variants.

### Upload to S3

```bash
REF_ROOT=~/refs/Homo_sapiens_assembly38 \
    bash scripts/upload_reference.sh <your-bucket> hg38

# (optional) meth index
REF_ROOT=~/refs/Homo_sapiens_assembly38 \
    bash scripts/upload_reference.sh <your-bucket> hg38-meth
```

---

## 2. Benchmark FASTQs

The four production samples in `config/samples.yaml`:

| Sample              | Source                                                       | Pairs |
| ------------------- | ------------------------------------------------------------ | ----- |
| `wgs-5M`            | 1000 Genomes WGS HG00096 (downsampled)                       | 5M    |
| `wes-5M`            | 1000 Genomes WES HG00100 (downsampled)                       | 5M    |
| `panel-twist-5M`    | Human Twist-UMI panel (SRR37186773, MCF10A) — public SRA     | 5M    |
| `meth-twist-emseq-5M` | Twist EM-seq dataset, provided by Twist                    | 5M    |

The two smoke samples (`smoke-1M` and `smoke-meth`) are heavy downsamples
of the panel-twist and meth-twist-emseq sources respectively.

### 1000 Genomes (HG00096 WGS, HG00100 WES)

Both samples are part of the [International Genome Sample Resource
(IGSR)][igsr] hosted at EBI. Direct download URLs for the artifacts we use:

[igsr]: https://www.internationalgenome.org/

| Sample / data type | URL | Size |
| ------------------ | --- | ---- |
| HG00096 30x WGS CRAM (NYGC, GRCh38) | <https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram> | 15.7 GB |
| HG00100 phase-3 exome BAM (Illumina, GRCh37→GRCh38 lifted, "mapped" subset) | <https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/phase3/data/HG00100/exome_alignment/HG00100.mapped.ILLUMINA.bwa.GBR.exome.20121211.bam> | 15.1 GB |

Both URLs were resolvable as of last verification (HTTP 200, accept-ranges,
served from `ftp.sra.ebi.ac.uk` / `ftp.1000genomes.ebi.ac.uk`). If the EBI
file layout changes, the canonical lookup is the
[IGSR sample portal](https://www.internationalgenome.org/data-portal/sample/HG00096):
filter by data collection and pick the alignment file for the corresponding
study.

```bash
mkdir -p ~/data-stage && cd ~/data-stage

curl -O https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram
curl -O https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/phase3/data/HG00100/exome_alignment/HG00100.mapped.ILLUMINA.bwa.GBR.exome.20121211.bam

# WGS — extract paired FASTQs (CRAM needs the matching reference)
samtools fastq \
    --reference ~/refs/Homo_sapiens_assembly38/Homo_sapiens_assembly38.fasta \
    -1 wgs-5M_1.fastq.gz -2 wgs-5M_2.fastq.gz \
    -0 /dev/null -s /dev/null -n \
    HG00096.final.cram

# WES — same, BAM does not need --reference
samtools fastq \
    -1 wes-5M_1.fastq.gz -2 wes-5M_2.fastq.gz \
    -0 /dev/null -s /dev/null -n \
    HG00100.mapped.ILLUMINA.bwa.GBR.exome.20121211.bam
```

`upload-data` will further downsample to 5M pairs (every-Nth-pair via
`mawk`) when staging to S3.

### Twist samples

**`twist-umi`** — a human Twist UMI Adapter System capture panel. Public SRA
run **`SRR37186773`** (BioProject `PRJNA1422021`, MCF10A cell line, *Homo
sapiens*), 2×151 targeted-capture, read structure `5M2S+T`. The full run is
~168M pairs; it is family-downsampled (UMI-family-aware, via the public
`fgumi downsample`) to ~8M pairs, which `upload-data` then decimates to the
benchmark's ~4M pairs.

> **Provenance note.** This replaced a *mislabeled* `twist-umi`: the previous
> `SRR34589119` is *Felis catus* (a domestic-cat liver-tumour sample) that
> merely used the Twist UMI kit. Aligned to hg38 it mapped only 0.79%
> on-target / 17% unmapped / 47% MAPQ-0, so its "panel" behaviour was
> cross-species misalignment, not panel biology. When choosing any vendor
> sample, verify the **organism / taxon id** — not just the library kit.

**`twist-emseq`** (enzymatic methylation sequencing) is a Twist Bioscience
example QC dataset, provided to us by Twist and not redistributed here. To
obtain an equivalent, contact Twist and request the example QC dataset for the
NGS Methylation Detection Kit (EM-seq workflow).

If you cannot obtain the exact files, substitute any **human** paired-end panel
FASTQ pair — the benchmark measures relative throughput between builds on a
fixed sample, but the sample must be genuinely human and on-target to represent
the panel workload (see the cat mislabel above).

### `hic-1M` — HG002 Hi-C (Zenodo)

1M Hi-C read pairs (2×151 bp, paired-end), HG002.

- **Source:** Zenodo record <https://zenodo.org/records/19703025>, DOI `10.5281/zenodo.19703025` (CC BY 4.0, Heng Li). File pair `HG002.HiC-1M_{1,2}.fq.gz`.
- **Local root:** download the two files, then set `BWA_MEM3_BENCH_ZENODO_ROOT` to the directory holding them (defaults to `./zenodo-fastqs` under the repo root).
- **Staging:** already 1M pairs — no downsample. `upload-data --what hic-1M` uploads them verbatim to `data/hic/hg002-1M/{r1,r2}.fq.gz`.

### `sbx-1M` — HG002 Roche SBX (single-end)

~1M single-end Roche SBX (Sequencing by eXpansion) reads, 50–974 bp (median ~224), HG002.

- **Source:** Roche SBX (Axelios "xoos") GIAB demonstration data — dataset "091025 Webinar GIAB BAMs BWA Non Downsampled" ("Genomic dataset containing 091025-Webinar-GIAB-BAMs-BWA-Non-Downsampled data"), from <https://roche-axelios.gitbook.io/xoos/tutorials/measuring-error-rate-for-sbx-duplex-data>. **License: CC BY-NC 4.0 (Attribution-NonCommercial), Roche** — note this is *non-commercial* use only, unlike the CC BY 4.0 Hi-C data above.
- **Local root:** download the HG002 SBX BAM, then set `BWA_MEM3_BENCH_SBX_ROOT` to the directory holding the SBX BAM tree (defaults to `./sbx-bams` under the repo root); the sample reads `<SBX_ROOT>/2026/HG002.bam`.
- **Staging:** `upload-data --what sbx-1M` subsamples primary reads genome-wide and converts to a single FASTQ:

  ```sh
  samtools view -h -F 0x900 -s 42.001166 <SBX_ROOT>/2026/HG002.bam \
    | samtools fastq -0 sbx-1M.fq.gz -
  ```

  `-F 0x900` keeps primary reads only (secondary + supplementary dropped); **duplicates (0x400) are intentionally kept** as real reads to align. The fraction `0.001166 = 1,000,000 / ~858M primary` (874.2M idxstats records minus ~1.9% supplementary). Uploaded to `data/sbx/hg002-1M/r1.fq.gz`.
- **Recompute the fraction** if the source BAM changes: `frac = 1_000_000 / $(samtools view -c -F 0x900 <bam>)`.

### Stage and upload

`bwa_mem3_bench.cli upload-data` deterministically downsamples the source
FASTQs (every-Nth-pair via `mawk`) and uploads to
`s3://<your-bucket>/data/<sample>/`. Source files are read from the
`BWA_MEM3_BENCH_VENDOR_ROOT` directory (defaults to `./vendor-fastqs`).

Place the source FASTQs into `BWA_MEM3_BENCH_VENDOR_ROOT` with these names:

| Filename                | Contents                            |
| ----------------------- | ----------------------------------- |
| `twist-umi_1.fastq.gz`  | Twist UMI panel R1                  |
| `twist-umi_2.fastq.gz`  | Twist UMI panel R2                  |
| `twist-emseq_1.fastq.gz`| Twist EM-seq R1                     |
| `twist-emseq_2.fastq.gz`| Twist EM-seq R2                     |

For the WGS / WES samples, place the converted FASTQs as
`BWA_MEM3_BENCH_STAGE_ROOT/wgs-5M_{1,2}.fastq.gz` and
`BWA_MEM3_BENCH_STAGE_ROOT/wes-5M_{1,2}.fastq.gz` (`STAGE_ROOT` defaults
to `./data-stage`).

Then upload everything:

```bash
BWA_MEM3_BENCH_VENDOR_ROOT=/path/to/vendor-fastqs \
BWA_MEM3_BENCH_STAGE_ROOT=/path/to/data-stage \
    pixi run python -m bwa_mem3_bench.cli upload-data --what data
```

Or upload one sample at a time by passing the sample name as `--what`
(e.g. `--what smoke-1M`).

---

## 3. Simulated truth datasets (holodeck)

The truth-based accuracy benchmark (`--target accuracy` / `accuracy_smoke`)
runs on reads simulated by [fg-labs/holodeck](https://github.com/fg-labs/holodeck),
which emits each read's true placement, the variant it carries, and (for
methylation) per-CpG truth. These are scored by `holodeck eval` against the
aligners' BAMs — see `README.md` and `workflow/rules/eval.smk`.

One command generates all of them and (optionally) stages them to S3:

```bash
# holodeck must be on PATH — build fg-labs/holodeck at the
# docker/build-arg-defaults.env HOLODECK_REF pin, or extract the binary from the
# bench image. Then:
scripts/gen_all_sim_datasets.sh \
    ~/work/references/hg38/Homo_sapiens_assembly38.fasta \
    /tmp/sim 42 s3://<your-bucket>/data/sim
```

`scripts/gen_all_sim_datasets.sh` is the single source of truth for the dataset
matrix; it drives the per-dataset `scripts/gen_holodeck_dataset.sh`
(`mutate` → optional `methylate` → `simulate`, renaming outputs to canonical
bare names). Per-kind coverage is fixed inside that script.

| Dataset | Kind | Coverage | Target | Chemistry |
|---|---|---|---|---|
| `sim-wgs-place` | `wgs-place` | 0.5× (~5M pairs) | genome-wide | DNA |
| `sim-meth-place` | `meth-place` | 0.5× (~5M pairs) | genome-wide | EM-seq |
| `sim-wgs-vars` | `wgs-vars` | 30× | `scripts/sim-targets/chr22.bed` | DNA |
| `sim-meth-vars` | `meth-vars` | 30× | `scripts/sim-targets/chr22.bed` | EM-seq |

The `-place` datasets sweep the whole genome at low coverage to probe placement
and MAPQ calibration across hard/repetitive loci; the `-vars` datasets put depth
over a **target BED** (real targeted-seq style — never a reduced reference, so
off-target mismapping is still observable) to probe per-read variant
representation. The meth arms also emit `cpg-truth.bedGraph` for methylation-level
correlation. Each dataset directory holds exactly the files the workflow resolves
from a sample's `source` prefix:

```text
data/sim/<name>/
  r1.fq.gz  r2.fq.gz        # simulated reads (truth encoded in read names)
  golden.bam                # ground-truth alignment (eval --truth)
  truth.vcf                 # simulated SNVs (eval --variants)
  cpg-truth.bedGraph        # per-CpG methylation truth (meth kinds; eval --cpg-truth)
```

**Provenance / reproducibility.** Everything is seeded (default `--seed 42`), so
regenerating reproduces byte-identical inputs. Record the seed and the holodeck
SHA (the `HOLODECK_REF` in `docker/build-arg-defaults.env`) alongside any run.
The `-vars` target region is versioned at `scripts/sim-targets/chr22.bed`.

A chr22-slice smoke variant (`sim-smoke-*`, used by `accuracy_smoke` for fast
CI/harness validation) is generated the same way against the full reference but
with a small chr22 target BED (`scripts/sim-targets/smoke.bed`), so the inputs
stay tiny while off-target mismapping is still observable.

---

## 4. Adapt to your own samples

The set of samples is defined in `config/samples.yaml`. Each entry needs:

```yaml
my-sample:
  baseline_tool: bwa-mem2-upstream      # or bwameth (for methylation samples)
  reference: hg38                       # or hg38-meth
  source: data/my-org/my-sample/        # bucket-relative key prefix; the
                                        # workflow reads
                                        # s3://<bucket>/<source>r{1,2}.fq.gz
  fg_labs_flags: []                     # extra args passed to bwa-mem2.fg-labs
```

`source` must be a bucket-relative key prefix — the loader rejects values
that start with `s3://`, since the bucket comes from `defaults.yaml` /
`BWA_MEM3_BENCH_S3_BUCKET` / `cdk/outputs.json`.

Upload your `r1.fq.gz` / `r2.fq.gz` to the matching key prefix in your
bucket and add a `DataSource` entry to `bwa_mem3_bench/data_sources.py` if
you want to use `cli upload-data` for staging. Otherwise upload directly
with `aws s3 cp` and the workflow will pick them up.

---

## Local mirror for offline reproducers

`pixi run python -m bwa_mem3_bench.cli sync-local --what references,data`
mirrors the configured S3 prefixes into the local mirror directory
(default `./local-mirror/`, override via `BWA_MEM3_BENCH_LOCAL_MIRROR`).
Useful for running the workflow against an air-gapped or low-bandwidth
environment, and for the `scripts/local-smoke.sh` developer smoke test.
