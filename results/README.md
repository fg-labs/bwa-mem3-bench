# bwa-mem3 benchmark results

Published at every bwa-mem3 release bless. Each release has its own frozen page; this index always leads with the latest. How the numbers are produced is in [methodology.md](methodology.md).

## Latest: v0.13.0

Same-host speed on `wgs-5M` (16 threads), median of the arena reps. Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means bwa-mem3 is faster).

### m8a (AMD EPYC Turin (Zen 5), AVX-512)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.13.0** | 30.57 | 1.00x | 2.16x | 26.1 |
| **bwa-mem3 v0.13.0 `--fast`** | 14.16 | — | 1.00x | 25.0 |
| bwa | 197.78 | 6.47x | 13.97x | 7.4 |
| bwa-mem2 | 84.09 | 2.75x | 5.94x | 21.2 |
| minibwa | 30.38 | 0.99x | 2.15x | 8.8 |

### m8g (AWS Graviton4, NEON)

| aligner | median wall (s) | bwa-mem3 speedup | `--fast` speedup | peak RSS (GB) |
| --- | --- | --- | --- | --- |
| **bwa-mem3 v0.13.0** | 55.26 | 1.00x | 2.87x | 25.9 |
| **bwa-mem3 v0.13.0 `--fast`** | 19.26 | — | 1.00x | 24.8 |
| bwa | 243.30 | 4.40x | 12.63x | 7.4 |
| minibwa | 41.14 | 0.74x | 2.14x | 8.8 |

Full results: [v0.13.0](releases/v0.13.0/README.md). Per dataset: [wgs-5M](releases/v0.13.0/wgs-5M.md), [wes-5M](releases/v0.13.0/wes-5M.md), [panel-agilent-qxt-5M](releases/v0.13.0/panel-agilent-qxt-5M.md), [hic-1M](releases/v0.13.0/hic-1M.md), [sbx-1M](releases/v0.13.0/sbx-1M.md), [meth-twist-emseq-5M](releases/v0.13.0/meth-twist-emseq-5M.md), [wgs-5M-alt](releases/v0.13.0/wgs-5M-alt.md), [accuracy](releases/v0.13.0/accuracy.md), [thread scaling](releases/v0.13.0/scaling.md).

## All releases

| release | benchmarked at | blessed | reps per cell |
| --- | --- | --- | --- |
| [v0.13.0](releases/v0.13.0/README.md) | `64420af4` | 2026-09-23 | 5 |
| [v0.12.0](releases/v0.12.0/README.md) | `2e662761` | 2026-09-13 | 5 |
