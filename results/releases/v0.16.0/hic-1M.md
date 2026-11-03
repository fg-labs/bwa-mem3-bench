# hic-1M: Hi-C (HG002)

bwa-mem3 v0.16.0. [All datasets](README.md) · [methodology](../../methodology.md)

HG002 Hi-C, 1M read pairs (2x151 bp) from Zenodo 10.5281/zenodo.19703025 (CC BY 4.0).

1,000,000 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Aligned with `-5SP` on both bwa-mem3 and bwa-mem2, the canonical Hi-C settings (no mate rescue, no pairing, smallest-coordinate split as primary).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) measures only `wgs-5M` (against bwa, bwa-mem2, minibwa), so this dataset has no same-host number. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 15.74 | 2.4% | 10.31 | 15.61 | 74.96 | 4.76x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 10.55 | 3.1% | 7.35 | 10.24 | 63.33 | 6.00x | 5 |
| c7g (AWS Graviton3, NEON) | 11.19 | 0.5% | 7.49 | 11.75 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 11.45 | 4.8% | 7.56 | 11.52 | 71.13 | 6.21x | 5 |
| c8g (AWS Graviton4, NEON) | 9.50 | 2.9% | 6.15 | 9.55 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 12.03 | 7.7% | 7.48 | 10.70 | 65.85 | 5.47x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 19.2 | 17.0 | 21.3 | 81% | 13.67 |
| c7a | 19.3 | 17.1 | 21.2 | 75% | 8.39 |
| c7g | 19.2 | 17.0 | — | 78% | 9.31 |
| c7i | 19.3 | 17.1 | 21.2 | 83% | 9.85 |
| c8g | 19.3 | 17.0 | — | 76% | 7.48 |
| m7i | 19.3 | 17.1 | 21.2 | 84% | 10.59 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 79.1948% | 30 | position 8.842%; CIGAR 6.988%; FLAG 7.194%; MAPQ 9.897%; aux tags 13.930%; mapped only by reference side 0.043% |
