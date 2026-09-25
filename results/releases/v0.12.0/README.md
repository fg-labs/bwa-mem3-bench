# bwa-mem3 v0.12.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `2e6627615ae5c50c5d12829ce9a3900f05dd58ff` (release tag commit `ac059d0152ad3ebcf5d3079492aab95551a328eb`), blessed 2026-09-13 ([fg-labs/bwa-mem3#444](https://github.com/fg-labs/bwa-mem3/pull/444)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.12.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. Every aligner used its stock index.

### c8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.12.0** | 36.99 | 1.00x | 2.35x | 14.1 |
| **bwa-mem3 v0.12.0 `--fast`** | 15.77 | — | 1.00x | 13.7 |
| bwa | 201.79 | 5.46x | 12.80x | 7.4 |
| bwa-mem2 | 99.92 | 2.70x | 6.34x | 21.3 |
| minibwa | 32.15 | 0.87x | 2.04x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 76.49 | — | — | 21.0 |
| v0.2.2 | 77.15 | — | 0.99x | 21.4 |
| v0.3.0 | 71.93 | — | 1.07x | 21.5 |
| v0.4.0 | 57.29 | — | 1.26x | 15.3 |
| v0.5.0 | 55.74 | 23.44 | 1.03x | 15.7 |
| v0.6.0 | 55.08 | 23.17 | 1.01x | 16.1 |
| v0.7.0 | 52.67 | 23.85 | 1.05x | 15.9 |
| 394f8f8 (interim build) | 49.22 | 23.38 | 1.07x | 15.4 |
| v0.8.0 | 47.94 | 19.22 | 1.03x | 13.9 |
| v0.9.0 | 47.22 | 19.93 | 1.02x | 14.0 |
| v0.10.0 | 47.73 | 20.21 | 0.99x | 13.9 |
| v0.11.0 | 46.29 | 18.21 | 1.03x | 14.0 |
| v0.12.0 | 36.99 | 15.77 | 1.25x | 14.1 |

</details>

### c8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.12.0** | 59.85 | 1.00x | 2.87x | 14.3 |
| **bwa-mem3 v0.12.0 `--fast`** | 20.86 | — | 1.00x | 13.4 |
| bwa | 245.88 | 4.11x | 11.79x | 7.4 |
| minibwa | 41.65 | 0.70x | 2.00x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 143.88 | — | — | 20.8 |
| v0.2.2 | 143.22 | — | 1.00x | 20.7 |
| v0.3.0 | 128.74 | — | 1.11x | 21.3 |
| v0.4.0 | 106.87 | — | 1.20x | 15.6 |
| v0.5.0 | 110.18 | 40.10 | 0.97x | 15.3 |
| v0.6.0 | 100.06 | 38.56 | 1.10x | 15.6 |
| v0.7.0 | 96.07 | 41.32 | 1.04x | 15.3 |
| 394f8f8 (interim build) | 89.76 | 38.38 | 1.07x | 15.0 |
| v0.8.0 | 77.72 | 29.06 | 1.15x | 14.2 |
| v0.9.0 | 78.12 | 29.30 | 0.99x | 14.3 |
| v0.10.0 | 72.89 | 30.45 | 1.07x | 14.2 |
| v0.11.0 | 66.38 | 27.23 | 1.10x | 14.2 |
| v0.12.0 | 59.85 | 20.86 | 1.11x | 14.3 |

</details>

## Datasets

Speed and agreement for each dataset, across 6 AWS instance types:

- [wgs-5M](wgs-5M.md): Whole genome (HG00096)
- [wes-5M](wes-5M.md): Whole exome (HG00100)
- [panel-agilent-qxt-5M](panel-agilent-qxt-5M.md): Targeted panel (Agilent SureSelect QXT)
- [hic-1M](hic-1M.md): Hi-C (HG002)
- [sbx-1M](sbx-1M.md): Roche SBX (HG002, single-end)
- [meth-twist-emseq-5M](meth-twist-emseq-5M.md): Methylation (Twist EM-seq)
- [wgs-5M-alt](wgs-5M-alt.md): Whole genome, ALT-aware (HG00096)

## Also

- [Accuracy against simulated truth](accuracy.md)
- [Thread scaling](scaling.md)
- Agreement with the previous release (v0.11.0) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
