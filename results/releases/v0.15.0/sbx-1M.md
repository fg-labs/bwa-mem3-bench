# sbx-1M: Roche SBX (HG002, single-end)

bwa-mem3 v0.15.0. [All datasets](README.md) · [methodology](../../methodology.md)

About 1M single-end Roche SBX reads, 50-974 bp (median ~224 bp), HG002, from the Roche Axelios GIAB demonstration data (CC BY-NC 4.0).

1,001,951 reads, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 19.35 | 5.0% | 10.32 | 18.56 | 65.66 | 3.39x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 13.21 | 4.2% | 7.00 | 12.64 | 57.97 | 4.39x | 5 |
| c7g (AWS Graviton3, NEON) | 22.18 | 1.1% | 8.08 | 22.33 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 12.77 | 15.0% | 7.06 | 13.66 | 64.08 | 5.02x | 5 |
| c8g (AWS Graviton4, NEON) | 18.62 | 2.1% | 6.13 | 18.86 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 12.78 | 7.9% | 7.04 | 13.24 | 64.28 | 5.03x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.5 | 16.8 | 21.4 | 84% | 17.83 |
| c7a | 19.7 | 17.1 | 21.5 | 77% | 11.43 |
| c7g | 20.1 | 16.7 | — | 68% | 20.79 |
| c7i | 19.7 | 17.1 | 21.5 | 86% | 11.93 |
| c8g | 20.2 | 16.8 | — | 66% | 17.13 |
| m7i | 19.7 | 17.1 | 21.5 | 87% | 11.95 |

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

### vs the previous release (v0.14.1)

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
| c6a, c7a, c7g, c7i, c8g, m7i | 91.9822% | 30 | position 3.099%; CIGAR 1.487%; FLAG 0.559%; MAPQ 6.635%; aux tags 2.195%; mapped only by reference side 0.018% |
