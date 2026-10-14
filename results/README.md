# bwa-mem3 benchmark results

Published at every bwa-mem3 release bless. Each release has its own frozen page; this index always leads with the latest. How the numbers are produced is in [methodology.md](methodology.md).

## Latest: v0.15.0

Same-host speed on `wgs-5M` (16 threads), median of the arena reps. Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means bwa-mem3 is faster).

### m8a (AMD EPYC Turin (Zen 5), AVX-512)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.0** | 22.83 | 1.00x | 1.89x | 26.5 |
| **bwa-mem3 v0.15.0 `--fast`** | 12.07 | — | 1.00x | 25.6 |
| bwa | 197.47 | 8.65x | 16.36x | 7.4 |
| bwa-mem2 | 84.53 | 3.70x | 7.00x | 21.2 |
| minibwa | 30.44 | 1.33x | 2.52x | 8.8 |

### m8g (AWS Graviton4, NEON)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.15.0** | 36.85 | 1.00x | 2.16x | 26.5 |
| **bwa-mem3 v0.15.0 `--fast`** | 17.08 | — | 1.00x | 25.3 |
| bwa | 255.55 | 6.93x | 14.96x | 7.4 |
| minibwa | 41.68 | 1.13x | 2.44x | 8.7 |

Full results: [v0.15.0](releases/v0.15.0/README.md). Per dataset: [wgs-5M](releases/v0.15.0/wgs-5M.md), [wes-5M](releases/v0.15.0/wes-5M.md), [panel-agilent-qxt-5M](releases/v0.15.0/panel-agilent-qxt-5M.md), [hic-1M](releases/v0.15.0/hic-1M.md), [sbx-1M](releases/v0.15.0/sbx-1M.md), [meth-twist-emseq-5M](releases/v0.15.0/meth-twist-emseq-5M.md), [wgs-5M-alt](releases/v0.15.0/wgs-5M-alt.md), [accuracy](releases/v0.15.0/accuracy.md), [thread scaling](releases/v0.15.0/scaling.md).

## All releases

| release | benchmarked at | blessed | max reps per cell |
| --- | --- | --- | --- |
| [v0.15.0](releases/v0.15.0/README.md) | `cbe2db32` | 2026-10-14 | 5 |
| [v0.14.1](releases/v0.14.1/README.md) | `e974f266` | 2026-10-11 | 5 |
| [v0.14.0](releases/v0.14.0/README.md) | `1a6a9d56` | 2026-10-03 | 5 |
| [v0.13.0](releases/v0.13.0/README.md) | `64420af4` | 2026-09-23 | 5 |
| [v0.12.0](releases/v0.12.0/README.md) | `2e662761` | 2026-09-13 | 5 |
