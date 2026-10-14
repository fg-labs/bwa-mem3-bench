# hic-1M: Hi-C (HG002)

bwa-mem3 v0.15.0. [All datasets](README.md) · [methodology](../../methodology.md)

HG002 Hi-C, 1M read pairs (2x151 bp) from Zenodo 10.5281/zenodo.19703025 (CC BY 4.0).

1,000,000 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Aligned with `-5SP` on both bwa-mem3 and bwa-mem2, the canonical Hi-C settings (no mate rescue, no pairing, smallest-coordinate split as primary).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 18.95 | 1.9% | 12.20 | 18.76 | 74.96 | 3.96x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 12.87 | 4.4% | 8.63 | 12.23 | 63.33 | 4.92x | 5 |
| c7g (AWS Graviton3, NEON) | 13.80 | 2.5% | 9.25 | 13.64 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 14.14 | 9.5% | 8.41 | 13.42 | 71.13 | 5.03x | 5 |
| c8g (AWS Graviton4, NEON) | 11.43 | 6.0% | 7.24 | 11.30 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 14.68 | 42.1% | 8.37 | 12.65 | 65.85 | 4.49x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.1 | 17.0 | 21.3 | 87% | 17.49 |
| c7a | 19.2 | 17.1 | 21.2 | 78% | 10.78 |
| c7g | 19.3 | 16.9 | — | 81% | 12.00 |
| c7i | 19.3 | 17.1 | 21.2 | 84% | 12.22 |
| c8g | 19.3 | 16.9 | — | 81% | 9.78 |
| m7i | 19.3 | 17.1 | 21.2 | 86% | 13.24 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 79.1948% | 30 | position 8.842%; CIGAR 6.988%; FLAG 7.194%; MAPQ 9.897%; aux tags 13.930%; mapped only by reference side 0.043% |
