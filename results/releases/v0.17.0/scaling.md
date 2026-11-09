# Thread scaling: bwa-mem3 v0.17.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 524.82 | 100.0% | 3 |
| 2 | 256.33 | 102.4% | 3 |
| 4 | 126.96 | 103.3% | 3 |
| 8 | 62.96 | 104.2% | 3 |
| 16 | 31.28 | 104.9% | 3 |
| 32 | 15.79 | 103.9% | 3 |
| 64 | 9.40 | 87.2% | 3 |
