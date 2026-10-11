# bwa-mem3 v0.14.1 benchmark results

Benchmarked at fg-labs/bwa-mem3 `e974f2666bae714afd942faf58e1cb27078b3fef` (release tag commit `d6745b5924b0b71d861bcadefca67a3ae3e6fad5`), blessed 2026-10-11 ([fg-labs/bwa-mem3#558](https://github.com/fg-labs/bwa-mem3/pull/558)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.14.1>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.14.1 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in its RSS); every other arm used its stock index.

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.1** | 22.63 | 1.00x | 1.73x | 26.7 |
| **bwa-mem3 v0.14.1 `--fast`** | 13.07 | — | 1.00x | 25.6 |
| bwa | 196.03 | 8.66x | 15.00x | 7.4 |
| bwa-mem2 | 85.33 | 3.77x | 6.53x | 21.0 |
| minibwa | 30.38 | 1.34x | 2.32x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 56.35 | — | — | 21.7 |
| v0.2.2 | 55.53 | — | 1.01x | 21.3 |
| v0.3.0 | 52.92 | — | 1.05x | 21.6 |
| v0.4.0 | 52.01 | — | 1.02x | 17.1 |
| v0.5.0 | 51.74 | 22.33 | 1.01x | 17.0 |
| v0.6.0 | 50.20 | 22.33 | 1.03x | 17.0 |
| v0.7.0 | 48.73 | 22.32 | 1.03x | 17.1 |
| 394f8f8 (interim build) | 46.56 | 21.76 | 1.05x | 16.6 |
| v0.8.0 | 43.40 | 18.67 | 1.07x | 14.6 |
| v0.9.0 | 43.37 | 18.18 | 1.00x | 14.6 |
| v0.10.0 | 43.33 | 18.17 | 1.00x | 14.6 |
| v0.11.0 | 41.82 | 17.66 | 1.04x | 14.7 |
| v0.12.0 | 33.09 | 14.64 | 1.26x | 14.8 |
| v0.13.0 | 30.61 | 14.18 | 1.08x | 26.1 |
| v0.14.0 | 26.66 | 13.07 | 1.15x | 26.4 |
| v0.14.1 | 22.63 | 13.07 | 1.18x | 26.7 |

</details>

### m8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.1** | 35.86 | 1.00x | 2.16x | 26.6 |
| **bwa-mem3 v0.14.1 `--fast`** | 16.59 | — | 1.00x | 25.2 |
| bwa | 243.12 | 6.78x | 14.65x | 7.4 |
| minibwa | 40.74 | 1.14x | 2.46x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 122.77 | — | — | 20.9 |
| v0.2.2 | 118.86 | — | 1.03x | 21.7 |
| v0.3.0 | 105.48 | — | 1.13x | 21.2 |
| v0.4.0 | 104.88 | — | 1.01x | 16.7 |
| v0.5.0 | 104.86 | 37.67 | 1.00x | 16.8 |
| v0.6.0 | 98.10 | 37.13 | 1.07x | 16.8 |
| v0.7.0 | 92.25 | 40.23 | 1.06x | 16.8 |
| 394f8f8 (interim build) | 86.57 | 37.45 | 1.07x | 16.5 |
| v0.8.0 | 76.52 | 28.32 | 1.13x | 14.5 |
| v0.9.0 | 76.80 | 28.36 | 1.00x | 14.6 |
| v0.10.0 | 71.43 | 27.83 | 1.08x | 14.4 |
| v0.11.0 | 64.68 | 26.30 | 1.10x | 14.5 |
| v0.12.0 | 59.01 | 19.74 | 1.10x | 14.6 |
| v0.13.0 | 55.17 | 18.70 | 1.07x | 25.9 |
| v0.14.0 | 43.46 | 18.10 | 1.27x | 26.5 |
| v0.14.1 | 35.86 | 16.59 | 1.21x | 26.6 |

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
- Agreement with the previous release (v0.14.0) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
