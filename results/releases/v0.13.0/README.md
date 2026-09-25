# bwa-mem3 v0.13.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `64420af4d1672350b734b9de49cae3933fb0cb8c` (release tag commit `e3904dd7e27792f4c97d98e7a2146a55096c3429`), blessed 2026-09-23 ([fg-labs/bwa-mem3#507](https://github.com/fg-labs/bwa-mem3/pull/507)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.13.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.13.0 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in its RSS); every other arm used its stock index.

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.13.0** | 30.57 | 1.00x | 2.16x | 26.1 |
| **bwa-mem3 v0.13.0 `--fast`** | 14.16 | — | 1.00x | 25.0 |
| bwa | 197.78 | 6.47x | 13.97x | 7.4 |
| bwa-mem2 | 84.09 | 2.75x | 5.94x | 21.2 |
| minibwa | 30.38 | 0.99x | 2.15x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 55.49 | — | — | 22.6 |
| v0.2.2 | 54.01 | — | 1.03x | 22.6 |
| v0.3.0 | 50.11 | — | 1.08x | 23.0 |
| v0.4.0 | 51.39 | — | 0.98x | 17.1 |
| v0.5.0 | 51.91 | 22.31 | 0.99x | 17.0 |
| v0.6.0 | 51.00 | 21.85 | 1.02x | 17.0 |
| v0.7.0 | 48.70 | 22.30 | 1.05x | 17.1 |
| 394f8f8 (interim build) | 46.49 | 21.76 | 1.05x | 16.6 |
| v0.8.0 | 43.79 | 18.68 | 1.06x | 14.6 |
| v0.9.0 | 43.78 | 18.18 | 1.00x | 14.6 |
| v0.10.0 | 43.29 | 18.66 | 1.01x | 14.6 |
| v0.11.0 | 41.73 | 17.66 | 1.04x | 14.7 |
| v0.12.0 | 33.02 | 15.14 | 1.26x | 14.8 |
| v0.13.0 | 30.57 | 14.16 | 1.08x | 26.1 |

</details>

### m8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.13.0** | 55.26 | 1.00x | 2.87x | 25.9 |
| **bwa-mem3 v0.13.0 `--fast`** | 19.26 | — | 1.00x | 24.8 |
| bwa | 243.30 | 4.40x | 12.63x | 7.4 |
| minibwa | 41.14 | 0.74x | 2.14x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 121.94 | — | — | 22.2 |
| v0.2.2 | 119.32 | — | 1.02x | 22.3 |
| v0.3.0 | 103.42 | — | 1.15x | 22.6 |
| v0.4.0 | 105.35 | — | 0.98x | 16.8 |
| v0.5.0 | 105.53 | 38.36 | 1.00x | 16.8 |
| v0.6.0 | 98.74 | 37.75 | 1.07x | 16.8 |
| v0.7.0 | 92.31 | 40.76 | 1.07x | 16.8 |
| 394f8f8 (interim build) | 86.77 | 38.02 | 1.06x | 16.5 |
| v0.8.0 | 77.19 | 28.91 | 1.12x | 14.5 |
| v0.9.0 | 77.87 | 28.89 | 0.99x | 14.5 |
| v0.10.0 | 72.14 | 28.37 | 1.08x | 14.4 |
| v0.11.0 | 65.11 | 26.82 | 1.11x | 14.5 |
| v0.12.0 | 58.97 | 20.24 | 1.10x | 14.6 |
| v0.13.0 | 55.26 | 19.26 | 1.07x | 25.9 |

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
- Agreement with the previous release (v0.12.0) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
