# panel-agilent-qxt-5M: Targeted panel (Agilent SureSelect QXT)

bwa-mem3 v0.15.0. [All datasets](README.md) · [methodology](../../methodology.md)

Agilent SureSelect QXT hereditary-cancer panel, SRR15497869 (PRJNA755485), downsampled to 5M read pairs.

5,309,541 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Deep target coverage produces many split alignments, so this dataset leans on supplementary-alignment handling more than the others.

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used a denser stride-4 suffix-array index (byte-identical output, faster, more memory); the comparator used its stock index. **Indicative only**: the reps of one cell can land on different hosts, and the bwa-mem2 column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwa-mem2` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwa-mem2 (s) | vs bwa-mem2 | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c6a (AMD EPYC Milan (Zen 3), AVX2) | 58.19 | 1.1% | 35.62 | 58.36 | 148.47 | 2.55x | 5 |
| c7a (AMD EPYC Genoa (Zen 4), AVX-512) | 39.23 | 9.7% | 24.13 | 36.17 | 131.59 | 3.35x | 5 |
| c7g (AWS Graviton3, NEON) | 40.62 | 2.9% | 27.22 | 40.87 | — | — | 5 |
| c7i (Intel Sapphire Rapids, AVX-512) | 40.28 | 3.2% | 26.05 | 38.55 | 155.03 | 3.85x | 5 |
| c8g (AWS Graviton4, NEON) | 34.41 | 22.6% | 20.69 | 31.29 | — | — | 5 |
| m7i (Intel Sapphire Rapids, AVX-512) | 40.13 | 52.4% | 24.45 | 45.38 | 136.35 | 3.40x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwa-mem2 RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| c6a | 18.4 | 18.0 | 19.6 | 94% | 55.64 |
| c7a | 18.6 | 17.9 | 19.7 | 80% | 32.34 |
| c7g | 18.4 | 17.6 | — | 94% | 39.30 |
| c7i | 18.6 | 18.0 | 19.7 | 94% | 38.29 |
| c8g | 18.5 | 17.7 | — | 91% | 31.79 |
| m7i | 18.6 | 18.0 | 19.7 | 92% | 37.39 |

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
| c6a, c7a, c7g, c7i, c8g, m7i | 96.4756% | 30 | position 2.303%; CIGAR 0.452%; FLAG 0.720%; MAPQ 1.431%; aux tags 0.867%; mapped only by reference side 0.010%; mapped only by bwa-mem3 0.004% |
