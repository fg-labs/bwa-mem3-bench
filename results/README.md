# bwa-mem3 benchmark results

Published at every bwa-mem3 release bless. Each release has its own frozen page; this index always leads with the latest. How the numbers are produced is in [methodology.md](methodology.md).

## Latest: v0.12.0

Same-host speed on `wgs-5M` (16 threads), median of the arena reps. Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means bwa-mem3 is faster).

### c8a (AMD EPYC Turin (Zen 5), AVX-512)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.12.0** | 36.99 | 1.00x | 2.35x | 14.1 |
| **bwa-mem3 v0.12.0 `--fast`** | 15.77 | — | 1.00x | 13.7 |
| bwa | 201.79 | 5.46x | 12.80x | 7.4 |
| bwa-mem2 | 99.92 | 2.70x | 6.34x | 21.3 |
| minibwa | 32.15 | 0.87x | 2.04x | 8.7 |

### c8g (AWS Graviton4, NEON)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.12.0** | 59.85 | 1.00x | 2.87x | 14.3 |
| **bwa-mem3 v0.12.0 `--fast`** | 20.86 | — | 1.00x | 13.4 |
| bwa | 245.88 | 4.11x | 11.79x | 7.4 |
| minibwa | 41.65 | 0.70x | 2.00x | 8.7 |

Full results: [v0.12.0](releases/v0.12.0/README.md). Per dataset: [wgs-5M](releases/v0.12.0/wgs-5M.md), [wes-5M](releases/v0.12.0/wes-5M.md), [panel-agilent-qxt-5M](releases/v0.12.0/panel-agilent-qxt-5M.md), [hic-1M](releases/v0.12.0/hic-1M.md), [sbx-1M](releases/v0.12.0/sbx-1M.md), [meth-twist-emseq-5M](releases/v0.12.0/meth-twist-emseq-5M.md), [wgs-5M-alt](releases/v0.12.0/wgs-5M-alt.md), [accuracy](releases/v0.12.0/accuracy.md), [thread scaling](releases/v0.12.0/scaling.md).

## All releases

| release | benchmarked at | blessed | reps per cell |
| --- | --- | --- | --- |
| [v0.12.0](releases/v0.12.0/README.md) | `2e662761` | 2026-09-13 | 5 |
