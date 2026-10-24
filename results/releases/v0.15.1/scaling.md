# Thread scaling: bwa-mem3 v0.15.1

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 598.80 | 100.0% | 3 |
| 2 | 291.01 | 102.9% | 3 |
| 4 | 146.36 | 102.3% | 3 |
| 8 | 70.93 | 105.5% | 3 |
| 16 | 35.43 | 105.6% | 3 |
| 32 | 18.11 | 103.3% | 3 |
| 64 | 9.91 | 94.4% | 3 |
