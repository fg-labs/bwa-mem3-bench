# sbx-1M: Roche SBX (HG002, single-end)

bwa-mem3 v0.13.0. [All datasets](README.md) · [methodology](../../methodology.md)

About 1M single-end Roche SBX reads, 50-974 bp (median ~224 bp), HG002, from the Roche Axelios GIAB demonstration data (CC BY-NC 4.0).

1,001,951 reads, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 22.49 | 1.5% | 10.90 | 23.72 | 65.66 | 2.92x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 14.34 | 2.7% | 7.22 | 15.71 | 57.97 | 4.04x | 5 |
| c7g (AWS Graviton3, NEON) | 27.58 | 1.0% | 8.68 | 28.27 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 17.08 | 3.7% | 7.70 | 18.14 | 64.08 | 3.75x | 5 |
| c8g (AWS Graviton4, NEON) | 22.24 | 3.0% | 7.26 | 23.76 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 18.12 | 10.0% | 8.08 | 17.67 | 64.28 | 3.55x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 18.6 | 16.2 | 21.4 | 82% | 20.61 |
| c7a | 18.8 | 16.4 | 21.5 | 77% | 12.88 |
| c7g | 19.3 | 16.2 | — | 65% | 26.18 |
| c7i | 18.8 | 16.5 | 21.5 | 85% | 15.86 |
| c8g | 19.3 | 16.2 | — | 65% | 20.73 |
| m7i | 18.8 | 16.6 | 21.5 | 84% | 16.71 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 91.9822% | 30 | position 3.099%; CIGAR 1.487%; FLAG 0.559%; MAPQ 6.635%; aux tags 2.195%; mapped only by reference side 0.018% |
