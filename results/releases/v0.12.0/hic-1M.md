# hic-1M: Hi-C (HG002)

bwa-mem3 v0.12.0. [All datasets](README.md) · [methodology](../../methodology.md)

HG002 Hi-C, 1M read pairs (2x151 bp) from Zenodo 10.5281/zenodo.19703025 (CC BY 4.0).

1,000,000 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Aligned with `-5SP` on both bwa-mem3 and bwa-mem2, the canonical Hi-C settings (no mate rescue, no pairing, smallest-coordinate split as primary).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 25.47 | 2.5% | 12.69 | 24.50 | 74.96 | 2.94x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 15.76 | 3.2% | 8.87 | 14.94 | 63.33 | 4.02x | 5 |
| c7g (AWS Graviton3, NEON) | 18.34 | 2.6% | 9.63 | 17.88 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 19.45 | 19.6% | 11.38 | 22.45 | 71.13 | 3.66x | 5 |
| c8g (AWS Graviton4, NEON) | 15.14 | 2.3% | 8.19 | 15.14 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 18.56 | 4.0% | 9.73 | 18.07 | 65.85 | 3.55x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 15.0 | 13.0 | 21.3 | 88% | 23.83 |
| c7a | 15.2 | 12.9 | 21.2 | 82% | 13.92 |
| c7g | 15.2 | 13.0 | — | 86% | 16.60 |
| c7i | 15.2 | 12.9 | 21.2 | 89% | 18.29 |
| c8g | 15.2 | 13.0 | — | 84% | 13.43 |
| m7i | 15.2 | 13.0 | 21.2 | 89% | 16.76 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 79.1948% | 30 | position 8.842%; CIGAR 6.988%; FLAG 7.194%; MAPQ 9.897%; aux tags 13.930%; mapped only by reference side 0.043% |
