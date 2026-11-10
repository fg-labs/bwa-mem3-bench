# bwa-mem3 v0.17.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `1eaeb8ee31f997a14c82c888efc0b43329a6b9c4` (release tag commit `58d55c1845acddda82bf8ad0b6518400f9c3036f`), blessed 2026-11-08 ([fg-labs/bwa-mem3#756](https://github.com/fg-labs/bwa-mem3/pull/756)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.17.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads. Runs were grouped into blocks by index family, and in each round of a block every arm ran once, in shuffled order. This release and the previous one always ran back-to-back, so their ratio is a paired, like-for-like estimate. Other arms in a block shared its rounds but were not always adjacent, and a ratio across blocks, such as against another aligner, can also carry a change in host state between them. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.17.0 and releases v0.13.0 to v0.16.0 used a denser stride-2 suffix-array index (identical alignment records, more memory, reflected in their RSS); v0.12.0 and older releases, and the other aligners, used their stock index.

Every row, earlier releases included, was re-measured in this release's arena run, so the rows compare with each other but not with the walls on another release's page: the arena's method has changed between releases (see [the methodology page](../../methodology.md)).

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.17.0** | 16.19 | 1.00x | 1.67x | 26.3 |
| **bwa-mem3 v0.17.0 `--fast`** | 9.69 | — | 1.00x | 25.5 |
| bwa | 208.17 | 12.86x | 21.48x | 7.4 |
| bwa-mem2 | 93.06 | 5.75x | 9.60x | 21.2 |
| minibwa | 32.24 | 1.99x | 3.33x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 58.05 | — | — | 22.5 |
| v0.2.2 | 55.77 | — | 1.04x (context) | 22.6 |
| v0.3.0 | 52.80 | — | 1.06x (context) | 23.0 |
| v0.4.0 | 54.20 | — | 0.97x (context) | 17.1 |
| v0.5.0 | 54.02 | 23.43 | 1.00x (context) | 17.0 |
| v0.6.0 | 52.60 | 23.14 | 1.03x (context) | 17.0 |
| v0.7.0 | 50.80 | 23.39 | 1.03x (context) | 17.1 |
| 394f8f8 (interim build) | 48.88 | 22.75 | 1.04x (context) | 16.6 |
| v0.8.0 | 46.06 | 19.14 | 1.06x (context) | 14.6 |
| v0.9.0 | 46.03 | 19.03 | 1.00x (context) | 14.7 |
| v0.10.0 | 45.16 | 19.10 | 1.02x (context) | 14.7 |
| v0.11.0 | 43.78 | 18.16 | 1.03x (context) | 14.8 |
| v0.12.0 | 34.95 | 14.98 | 1.25x (context) | 14.9 |
| v0.13.0 | 30.84 | 13.94 | 1.13x (unpaired) | 26.1 |
| v0.14.0 | 24.71 | 12.86 | 1.25x (inconclusive) | 26.7 |
| v0.14.1 | 22.90 | 12.68 | 1.08x (same-round) | 26.7 |
| v0.15.0 | 21.43 | 11.62 | 1.07x (same-round) | 26.8 |
| v0.15.1 | 19.70 | 11.01 | 1.08x (same-round) | 26.8 |
| v0.16.0 | 17.21 | 9.78 | 1.15x (inconclusive) | 26.8 |
| v0.17.0 | 16.19 | 9.69 | 1.06x | 26.3 |

</details>

### m8g (AWS Graviton4, NEON), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.17.0** | 28.19 | 1.00x | 2.29x | 26.4 |
| **bwa-mem3 v0.17.0 `--fast`** | 12.33 | — | 1.00x | 25.5 |
| bwa | 235.69 | 8.36x | 19.12x | 7.4 |
| minibwa | 40.43 | 1.43x | 3.28x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 120.01 | — | — | 22.3 |
| v0.2.2 | 117.71 | — | 1.02x (context) | 22.3 |
| v0.3.0 | 101.37 | — | 1.16x (context) | 22.6 |
| v0.4.0 | 103.54 | — | 0.98x (context) | 16.8 |
| v0.5.0 | 103.74 | 36.54 | 1.00x (context) | 16.8 |
| v0.6.0 | 96.67 | 35.65 | 1.07x (context) | 16.8 |
| v0.7.0 | 91.10 | 39.10 | 1.06x (context) | 16.8 |
| 394f8f8 (interim build) | 85.68 | 36.33 | 1.06x (context) | 16.5 |
| v0.8.0 | 75.82 | 27.50 | 1.13x (context) | 14.5 |
| v0.9.0 | 75.78 | 27.60 | 1.00x (context) | 14.5 |
| v0.10.0 | 70.32 | 26.99 | 1.08x (context) | 14.4 |
| v0.11.0 | 63.50 | 25.40 | 1.11x (context) | 14.5 |
| v0.12.0 | 57.66 | 18.56 | 1.10x (context) | 14.6 |
| v0.13.0 | 53.98 | 17.92 | 1.07x (unpaired) | 25.9 |
| v0.14.0 | 41.05 | 16.96 | 1.31x (same-round) | 26.7 |
| v0.14.1 | 34.64 | 15.72 | 1.18x (same-round) | 26.7 |
| v0.15.0 | 33.93 | 15.15 | 1.02x (same-round) | 26.8 |
| v0.15.1 | 31.09 | 14.07 | 1.09x (same-round) | 26.7 |
| v0.16.0 | 28.53 | 12.45 | 1.09x (same-round) | 26.8 |
| v0.17.0 | 28.19 | 12.33 | 1.01x | 26.4 |

</details>

## Datasets

Speed and agreement for each dataset, across 6 AWS instance types:

- [wgs-5M](wgs-5M.md): Whole genome (HG00096)
- [wes-5M](wes-5M.md): Whole exome (HG00100)
- [panel-agilent-qxt-5M](panel-agilent-qxt-5M.md): Targeted panel (Agilent SureSelect QXT)
- [hic-1M](hic-1M.md): Hi-C (HG002)
- [sbx-1M](sbx-1M.md): Roche SBX (HG002, single-end)
- [meth-twist-emseq-trim-5M](meth-twist-emseq-trim-5M.md): Methylation (Twist EM-seq) (m7i only)
- [wgs-5M-alt](wgs-5M-alt.md): Whole genome, ALT-aware (HG00096)

## Also

- [Accuracy against simulated truth](accuracy.md)
- [Thread scaling](scaling.md)
- Agreement with the previous release (v0.16.0) is on the pages for [wgs-5M](wgs-5M.md), [wes-5M](wes-5M.md), [panel-agilent-qxt-5M](panel-agilent-qxt-5M.md), [hic-1M](hic-1M.md), [sbx-1M](sbx-1M.md); it was not measured for meth-twist-emseq-trim-5M, wgs-5M-alt.
- Raw aggregates: [`data.json`](data.json)
