# bwa-mem3 v0.16.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `0a65bec1f47f8b773cf40b85d16833de010c8107` (release tag commit `fa688f0bc019968844073ae7d3fe124cf66d0011`), blessed 2026-11-02 ([fg-labs/bwa-mem3#637](https://github.com/fg-labs/bwa-mem3/pull/637)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.16.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.16.0 and releases v0.13.0 to v0.15.1 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in their RSS); v0.12.0 and older releases, and the other aligners, used their stock index.

Every row, earlier releases included, was re-measured in this release's arena run, so the rows compare with each other but not with the walls on another release's page: the arena's method has changed between releases (see [the methodology page](../../methodology.md)).

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.16.0** | 16.36 | 1.00x | 1.81x | 26.8 |
| **bwa-mem3 v0.16.0 `--fast`** | 9.05 | — | 1.00x | 25.6 |
| bwa | 198.77 | 12.15x | 21.96x | 7.4 |
| bwa-mem2 | 88.57 | 5.41x | 9.79x | 21.1 |
| minibwa | 30.10 | 1.84x | 3.33x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 54.58 | — | — | 22.6 |
| v0.2.2 | 52.41 | — | 1.04x (context) | 22.6 |
| v0.3.0 | 49.07 | — | 1.07x (context) | 23.0 |
| v0.4.0 | 51.14 | — | 0.96x (context) | 17.1 |
| v0.5.0 | 51.07 | 21.97 | 1.00x (context) | 17.0 |
| v0.6.0 | 49.97 | 21.53 | 1.02x (context) | 17.1 |
| v0.7.0 | 47.96 | 21.87 | 1.04x (context) | 17.1 |
| 394f8f8 (interim build) | 46.01 | 21.28 | 1.04x (context) | 16.6 |
| v0.8.0 | 43.05 | 17.81 | 1.07x (context) | 14.7 |
| v0.9.0 | 42.73 | 17.77 | 1.01x (context) | 14.7 |
| v0.10.0 | 42.68 | 17.88 | 1.00x (context) | 14.7 |
| v0.11.0 | 41.38 | 17.10 | 1.03x (context) | 14.7 |
| v0.12.0 | 32.61 | 14.13 | 1.27x (context) | 14.8 |
| v0.13.0 | 29.56 | 13.47 | 1.10x (unpaired) | 26.2 |
| v0.14.0 | 23.46 | 12.43 | 1.26x (same-round) | 26.8 |
| v0.14.1 | 22.04 | 12.14 | 1.07x (same-round) | 26.7 |
| v0.15.0 | 20.68 | 11.41 | 1.06x (same-round) | 26.7 |
| v0.15.1 | 18.65 | 10.60 | 1.11x (same-round) | 26.8 |
| v0.16.0 | 16.36 | 9.05 | 1.15x | 26.8 |

</details>

### m8g (AWS Graviton4, NEON), 5 paired rounds (comparators 2), median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.16.0** | 28.72 | 1.00x | 2.30x | 26.8 |
| **bwa-mem3 v0.16.0 `--fast`** | 12.46 | — | 1.00x | 25.6 |
| bwa | 234.36 | 8.16x | 18.81x | 7.4 |
| minibwa | 40.35 | 1.40x | 3.24x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 120.02 | — | — | 22.3 |
| v0.2.2 | 117.51 | — | 1.02x (context) | 22.3 |
| v0.3.0 | 101.31 | — | 1.16x (context) | 22.7 |
| v0.4.0 | 103.46 | — | 0.98x (context) | 16.8 |
| v0.5.0 | 103.58 | 36.50 | 1.00x (context) | 16.8 |
| v0.6.0 | 96.61 | 35.67 | 1.07x (context) | 16.8 |
| v0.7.0 | 91.00 | 39.04 | 1.06x (context) | 16.9 |
| 394f8f8 (interim build) | 85.50 | 36.29 | 1.06x (context) | 16.5 |
| v0.8.0 | 75.74 | 27.51 | 1.13x (context) | 14.5 |
| v0.9.0 | 75.81 | 27.63 | 1.00x (context) | 14.5 |
| v0.10.0 | 70.30 | 26.89 | 1.08x (context) | 14.4 |
| v0.11.0 | 63.38 | 25.36 | 1.11x (context) | 14.5 |
| v0.12.0 | 57.63 | 18.55 | 1.10x (context) | 14.6 |
| v0.13.0 | 53.91 | 17.86 | 1.07x (unpaired) | 25.9 |
| v0.14.0 | 40.99 | 16.94 | 1.31x (same-round) | 26.7 |
| v0.14.1 | 34.56 | 15.72 | 1.19x (same-round) | 26.7 |
| v0.15.0 | 33.82 | 15.11 | 1.02x (same-round) | 26.9 |
| v0.15.1 | 31.29 | 14.08 | 1.08x (same-round) | 26.8 |
| v0.16.0 | 28.72 | 12.46 | 1.09x | 26.8 |

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
- Agreement with the previous release (v0.15.1) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
