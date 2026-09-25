# wes-5M: Whole exome (HG00100)

bwa-mem3 v0.13.0. [All datasets](README.md) · [methodology](../../methodology.md)

1000 Genomes HG00100 phase-3 Illumina exome, downsampled to 5M read pairs.

5,025,585 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 55.55 | 0.8% | 28.40 | 56.43 | 119.10 | 2.14x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 32.66 | 2.9% | 19.19 | 33.41 | 99.70 | 3.05x | 5 |
| c7g (AWS Graviton3, NEON) | 41.28 | 1.7% | 21.17 | 42.95 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 42.03 | 4.2% | 20.96 | 43.38 | 119.36 | 2.84x | 5 |
| c8g (AWS Graviton4, NEON) | 31.42 | 10.0% | 17.01 | 33.01 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 42.29 | 3.3% | 22.96 | 43.15 | 108.02 | 2.55x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.7 | 17.7 | 21.8 | 90% | 52.80 |
| c7a | 19.7 | 17.7 | 22.0 | 89% | 30.71 |
| c7g | 19.7 | 17.7 | — | 92% | 39.59 |
| c7i | 19.7 | 17.7 | 22.1 | 93% | 40.74 |
| c8g | 19.7 | 17.7 | — | 91% | 29.84 |
| m7i | 19.7 | 17.7 | 22.0 | 93% | 41.02 |

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

### vs the previous release (v0.12.0)

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
