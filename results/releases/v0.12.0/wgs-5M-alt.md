# wgs-5M-alt: Whole genome, ALT-aware (HG00096)

bwa-mem3 v0.12.0. [All datasets](README.md) · [methodology](../../methodology.md)

The wgs-5M reads aligned with the reference's `.alt` file present.

4,990,436 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Measured at one rep per architecture by design; timings are indicative only.

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

_This dataset has one rep per cell, so there is no spread (CV) to show._

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 102.50 | — | — | 104.45 | 207.37 | 2.02x | 1 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 62.17 | — | — | 65.04 | 165.35 | 2.66x | 1 |
| c7g (AWS Graviton3, NEON) | 92.54 | — | — | 86.43 | — | — | 1 |
| c7i (Intel Sapphire Rapids, AVX-512) | 101.70 | — | — | 102.57 | 202.93 | 2.00x | 1 |
| c8g (AWS Graviton4, NEON) | 71.13 | — | — | 70.44 | — | — | 1 |
| m7i (Intel Sapphire Rapids, AVX-512) | 82.95 | — | — | 82.23 | 170.00 | 2.05x | 1 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 14.6 | — | 21.2 | 96% | 101.07 |
| c7a | 14.9 | — | 21.2 | 93% | 59.71 |
| c7g | 14.6 | — | — | 95% | 90.67 |
| c7i | 14.7 | — | 21.3 | 96% | 100.20 |
| c8g | 14.6 | — | — | 94% | 68.56 |
| m7i | 14.7 | — | 21.4 | 96% | 81.49 |

## Agreement

Concordance is the percentage of primary reads whose alignment record matches between the two runs. Where it is below 99.99% the table breaks the differences down by class; a read counts once under each class of difference it has. See [methodology](../../methodology.md#agreement) for which tags each comparison ignores.

### vs bwa-mem2 v2.2.1

x86 instances only (bwa-mem2 v2.2.1 has no Arm build). Tags bwa-mem3 adds or computes differently by design are ignored.

| instances | concordance | cells |
| --- | --- | --- |
| c6a, c7a, c7i, m7i | 100.0000% | 4 |

### `--compat` vs bwa-mem2 v2.2.1

`--compat=bwa-mem2` promises output identical to bwa-mem2, with nothing ignored.

_Not measured for this release._

### vs the previous release (v0.11.0)

Every tag compared. Differences here are intentional changes in this release, described in its release notes.

_Not measured for this release._

### Arm vs x86

The same release on Graviton compared with its x86 output.

| instances | concordance | cells |
| --- | --- | --- |
| c7g, c8g | 100.0000% | 2 |

### `--fast` vs the default preset

`--fast` deliberately prunes the candidate alignments it considers, so it is not expected to match the default preset. bwa-mem3's documentation reports that about 85% of the reads it re-places had MAPQ 0 (multi-mapping); the [accuracy page](accuracy.md) measures the effect against simulated truth.

_Not measured for this release._
