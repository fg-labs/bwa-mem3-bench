# bwa-mem3 benchmark results

Published at every bwa-mem3 release bless. Each release has its own frozen page; this index always leads with the latest. How the numbers are produced is in [methodology.md](methodology.md).

## Latest: v0.15.1

Same-host speed on `wgs-5M` (16 threads), median of the arena reps. Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means bwa-mem3 is faster).

### m8a (AMD EPYC Turin (Zen 5), AVX-512)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.1** | 19.62 | 1.00x | 1.70x | 26.7 |
| **bwa-mem3 v0.15.1 `--fast`** | 11.56 | — | 1.00x | 25.5 |
| bwa | 195.57 | 9.97x | 16.92x | 7.4 |
| bwa-mem2 | 85.92 | 4.38x | 7.43x | 21.1 |
| minibwa | 30.42 | 1.55x | 2.63x | 8.8 |

### m8g (AWS Graviton4, NEON)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.1** | 32.38 | 1.00x | 2.15x | 26.7 |
| **bwa-mem3 v0.15.1 `--fast`** | 15.07 | — | 1.00x | 25.3 |
| bwa | 233.94 | 7.22x | 15.52x | 7.4 |
| minibwa | 40.70 | 1.26x | 2.70x | 8.7 |

Full results: [v0.15.1](releases/v0.15.1/README.md). Per dataset: [wgs-5M](releases/v0.15.1/wgs-5M.md), [wes-5M](releases/v0.15.1/wes-5M.md), [panel-agilent-qxt-5M](releases/v0.15.1/panel-agilent-qxt-5M.md), [hic-1M](releases/v0.15.1/hic-1M.md), [sbx-1M](releases/v0.15.1/sbx-1M.md), [meth-twist-emseq-5M](releases/v0.15.1/meth-twist-emseq-5M.md), [wgs-5M-alt](releases/v0.15.1/wgs-5M-alt.md), [accuracy](releases/v0.15.1/accuracy.md), [thread scaling](releases/v0.15.1/scaling.md).

## All releases

| release | benchmarked at | blessed | max reps per cell |
| --- | --- | --- | --- |
| [v0.15.1](releases/v0.15.1/README.md) | `a6b2ba1e` | 2026-10-24 | 5 |
| [v0.15.0](releases/v0.15.0/README.md) | `cbe2db32` | 2026-10-14 | 5 |
| [v0.14.1](releases/v0.14.1/README.md) | `e974f266` | 2026-10-11 | 5 |
| [v0.14.0](releases/v0.14.0/README.md) | `1a6a9d56` | 2026-10-03 | 5 |
| [v0.13.0](releases/v0.13.0/README.md) | `64420af4` | 2026-09-23 | 5 |
| [v0.12.0](releases/v0.12.0/README.md) | `2e662761` | 2026-09-13 | 5 |
