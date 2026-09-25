# Thread scaling: bwa-mem3 v0.13.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 950.58 | 100.0% | 3 |
| 2 | 468.24 | 101.5% | 3 |
| 4 | 231.43 | 102.7% | 3 |
| 8 | 115.32 | 103.0% | 3 |
| 16 | 57.86 | 102.7% | 3 |
| 32 | 29.90 | 99.3% | 3 |
| 64 | 16.13 | 92.1% | 3 |
