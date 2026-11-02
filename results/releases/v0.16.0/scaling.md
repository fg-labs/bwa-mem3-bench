# Thread scaling: bwa-mem3 v0.16.0

[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)

One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is *pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with compute, so it understates pure kernel scaling slightly at high thread counts.

## wgs-5M on AWS Graviton4, NEON (64 vCPU)

| threads | PROCESS() (s) | efficiency | reps |
| --- | --- | --- | --- |
| 1 | 487.42 | 100.0% | 3 |
| 2 | 243.36 | 100.1% | 3 |
| 4 | 122.48 | 99.5% | 3 |
| 8 | 61.48 | 99.1% | 3 |
| 16 | 30.82 | 98.8% | 3 |
| 32 | 15.67 | 97.2% | 3 |
| 64 | 9.03 | 84.3% | 3 |
