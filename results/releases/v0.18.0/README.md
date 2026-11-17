# bwa-mem3 v0.18.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `0368a60001874372f8203aade5cf1c8934324068` (release tag commit `e0903cb9395bc755059358f916b2efdb58a30073`), blessed 2026-11-16 ([fg-labs/bwa-mem3#802](https://github.com/fg-labs/bwa-mem3/pull/802)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.18.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads. Runs were grouped into blocks by index family, and in each round of a block every arm ran once, in shuffled order. This release and the previous one always ran back-to-back, so their ratio is a paired, like-for-like estimate. Other arms in a block shared its rounds but were not always adjacent, and a ratio across blocks, such as against another aligner, can also carry a change in host state between them. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.18.0 and releases v0.13.0 to v0.17.0 used a denser stride-2 suffix-array index (identical alignment records, more memory, reflected in their RSS); v0.12.0 and older releases, and the other aligners, used their stock index.

Every row, earlier releases included, was re-measured in this release's arena run, so the rows compare with each other but not with the walls on another release's page: the arena's method has changed between releases (see [the methodology page](../../methodology.md)).

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.18.0** | 15.44 | 1.00x | 1.73x | 26.3 |
| **bwa-mem3 v0.18.0 `--fast`** | 8.95 | — | 1.00x | 25.5 |
| bwa | 198.33 | 12.85x | 22.16x | 7.4 |
| bwa-mem2 | 88.30 | 5.72x | 9.87x | 21.1 |
| minibwa | 30.03 | 1.94x | 3.36x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 54.20 | — | — | 22.6 |
| v0.2.2 | 52.18 | — | 1.04x (context) | 22.6 |
| v0.3.0 | 49.12 | — | 1.06x (context) | 23.0 |
| v0.4.0 | 51.04 | — | 0.96x (context) | 17.1 |
| v0.5.0 | 50.92 | 22.08 | 1.00x (context) | 17.0 |
| v0.6.0 | 49.56 | 21.63 | 1.03x (context) | 17.1 |
| v0.7.0 | 47.71 | 21.89 | 1.04x (context) | 17.1 |
| 394f8f8 (interim build) | 45.80 | 21.37 | 1.04x (context) | 16.6 |
| v0.8.0 | 42.78 | 17.76 | 1.07x (context) | 14.7 |
| v0.9.0 | 42.89 | 17.67 | 1.00x (context) | 14.7 |
| v0.10.0 | 42.30 | 17.63 | 1.01x (context) | 14.6 |
| v0.11.0 | 41.07 | 16.80 | 1.03x (context) | 14.7 |
| v0.12.0 | 32.32 | 13.92 | 1.27x (context) | 14.8 |
| v0.13.0 | 29.24 | 13.24 | 1.10x (unpaired) | 26.1 |
| v0.14.0 | 23.17 | 12.12 | 1.26x (same-round) | 26.7 |
| v0.14.1 | 21.39 | 11.93 | 1.08x (same-round) | 26.7 |
| v0.15.0 | 20.30 | 11.11 | 1.05x (same-round) | 26.8 |
| v0.15.1 | 18.44 | 10.38 | 1.11x (same-round) | 26.8 |
| v0.16.0 | 15.96 | 9.06 | 1.16x (same-round) | 26.8 |
| v0.17.0 | 15.25 | 9.04 | 1.05x (same-round) | 26.3 |
| v0.18.0 | 15.44 | 8.95 | 1.00x (inconclusive) | 26.3 |

</details>

### m8g (AWS Graviton4, NEON), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.18.0** | 28.42 | 1.00x | 2.28x | 26.5 |
| **bwa-mem3 v0.18.0 `--fast`** | 12.44 | — | 1.00x | 25.5 |
| bwa | 250.66 | 8.82x | 20.15x | 7.4 |
| minibwa | 40.66 | 1.43x | 3.27x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 120.63 | — | — | 22.3 |
| v0.2.2 | 118.26 | — | 1.02x (context) | 22.3 |
| v0.3.0 | 102.35 | — | 1.16x (context) | 22.7 |
| v0.4.0 | 104.10 | — | 0.98x (context) | 16.8 |
| v0.5.0 | 104.20 | 36.73 | 1.00x (context) | 16.8 |
| v0.6.0 | 97.40 | 36.22 | 1.07x (context) | 16.8 |
| v0.7.0 | 91.45 | 39.33 | 1.06x (context) | 16.8 |
| 394f8f8 (interim build) | 85.91 | 36.51 | 1.06x (context) | 16.5 |
| v0.8.0 | 76.42 | 27.71 | 1.12x (context) | 14.5 |
| v0.9.0 | 76.39 | 27.85 | 1.00x (context) | 14.5 |
| v0.10.0 | 70.85 | 27.04 | 1.08x (context) | 14.4 |
| v0.11.0 | 63.96 | 25.52 | 1.11x (context) | 14.5 |
| v0.12.0 | 58.05 | 18.70 | 1.10x (context) | 14.6 |
| v0.13.0 | 54.32 | 18.10 | 1.07x (unpaired) | 25.9 |
| v0.14.0 | 41.50 | 17.17 | 1.31x (same-round) | 26.7 |
| v0.14.1 | 35.08 | 15.93 | 1.18x (same-round) | 26.8 |
| v0.15.0 | 34.16 | 15.42 | 1.02x (same-round) | 26.8 |
| v0.15.1 | 31.40 | 14.12 | 1.09x (same-round) | 26.8 |
| v0.16.0 | 28.81 | 12.64 | 1.09x (same-round) | 26.8 |
| v0.17.0 | 28.39 | 12.48 | 1.02x (same-round) | 26.4 |
| v0.18.0 | 28.42 | 12.44 | 1.00x | 26.5 |

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
- Agreement with the previous release (v0.17.0) is on the pages for [wgs-5M](wgs-5M.md), [wes-5M](wes-5M.md), [panel-agilent-qxt-5M](panel-agilent-qxt-5M.md), [hic-1M](hic-1M.md), [sbx-1M](sbx-1M.md), [meth-twist-emseq-trim-5M](meth-twist-emseq-trim-5M.md); it was not measured for wgs-5M-alt.
- Raw aggregates: [`data.json`](data.json)
