# Thread scaling: bwa-mem3 v0.18.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 482.15 | 100.0% | 3 |
| 2 | 239.97 | 100.5% | 3 |
| 4 | 121.05 | 99.6% | 3 |
| 8 | 60.87 | 99.0% | 3 |
| 16 | 30.46 | 98.9% | 3 |
| 32 | 15.54 | 97.0% | 3 |
| 64 | 9.29 | 81.1% | 3 |
