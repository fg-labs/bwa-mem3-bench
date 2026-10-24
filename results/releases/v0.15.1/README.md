# bwa-mem3 v0.15.1 benchmark results

Benchmarked at fg-labs/bwa-mem3 `a6b2ba1eb5cda2efbd45d22e2010b8afc87f928e` (release tag commit `51ee7ebde618dfbcee8a4f17f7a69bc5e7c4e955`), blessed 2026-10-24 ([fg-labs/bwa-mem3#594](https://github.com/fg-labs/bwa-mem3/pull/594)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.15.1>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.15.1 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in its RSS); every other arm used its stock index.

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.1** | 19.62 | 1.00x | 1.70x | 26.7 |
| **bwa-mem3 v0.15.1 `--fast`** | 11.56 | — | 1.00x | 25.5 |
| bwa | 195.57 | 9.97x | 16.92x | 7.4 |
| bwa-mem2 | 85.92 | 4.38x | 7.43x | 21.1 |
| minibwa | 30.42 | 1.55x | 2.63x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 56.39 | — | — | 21.8 |
| v0.2.2 | 56.78 | — | 0.99x | 20.9 |
| v0.3.0 | 55.35 | — | 1.03x | 21.2 |
| v0.4.0 | 52.73 | — | 1.05x | 16.3 |
| v0.5.0 | 52.55 | 22.81 | 1.00x | 16.5 |
| v0.6.0 | 50.37 | 21.84 | 1.04x | 17.0 |
| v0.7.0 | 48.90 | 22.29 | 1.03x | 17.1 |
| 394f8f8 (interim build) | 46.50 | 21.75 | 1.05x | 16.6 |
| v0.8.0 | 43.43 | 18.71 | 1.07x | 14.6 |
| v0.9.0 | 43.42 | 18.21 | 1.00x | 14.6 |
| v0.10.0 | 43.42 | 18.70 | 1.00x | 14.6 |
| v0.11.0 | 41.83 | 17.67 | 1.04x | 14.7 |
| v0.12.0 | 33.18 | 14.65 | 1.26x | 14.8 |
| v0.13.0 | 30.05 | 14.18 | 1.10x | 26.1 |
| v0.14.0 | 24.15 | 13.07 | 1.24x | 26.8 |
| v0.14.1 | 24.04 | 13.07 | 1.00x | 26.3 |
| v0.15.0 | 24.34 | 12.07 | 0.99x | 25.9 |
| v0.15.1 | 19.62 | 11.56 | 1.24x | 26.7 |

</details>

### m8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.1** | 32.38 | 1.00x | 2.15x | 26.7 |
| **bwa-mem3 v0.15.1 `--fast`** | 15.07 | — | 1.00x | 25.3 |
| bwa | 233.94 | 7.22x | 15.52x | 7.4 |
| minibwa | 40.70 | 1.26x | 2.70x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 121.08 | — | — | 21.5 |
| v0.2.2 | 118.12 | — | 1.03x | 21.8 |
| v0.3.0 | 103.20 | — | 1.14x | 21.4 |
| v0.4.0 | 104.25 | — | 0.99x | 16.3 |
| v0.5.0 | 104.45 | 37.16 | 1.00x | 16.2 |
| v0.6.0 | 97.21 | 36.49 | 1.07x | 16.7 |
| v0.7.0 | 91.55 | 39.69 | 1.06x | 16.8 |
| 394f8f8 (interim build) | 85.96 | 36.96 | 1.07x | 16.5 |
| v0.8.0 | 76.32 | 28.46 | 1.13x | 14.5 |
| v0.9.0 | 76.43 | 28.47 | 1.00x | 14.5 |
| v0.10.0 | 71.05 | 27.90 | 1.08x | 14.4 |
| v0.11.0 | 63.93 | 26.31 | 1.11x | 14.5 |
| v0.12.0 | 58.22 | 19.24 | 1.10x | 14.6 |
| v0.13.0 | 55.08 | 18.72 | 1.06x | 25.8 |
| v0.14.0 | 41.72 | 17.60 | 1.32x | 26.8 |
| v0.14.1 | 35.55 | 16.59 | 1.17x | 26.7 |
| v0.15.0 | 34.85 | 16.09 | 1.02x | 26.8 |
| v0.15.1 | 32.38 | 15.07 | 1.08x | 26.7 |

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
- Agreement with the previous release (v0.15.0) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
