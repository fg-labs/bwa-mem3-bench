# meth-twist-emseq-5M: Methylation (Twist EM-seq)

bwa-mem3 v0.12.0. [All datasets](README.md) · [methodology](../../methodology.md)

Twist EM-seq library, downsampled to 5M read pairs, aligned with `--meth`.

5,184,846 read pairs, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).

Compared against bwameth, not bwa-mem2, and run only on m7i (the methylation index needs a 64 GB host).

## Speed by instance type

Median wall-clock seconds, 16 threads, on AWS Batch spot hosts. bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator. **Indicative only**: the reps of one cell can land on different hosts, and the bwameth column was measured in a separate run on other hosts, so the ratio mixes code speed with host variation. The same-host [arena](README.md#headline-same-host-speed-arena) is the number to quote. `vs bwameth` above 1 means bwa-mem3 is faster.

| instance | bwa-mem3 (s) | CV | `--fast` (s) | `--compat` (s) | bwameth (s) | vs bwameth | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| m7i (Intel Sapphire Rapids, AVX-512) | 76.05 | 4.7% | 37.38 | — | 252.75 | 3.32x | 5 |

## Memory and CPU

Peak resident memory (GB), CPU efficiency (CPU time / (wall x 16 threads)), and bwa-mem3's own `PROCESS()` time, which excludes index loading.

| instance | bwa-mem3 RSS | `--fast` RSS | bwameth RSS | CPU efficiency | PROCESS() (s) |
| --- | --- | --- | --- | --- | --- |
| m7i | 27.1 | 25.2 | 43.0 | 94% | 71.62 |

## Agreement

Concordance is the percentage of primary reads whose alignment record matches between the two runs. Where it is below 99.99% the table breaks the differences down by class; a read counts once under each class of difference it has. See [methodology](../../methodology.md#agreement) for which tags each comparison ignores.

### vs bwameth

bwa-mem3 `--meth` and bwameth score reads differently by design, so whole-record concordance is not meaningful here. The measure is **confident relocation**: the share of primary reads either aligner maps at MAPQ >= 20 that the two place at a different locus.

_Not measured for this release._

### vs the previous release (v0.11.0)

Every tag compared. Differences here are intentional changes in this release, described in its release notes.

| instances | concordance | cells | differences (% of reads) |
| --- | --- | --- | --- |
| m7i | 73.7183% | 5 | position 0.884%; CIGAR 0.340%; FLAG 0.296%; MAPQ 0.332%; aux tags 26.080%; mapped only by reference side 0.015%; mapped only by bwa-mem3 0.002% |

### `--fast` vs the default preset

`--fast` deliberately prunes the candidate alignments it considers, so it is not expected to match the default preset. bwa-mem3's documentation reports that about 85% of the reads it re-places had MAPQ 0 (multi-mapping); the [accuracy page](accuracy.md) measures the effect against simulated truth.

| instances | concordance | cells | differences (% of reads) |
| --- | --- | --- | --- |
| m7i | 92.7902% | 5 | position 2.887%; CIGAR 1.539%; FLAG 1.423%; MAPQ 4.140%; aux tags 2.586%; mapped only by reference side 0.211%; mapped only by bwa-mem3 0.002% |
