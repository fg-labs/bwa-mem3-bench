# wgs-5M: Whole genome (HG00096)

bwa-mem3 v0.16.0. [All datasets](README.md) · [methodology](../../methodology.md)

1000 Genomes HG00096 30x WGS (NYGC, GRCh38), downsampled to 5M read pairs.

4,990,436 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 58.84 | 1.3% | 31.74 | 58.83 | 208.36 | 3.54x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 42.78 | 11.1% | 20.41 | 34.91 | 156.02 | 3.65x | 5 |
| c7g (AWS Graviton3, NEON) | 43.60 | 4.2% | 27.48 | 46.78 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 43.02 | 1.9% | 24.55 | 44.08 | 187.19 | 4.35x | 5 |
| c8g (AWS Graviton4, NEON) | 36.05 | 3.5% | 17.74 | 36.75 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 50.87 | 8.1% | 23.42 | 50.79 | 190.20 | 3.74x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.0 | 18.0 | 21.3 | 94% | 57.28 |
| c7a | 19.3 | 18.1 | 21.2 | 75% | 31.98 |
| c7g | 19.3 | 18.1 | — | 95% | 42.04 |
| c7i | 19.3 | 18.0 | 21.2 | 96% | 42.18 |
| c8g | 19.3 | 18.1 | — | 94% | 34.29 |
| m7i | 19.3 | 18.0 | 21.2 | 96% | 49.03 |

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

### vs the previous release (v0.15.1)

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
