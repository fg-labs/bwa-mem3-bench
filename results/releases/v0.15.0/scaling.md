# Thread scaling: bwa-mem3 v0.15.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 660.14 | 100.0% | 3 |
| 2 | 320.83 | 102.9% | 3 |
| 4 | 156.23 | 105.6% | 3 |
| 8 | 77.22 | 106.9% | 3 |
| 16 | 38.28 | 107.8% | 3 |
| 32 | 19.65 | 105.0% | 3 |
| 64 | 11.88 | 86.8% | 3 |
