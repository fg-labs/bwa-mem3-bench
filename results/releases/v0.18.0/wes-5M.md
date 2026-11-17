# wes-5M: Whole exome (HG00100)

bwa-mem3 v0.18.0. [All datasets](README.md) · [methodology](../../methodology.md)

1000 Genomes HG00100 phase-3 Illumina exome, downsampled to 5M read pairs.

5,025,585 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (identical alignment records, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) measures only `wgs-5M` (against bwa, bwa-mem2, minibwa), so this dataset has no same-host number. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 31.07 | 1.3% | 21.74 | 31.21 | 119.10 | 3.83x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 18.63 | 1.8% | 13.55 | 18.78 | 99.70 | 5.35x | 5 |
| c7g (AWS Graviton3, NEON) | 21.47 | 2.9% | 16.18 | 21.55 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 21.76 | 7.9% | 16.19 | 22.52 | 119.36 | 5.49x | 5 |
| c8g (AWS Graviton4, NEON) | 16.73 | 4.5% | 11.92 | 17.29 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 21.95 | 3.9% | 15.75 | 22.01 | 108.02 | 4.92x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.7 | 18.2 | 21.8 | 92% | 29.57 |
| c7a | 19.8 | 18.2 | 22.0 | 89% | 17.50 |
| c7g | 19.8 | 18.3 | — | 92% | 20.04 |
| c7i | 19.7 | 18.2 | 22.1 | 94% | 20.91 |
| c8g | 19.9 | 18.2 | — | 91% | 15.67 |
| m7i | 19.8 | 18.2 | 22.0 | 93% | 20.94 |

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

### vs the previous release (v0.17.0)

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
