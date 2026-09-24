# Thread scaling: bwa-mem3 v0.12.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 1011.08 | 100.0% | 3 |
| 2 | 497.99 | 101.5% | 3 |
| 4 | 246.66 | 102.5% | 3 |
| 8 | 122.24 | 103.4% | 3 |
| 16 | 61.03 | 103.5% | 3 |
| 32 | 31.54 | 100.2% | 3 |
| 64 | 16.78 | 94.1% | 3 |
