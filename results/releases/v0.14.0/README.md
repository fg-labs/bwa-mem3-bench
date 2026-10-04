# bwa-mem3 v0.14.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `1a6a9d56c5d2acf186071d90a2084e59743152c7`, blessed 2026-10-03 ([fg-labs/bwa-mem3#519](https://github.com/fg-labs/bwa-mem3/pull/519)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.14.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.14.0 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in its RSS); every other arm used its stock index.

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.0** | 24.15 | 1.00x | 1.85x | 26.7 |
| **bwa-mem3 v0.14.0 `--fast`** | 13.07 | — | 1.00x | 25.5 |
| bwa | 194.94 | 8.07x | 14.92x | 7.4 |
| bwa-mem2 | 85.04 | 3.52x | 6.51x | 21.2 |
| minibwa | 30.36 | 1.26x | 2.32x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 56.63 | — | — | 21.8 |
| v0.2.2 | 53.06 | — | 1.07x | 22.3 |
| v0.3.0 | 51.30 | — | 1.03x | 22.1 |
| v0.4.0 | 51.49 | — | 1.00x | 17.1 |
| v0.5.0 | 51.44 | 22.84 | 1.00x | 17.0 |
| v0.6.0 | 50.03 | 22.34 | 1.03x | 17.0 |
| v0.7.0 | 48.34 | 22.31 | 1.03x | 17.0 |
| 394f8f8 (interim build) | 46.46 | 21.75 | 1.04x | 16.6 |
| v0.8.0 | 43.37 | 18.69 | 1.07x | 14.6 |
| v0.9.0 | 43.29 | 18.17 | 1.00x | 14.6 |
| v0.10.0 | 42.82 | 18.16 | 1.01x | 14.6 |
| v0.11.0 | 41.77 | 17.64 | 1.03x | 14.8 |
| v0.12.0 | 32.59 | 14.64 | 1.28x | 14.8 |
| v0.13.0 | 29.83 | 13.65 | 1.09x | 26.1 |
| v0.14.0 | 24.15 | 13.07 | 1.24x | 26.7 |

</details>

### m8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.0** | 42.23 | 1.00x | 2.34x | 26.7 |
| **bwa-mem3 v0.14.0 `--fast`** | 18.08 | — | 1.00x | 25.3 |
| bwa | 240.38 | 5.69x | 13.30x | 7.4 |
| minibwa | 40.59 | 0.96x | 2.25x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 120.54 | — | — | 22.2 |
| v0.2.2 | 122.73 | — | 0.98x | 20.9 |
| v0.3.0 | 102.11 | — | 1.20x | 22.5 |
| v0.4.0 | 104.14 | — | 0.98x | 16.8 |
| v0.5.0 | 104.47 | 37.23 | 1.00x | 16.8 |
| v0.6.0 | 97.69 | 36.59 | 1.07x | 16.8 |
| v0.7.0 | 92.06 | 39.78 | 1.06x | 16.8 |
| 394f8f8 (interim build) | 86.21 | 36.96 | 1.07x | 16.5 |
| v0.8.0 | 76.34 | 28.33 | 1.13x | 14.5 |
| v0.9.0 | 76.65 | 28.34 | 1.00x | 14.5 |
| v0.10.0 | 71.11 | 27.80 | 1.08x | 14.4 |
| v0.11.0 | 64.32 | 26.30 | 1.11x | 14.5 |
| v0.12.0 | 58.68 | 19.22 | 1.10x | 14.6 |
| v0.13.0 | 55.02 | 18.73 | 1.07x | 25.9 |
| v0.14.0 | 42.23 | 18.08 | 1.30x | 26.7 |

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
- Agreement with the previous release (v0.13.0) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
