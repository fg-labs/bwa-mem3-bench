# hic-1M: Hi-C (HG002)

bwa-mem3 v0.13.0. [All datasets](README.md) · [methodology](../../methodology.md)

HG002 Hi-C, 1M read pairs (2x151 bp) from Zenodo 10.5281/zenodo.19703025 (CC BY 4.0).

1,000,000 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Aligned with `-5SP` on both bwa-mem3 and bwa-mem2, the canonical Hi-C settings (no mate rescue, no pairing, smallest-coordinate split as primary).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 23.89 | 4.0% | 13.23 | 24.68 | 74.96 | 3.14x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 15.21 | 4.1% | 9.28 | 15.20 | 63.33 | 4.16x | 5 |
| c7g (AWS Graviton3, NEON) | 16.85 | 1.7% | 9.97 | 18.08 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 17.82 | 3.9% | 9.60 | 18.25 | 71.13 | 3.99x | 5 |
| c8g (AWS Graviton4, NEON) | 13.61 | 12.2% | 8.47 | 14.94 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 20.16 | 13.3% | 9.74 | 17.75 | 65.85 | 3.27x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 18.8 | 16.7 | 21.3 | 86% | 22.07 |
| c7a | 18.9 | 16.7 | 21.2 | 80% | 13.12 |
| c7g | 19.0 | 16.7 | — | 84% | 15.02 |
| c7i | 19.0 | 16.7 | 21.2 | 88% | 16.55 |
| c8g | 18.8 | 16.5 | — | 80% | 11.74 |
| m7i | 19.0 | 16.8 | 21.2 | 89% | 18.83 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 79.1948% | 30 | position 8.842%; CIGAR 6.988%; FLAG 7.194%; MAPQ 9.897%; aux tags 13.930%; mapped only by reference side 0.043% |
