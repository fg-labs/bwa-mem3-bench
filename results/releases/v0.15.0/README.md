# bwa-mem3 v0.15.0 benchmark results

Benchmarked at fg-labs/bwa-mem3 `cbe2db325cf057eff03a7ffb3ebd629453d0fd49` (release tag commit `7754eccedc97fe4a1833b9b59413f841a4b6ac35`), blessed 2026-10-14 ([fg-labs/bwa-mem3#585](https://github.com/fg-labs/bwa-mem3/pull/585)).
Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/v0.15.0>.

Comparators: bwa 0.7.19, bwa-mem2 v2.2.1, bwameth 0.2.7, minibwa `d6d9f87d`.

How these numbers were produced, and what each comparison means, is in [the methodology page](../../methodology.md).

## Headline: same-host speed (arena)

Every aligner below ran interleaved on one dedicated on-demand host per architecture, aligning `wgs-5M` with 16 threads, so these ratios are like-for-like. Speedup is the other aligner's wall time divided by bwa-mem3's: above 1 means bwa-mem3 is faster. bwa-mem3 v0.15.0 used a denser stride-2 suffix-array index (byte-identical output, more memory, reflected in its RSS); every other arm used its stock index.

### m8a (AMD EPYC Turin (Zen 5), AVX-512), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.0** | 22.83 | 1.00x | 1.89x | 26.5 |
| **bwa-mem3 v0.15.0 `--fast`** | 12.07 | — | 1.00x | 25.6 |
| bwa | 197.47 | 8.65x | 16.36x | 7.4 |
| bwa-mem2 | 84.53 | 3.70x | 7.00x | 21.2 |
| minibwa | 30.44 | 1.33x | 2.52x | 8.8 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 60.31 | — | — | 20.7 |
| v0.2.2 | 57.24 | — | 1.05x | 20.9 |
| v0.3.0 | 53.47 | — | 1.07x | 21.6 |
| v0.4.0 | 53.20 | — | 1.01x | 16.6 |
| v0.5.0 | 52.33 | 22.85 | 1.02x | 16.8 |
| v0.6.0 | 52.66 | 22.35 | 0.99x | 17.0 |
| v0.7.0 | 48.98 | 22.36 | 1.08x | 17.1 |
| 394f8f8 (interim build) | 47.07 | 21.77 | 1.04x | 16.6 |
| v0.8.0 | 45.01 | 18.67 | 1.05x | 14.6 |
| v0.9.0 | 44.70 | 18.19 | 1.01x | 14.4 |
| v0.10.0 | 43.47 | 18.68 | 1.03x | 14.6 |
| v0.11.0 | 42.25 | 17.66 | 1.03x | 14.7 |
| v0.12.0 | 33.18 | 14.65 | 1.27x | 14.8 |
| v0.13.0 | 31.10 | 14.16 | 1.07x | 25.9 |
| v0.14.0 | 25.13 | 13.58 | 1.24x | 26.4 |
| v0.14.1 | 24.11 | 13.08 | 1.04x | 26.3 |
| v0.15.0 | 22.83 | 12.07 | 1.06x | 26.5 |

</details>

### m8g (AWS Graviton4, NEON), 3 reps, median

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.0** | 36.85 | 1.00x | 2.16x | 26.5 |
| **bwa-mem3 v0.15.0 `--fast`** | 17.08 | — | 1.00x | 25.3 |
| bwa | 255.55 | 6.93x | 14.96x | 7.4 |
| minibwa | 41.68 | 1.13x | 2.44x | 8.7 |

<details><summary>Every bwa-mem3 release on this host</summary>

| release | median wall (s) | `--fast` wall (s) | vs previous row | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| v0.2.1 | 129.89 | — | — | 20.7 |
| v0.2.2 | 120.02 | — | 1.08x | 21.0 |
| v0.3.0 | 106.00 | — | 1.13x | 21.9 |
| v0.4.0 | 105.08 | — | 1.01x | 16.8 |
| v0.5.0 | 105.40 | 37.99 | 1.00x | 16.8 |
| v0.6.0 | 100.43 | 37.20 | 1.05x | 16.8 |
| v0.7.0 | 93.35 | 40.90 | 1.08x | 16.8 |
| 394f8f8 (interim build) | 87.91 | 37.54 | 1.06x | 16.5 |
| v0.8.0 | 76.88 | 28.38 | 1.14x | 14.5 |
| v0.9.0 | 78.03 | 28.93 | 0.99x | 14.6 |
| v0.10.0 | 72.60 | 27.88 | 1.07x | 14.4 |
| v0.11.0 | 64.74 | 27.35 | 1.12x | 14.5 |
| v0.12.0 | 59.02 | 19.82 | 1.10x | 14.6 |
| v0.13.0 | 56.24 | 19.80 | 1.05x | 25.9 |
| v0.14.0 | 44.06 | 18.61 | 1.28x | 26.3 |
| v0.14.1 | 36.04 | 17.60 | 1.22x | 26.6 |
| v0.15.0 | 36.85 | 17.08 | 0.98x | 26.5 |

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
- Agreement with the previous release (v0.14.1) is on each dataset page.
- Raw aggregates: [`data.json`](data.json)
