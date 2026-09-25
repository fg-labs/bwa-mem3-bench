# wgs-5M: Whole genome (HG00096)

bwa-mem3 v0.12.0. [All datasets](README.md) · [methodology](../../methodology.md)

1000 Genomes HG00096 30x WGS (NYGC, GRCh38), downsampled to 5M read pairs.

4,990,436 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 100.70 | 0.6% | 43.83 | 101.55 | 208.36 | 2.07x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 61.88 | 20.1% | 29.56 | 61.59 | 156.02 | 2.52x | 5 |
| c7g (AWS Graviton3, NEON) | 84.96 | 2.8% | 33.08 | 85.15 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 86.67 | 8.7% | 38.73 | 100.52 | 187.19 | 2.16x | 5 |
| c8g (AWS Graviton4, NEON) | 68.78 | 15.1% | 36.87 | 66.94 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 81.56 | 6.9% | 32.08 | 83.39 | 190.20 | 2.33x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 14.5 | 13.5 | 21.3 | 94% | 98.44 |
| c7a | 14.8 | 13.6 | 21.2 | 92% | 59.22 |
| c7g | 14.5 | 13.4 | — | 95% | 82.98 |
| c7i | 14.7 | 13.5 | 21.2 | 96% | 85.20 |
| c8g | 14.6 | 13.5 | — | 95% | 65.95 |
| m7i | 14.6 | 13.5 | 21.2 | 96% | 80.61 |

## Agreement

Concordance is the percentage of primary reads whose alignment record matches between the two runs. Where it is below 99.99% the table breaks the differences down by class; a read counts once under each class of difference it has. See [methodology](../../methodology.md#agreement) for which tags each comparison ignores.

### vs bwa-mem2 v2.2.1

x86 instances only (bwa-mem2 v2.2.1 has no Arm build). Tags bwa-mem3 adds or computes differently by design are ignored.

| instances | concordance | cells |
| --- | --- | --- |
| c6a, c7a, c7i, m7i | 100.0000% | 20 |

### `--compat` vs bwa-mem2 v2.2.1

`--compat=bwa-mem2` promises output identical to bwa-mem2, with nothing ignored.

| instances | concordance | cells |
| --- | --- | --- |
| c6a, c7a, c7g, c7i, c8g, m7i | 100.0000% | 30 |

### vs the previous release (v0.11.0)

Every tag compared. Differences here are intentional changes in this release, described in its release notes.

| instances | concordance | cells |
| --- | --- | --- |
| c6a, c7a, c7g, c7i, c8g, m7i | 100.0000% | 30 |

### Arm vs x86

The same release on Graviton compared with its x86 output.

| instances | concordance | cells |
| --- | --- | --- |
| c7g, c8g | 100.0000% | 10 |

### `--fast` vs the default preset

`--fast` deliberately prunes the candidate alignments it considers, so it is not expected to match the default preset. bwa-mem3's documentation reports that about 85% of the reads it re-places had MAPQ 0 (multi-mapping); the [accuracy page](accuracy.md) measures the effect against simulated truth.

| instances | concordance | cells | differences (% of reads) |
| --- | --- | --- | --- |
| c6a, c7a, c7g, c7i, c8g, m7i | 94.2390% | 30 | position 3.368%; CIGAR 0.792%; FLAG 1.055%; MAPQ 3.080%; aux tags 1.716%; mapped only by reference side 0.016%; mapped only by bwa-mem3 0.002% |
