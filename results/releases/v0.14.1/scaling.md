# Thread scaling: bwa-mem3 v0.14.1

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 718.18 | 100.0% | 3 |
| 2 | 349.80 | 102.7% | 3 |
| 4 | 169.61 | 105.9% | 3 |
| 8 | 82.88 | 108.3% | 3 |
| 16 | 41.07 | 109.3% | 3 |
| 32 | 21.04 | 106.7% | 3 |
| 64 | 12.28 | 91.4% | 3 |
