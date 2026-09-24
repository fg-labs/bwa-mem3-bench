# wes-5M: Whole exome (HG00100)

bwa-mem3 v0.12.0. [All datasets](README.md) · [methodology](../../methodology.md)

1000 Genomes HG00100 phase-3 Illumina exome, downsampled to 5M read pairs.

5,025,585 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 56.62 | 1.1% | 31.12 | 56.55 | 119.10 | 2.10x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 37.90 | 23.2% | 19.76 | 32.68 | 99.70 | 2.63x | 5 |
| c7g (AWS Graviton3, NEON) | 44.59 | 5.9% | 24.27 | 41.80 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 52.49 | 15.1% | 26.13 | 51.04 | 119.36 | 2.27x | 5 |
| c8g (AWS Graviton4, NEON) | 32.73 | 25.2% | 19.77 | 33.55 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 41.85 | 3.2% | 23.03 | 43.09 | 108.02 | 2.58x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 15.9 | 13.8 | 21.8 | 91% | 54.07 |
| c7a | 15.9 | 13.9 | 22.0 | 81% | 32.34 |
| c7g | 15.9 | 13.7 | — | 92% | 42.45 |
| c7i | 16.0 | 13.9 | 22.1 | 94% | 50.93 |
| c8g | 16.0 | 13.7 | — | 92% | 31.29 |
| m7i | 15.9 | 13.9 | 22.0 | 94% | 40.87 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 93.7384% | 30 | position 3.679%; CIGAR 0.567%; FLAG 1.167%; MAPQ 2.840%; aux tags 1.163%; mapped only by reference side 0.082%; mapped only by bwa-mem3 0.001% |
