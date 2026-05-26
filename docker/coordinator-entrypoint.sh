#!/usr/bin/env bash
# coordinator-entrypoint.sh — run inside the coordinator Batch job.
#
# Required environment variables (set by the Batch job definition or overrides):
#   FG_LABS_SHA    — fg-labs/bwa-mem3 commit SHA; must already be built + pushed to ECR.
#   TARGET         — Snakemake target (e.g. smoke, all, baseline_all). Default: smoke.
#
# Optional environment variables:
#   ARCHS          — comma-separated arch subset (e.g. c8g,c7g). Empty = core_arch.
#   REPS           — replicate count. Empty = Snakemake config default.
#   SAMPLES        — comma-separated sample subset. Empty = all.
#   IMAGE_TAG      — Docker image tag passed through to snakemake's
#                    `image_tag` config. Auto-derived from FG_LABS_SHA only
#                    when BUILD_VARIANT is set; unset otherwise.
#                    NOTE: no current worker rule
#                    consumes this config — per-rule images are derived from
#                    `fg_labs_sha` via `image_for_arch` in workflow/Snakefile.
#                    Useful for log inspection / debugging only until
#                    `image_tag` is wired into the per-rule image fallback.
#   BUILD_VARIANT  — non-default fg-labs/bwa-mem3 Makefile target (e.g.
#                    `lto-build`). When set, both the image tag and the
#                    snakemake `fg_labs_sha` config are suffixed
#                    `-<build_variant>`, so workers pull the right image
#                    AND the run namespace under `s3://.../runs/<sha>/`
#                    does not collide with a same-SHA default-build run.
#
# Snakemake AWS Batch plugin reads:
#   SNAKEMAKE_AWS_BATCH_REGION    — set by job definition.
#   SNAKEMAKE_AWS_BATCH_JOB_ROLE  — set by job definition (fallback if profile omits it).
set -euo pipefail

: "${FG_LABS_SHA:?FG_LABS_SHA is required}"
: "${TARGET:=smoke}"

# Derive composite IDs from BUILD_VARIANT before constructing CONFIG_ARGS so
# every downstream snakemake config sees a consistent set. Keeping the
# composition in this single place (vs duplicating in submit.py / Snakefile)
# means a future variant-naming change is one edit.
if [[ -n "${BUILD_VARIANT:-}" ]]; then
    FG_LABS_SHA="${FG_LABS_SHA}-${BUILD_VARIANT}"
    # Honor an explicit IMAGE_TAG override if the caller set one (e.g. for
    # debugging against a manually-tagged image); otherwise derive.
    : "${IMAGE_TAG:=${FG_LABS_SHA}}"
fi

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
