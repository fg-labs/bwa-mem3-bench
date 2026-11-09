# sbx-1M: Roche SBX (HG002, single-end)

bwa-mem3 v0.17.0. [All datasets](README.md) · [methodology](../../methodology.md)

About 1M single-end Roche SBX reads, 50-974 bp (median ~224 bp), HG002, from the Roche Axelios GIAB demonstration data (CC BY-NC 4.0).

1,001,951 reads, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (identical alignment records, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) measures only `wgs-5M` (against bwa, bwa-mem2, minibwa), so this dataset has no same-host number. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 14.46 | 6.3% | 7.87 | 15.04 | 65.66 | 4.54x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 10.50 | 3.2% | 5.77 | 10.39 | 57.97 | 5.52x | 5 |
| c7g (AWS Graviton3, NEON) | 17.95 | 3.3% | 6.23 | 18.57 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 10.28 | 5.2% | 5.89 | 11.01 | 64.08 | 6.23x | 5 |
| c8g (AWS Graviton4, NEON) | 15.27 | 4.8% | 4.99 | 15.21 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 10.17 | 2.1% | 5.32 | 9.98 | 64.28 | 6.32x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.4 | 17.2 | 21.4 | 83% | 13.08 |
| c7a | 19.4 | 17.3 | 21.5 | 80% | 9.09 |
| c7g | 20.0 | 17.0 | — | 63% | 16.88 |
| c7i | 19.5 | 17.3 | 21.5 | 85% | 9.56 |
| c8g | 20.2 | 17.1 | — | 61% | 14.15 |
| m7i | 19.5 | 17.4 | 21.5 | 87% | 9.21 |

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

### vs the previous release (v0.16.0)

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
