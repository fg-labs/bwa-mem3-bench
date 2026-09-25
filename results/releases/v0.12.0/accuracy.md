# Accuracy against simulated truth: bwa-mem3 v0.12.0

[All datasets](README.md) · [methodology](../../methodology.md#accuracy)

Reads simulated from GRCh38 with known origins, so each alignment can be graded as correct or not. `MAPQ >= 20` shows how many reads an aligner is confident about, and how often that confidence is wrong. MD and NM columns are the share of variant-bearing reads whose MD/NM tag matches the truth.

## Whole genome, placement (`sim-wgs-place`)

| aligner | correct | mismapped | MAPQ >= 20 | mismapped at MAPQ >= 20 | MD match | NM match | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bwa-mem2 | 95.17% | 4.83% | 92.19% | 0.019% | 94.62% | 99.02% | 5 |
| bwa-mem3 | 95.17% | 4.83% | 92.19% | 0.019% | 94.62% | 99.02% | 5 |
| minibwa | 95.07% | 4.93% | 91.81% | 0.015% | 94.58% | 99.05% | 5 |
| bwa-mem3 --fast | 95.15% | 4.85% | 92.29% | 0.026% | 94.58% | 98.99% | 5 |

## Whole genome, variant-bearing reads (`sim-wgs-vars`)

| aligner | correct | mismapped | MAPQ >= 20 | mismapped at MAPQ >= 20 | MD match | NM match | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bwa-mem2 | 96.11% | 3.89% | 92.75% | 0.021% | 95.46% | 99.09% | 5 |
| bwa-mem3 | 96.11% | 3.89% | 92.75% | 0.021% | 95.46% | 99.09% | 5 |
| minibwa | 96.12% | 3.88% | 92.18% | 0.016% | 95.60% | 99.20% | 5 |
| bwa-mem3 --fast | 96.10% | 3.90% | 92.77% | 0.022% | 95.47% | 99.08% | 5 |

## Methylation, placement (`sim-meth-place`)

| aligner | correct | mismapped | MAPQ >= 20 | mismapped at MAPQ >= 20 | MD match | NM match | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bwameth | 94.66% | 5.34% | 90.51% | 0.023% | 94.50% | 98.02% | 5 |
| bwa-mem3 | 94.65% | 5.35% | 90.41% | 0.022% | 94.49% | 98.00% | 5 |
| minibwa | 94.62% | 5.38% | 91.17% | 0.016% | 94.27% | 97.82% | 5 |
| bwa-mem3 --fast | 94.65% | 5.35% | 90.55% | 0.028% | 94.46% | 97.94% | 5 |

## Methylation, variant-bearing reads (`sim-meth-vars`)

| aligner | correct | mismapped | MAPQ >= 20 | mismapped at MAPQ >= 20 | MD match | NM match | reps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bwameth | 95.41% | 4.59% | 89.50% | 0.023% | 95.18% | 97.90% | 5 |
| bwa-mem3 | 95.41% | 4.59% | 89.43% | 0.022% | 95.18% | 97.89% | 5 |
| minibwa | 95.51% | 4.49% | 91.08% | 0.017% | 95.14% | 97.83% | 5 |
| bwa-mem3 --fast | 95.39% | 4.61% | 89.48% | 0.024% | 95.13% | 97.86% | 5 |
