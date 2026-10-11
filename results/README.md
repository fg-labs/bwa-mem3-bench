# bwa-mem3 benchmark results

Published at every bwa-mem3 release bless. Each release has its own frozen page; this index always leads with the latest. How the numbers are produced is in [methodology.md](methodology.md).

## Latest: v0.14.1

Same-host speed on `wgs-5M` (16 threads), median of the arena reps. Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means bwa-mem3 is faster).

### m8a (AMD EPYC Turin (Zen 5), AVX-512)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.1** | 22.63 | 1.00x | 1.73x | 26.7 |
| **bwa-mem3 v0.14.1 `--fast`** | 13.07 | — | 1.00x | 25.6 |
| bwa | 196.03 | 8.66x | 15.00x | 7.4 |
| bwa-mem2 | 85.33 | 3.77x | 6.53x | 21.0 |
| minibwa | 30.38 | 1.34x | 2.32x | 8.8 |

### m8g (AWS Graviton4, NEON)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.14.1** | 35.86 | 1.00x | 2.16x | 26.6 |
| **bwa-mem3 v0.14.1 `--fast`** | 16.59 | — | 1.00x | 25.2 |
| bwa | 243.12 | 6.78x | 14.65x | 7.4 |
| minibwa | 40.74 | 1.14x | 2.46x | 8.8 |

Full results: [v0.14.1](releases/v0.14.1/README.md). Per dataset: [wgs-5M](releases/v0.14.1/wgs-5M.md), [wes-5M](releases/v0.14.1/wes-5M.md), [panel-agilent-qxt-5M](releases/v0.14.1/panel-agilent-qxt-5M.md), [hic-1M](releases/v0.14.1/hic-1M.md), [sbx-1M](releases/v0.14.1/sbx-1M.md), [meth-twist-emseq-5M](releases/v0.14.1/meth-twist-emseq-5M.md), [wgs-5M-alt](releases/v0.14.1/wgs-5M-alt.md), [accuracy](releases/v0.14.1/accuracy.md), [thread scaling](releases/v0.14.1/scaling.md).

## All releases

| release | benchmarked at | blessed | reps per cell |
| --- | --- | --- | --- |
| [v0.14.1](releases/v0.14.1/README.md) | `e974f266` | 2026-10-11 | 5 |
| [v0.14.0](releases/v0.14.0/README.md) | `1a6a9d56` | 2026-10-03 | 5 |
| [v0.13.0](releases/v0.13.0/README.md) | `64420af4` | 2026-09-23 | 5 |
| [v0.12.0](releases/v0.12.0/README.md) | `2e662761` | 2026-09-13 | 5 |
