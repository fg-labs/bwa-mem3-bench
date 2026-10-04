# Thread scaling: bwa-mem3 v0.14.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 818.85 | 100.0% | 3 |
| 2 | 401.56 | 102.0% | 3 |
| 4 | 197.61 | 103.6% | 3 |
| 8 | 98.49 | 103.9% | 3 |
| 16 | 47.97 | 106.7% | 3 |
| 32 | 24.29 | 105.3% | 3 |
| 64 | 13.24 | 96.6% | 3 |
