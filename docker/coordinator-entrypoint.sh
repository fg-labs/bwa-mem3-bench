#!/usr/bin/env bash
# coordinator-entrypoint.sh — run inside the coordinator Batch job.
#
# Required environment variables (set by the Batch job definition or overrides):
#   FG_LABS_SHA   — fg-labs/bwa-mem2 commit SHA; must already be built + pushed to ECR.
#   TARGET        — Snakemake target (e.g. smoke, all, baseline_all). Default: smoke.
#
# Optional environment variables:
#   ARCHS         — comma-separated arch subset (e.g. c8g,c7g). Empty = core_arch.
#   REPS          — replicate count. Empty = Snakemake config default.
#   SAMPLES       — comma-separated sample subset. Empty = all.
#   IMAGE_TAG     — Docker image tag for child jobs. Default: same as FG_LABS_SHA.
#
# Snakemake AWS Batch plugin reads:
#   SNAKEMAKE_AWS_BATCH_REGION    — set by job definition.
#   SNAKEMAKE_AWS_BATCH_JOB_ROLE  — set by job definition (fallback if profile omits it).
set -euo pipefail

: "${FG_LABS_SHA:?FG_LABS_SHA is required}"
: "${TARGET:=smoke}"

CONFIG_ARGS="fg_labs_sha=${FG_LABS_SHA}"
[[ -n "${ARCHS:-}" ]]      && CONFIG_ARGS="${CONFIG_ARGS} archs=${ARCHS}"
[[ -n "${REPS:-}" ]]       && CONFIG_ARGS="${CONFIG_ARGS} reps=${REPS}"
[[ -n "${SAMPLES:-}" ]]    && CONFIG_ARGS="${CONFIG_ARGS} samples=${SAMPLES}"
[[ -n "${IMAGE_TAG:-}" ]]  && CONFIG_ARGS="${CONFIG_ARGS} image_tag=${IMAGE_TAG}"

# Render the Snakemake AWS Batch profile from its template using the
# BWA_MEM3_BENCH_{ECR_REPO,S3_BUCKET} env vars baked into the coordinator
# Batch job definition (see cdk/stacks/batch_stack.py). Optional
# BWA_MEM3_BENCH_COST_CENTER adds a CostCenter tag to spawned worker jobs.
python -m bwa_mem3_bench.cli render-profile \
    --template /opt/workflow/profiles/aws-batch/config.yaml.template \
    --output /opt/workflow/profiles/aws-batch/config.yaml

exec snakemake \
    -s /opt/workflow/Snakefile \
    --profile /opt/workflow/profiles/aws-batch \
    --config ${CONFIG_ARGS} \
    -- "${TARGET}"
