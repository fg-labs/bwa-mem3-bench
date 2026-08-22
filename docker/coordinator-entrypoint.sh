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
#   GOLDEN_REF_SHA — pinned previous-release SHA for the vs-golden (Gate #2)
#                    comparison. Empty = vs-golden disabled.
#   IMAGE_TAG      — Docker image tag passed through to snakemake's
#                    `image_tag` config. Auto-derived from FG_LABS_SHA only
#                    when BUILD_VARIANT is set; unset otherwise. Every
#                    per-rule image (`image_for_arch` in workflow/Snakefile,
#                    via `resolve_worker_image_sha`) uses this in place of
#                    `fg_labs_sha` when set, e.g. to pull a manually-tagged
#                    debug image while the run still writes outputs under
#                    its own `fg_labs_sha` S3 namespace.
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

# An array, not a space-separated string: every value here arrives from a Batch
# env override, and an unquoted expansion at the call site would both re-split
# them on whitespace and glob any `*` against the coordinator's working
# directory. The FORCERUN list below is exposed to the same thing and guards it
# the same way.
CONFIG_ARGS=("fg_labs_sha=${FG_LABS_SHA}")
[[ -n "${ARCHS:-}" ]]      && CONFIG_ARGS+=("archs=${ARCHS}")
[[ -n "${REPS:-}" ]]       && CONFIG_ARGS+=("reps=${REPS}")
[[ -n "${SAMPLES:-}" ]]    && CONFIG_ARGS+=("samples=${SAMPLES}")
[[ -n "${GOLDEN_REF_SHA:-}" ]] && CONFIG_ARGS+=("golden_ref_sha=${GOLDEN_REF_SHA}")
# Ad-hoc thread-scaling ladder, e.g. LADDER="16:3,32:3,64:3" to probe only
# the high thread counts. Omitting the 1-thread rung means no efficiency is
# computable, so Gate #3 no-ops for that run by design.
[[ -n "${LADDER:-}" ]]     && CONFIG_ARGS+=("ladder=${LADDER}")
[[ -n "${IMAGE_TAG:-}" ]]  && CONFIG_ARGS+=("image_tag=${IMAGE_TAG}")
# Thread the bucket through snakemake config so worker jobs resolve it too.
# Workers re-parse the Snakefile but their job definitions don't carry this
# env, so without this the golden listing (golden_backed_samples) falls back to
# a wrong default bucket and a golden-gated run aborts. `--config` propagates to
# workers (the job-def env does not), so this is the reliable channel.
[[ -n "${BWA_MEM3_BENCH_S3_BUCKET:-}" ]] && CONFIG_ARGS+=("s3_bucket=${BWA_MEM3_BENCH_S3_BUCKET}")

# Render the Snakemake AWS Batch profile from its template using the
# BWA_MEM3_BENCH_{ECR_REPO,S3_BUCKET} env vars baked into the coordinator
# Batch job definition (see cdk/stacks/batch_stack.py). Optional
# BWA_MEM3_BENCH_COST_CENTER adds a CostCenter tag to spawned worker jobs.
python -m bwa_mem3_bench.cli render-profile \
    --template /opt/workflow/profiles/aws-batch.config.yaml.template \
    --output /opt/workflow/profiles/aws-batch/config.yaml

# --cores is REQUIRED here, and must be large. Snakemake clamps every rule's
# `threads` to the core count, and when --cores is omitted it resolves to the
# LOCAL core count — which on this coordinator is a c6a.large, i.e. 2 vCPUs.
# Our snakemake-executor-plugin-aws-batch fork derives each worker's Batch VCPU
# requirement from `threads`, so without this every 16-thread alignment was
# submitted as a 2-vCPU job and Batch happily packed several onto one host.
#
# Observed directly: with this flag absent, `align_fg_labs` (threads: 16) was
# submitted as VCPU=2 on a real run, even though a local `--dry-run` on a
# 12-core laptop reported `threads: 16` — dry-run does not apply the clamp, so
# this cannot be caught without submitting a real job.
#
# The value only governs the coordinator's own resource accounting; actual
# worker concurrency is capped by `jobs:` in the profile. It must exceed the
# largest `threads:` any rule declares (currently 64, the thread-scaling
# ladder), so pick a value well clear of that.
# Optional `--forcerun <rules>`. Snakemake skips a rule whose outputs already
# exist, and its rerun-triggers watch the rule's own definition -- not the
# BINARIES inside the image. So re-deriving artifacts for a SHA that is already
# aligned (e.g. after a compare-bams change) needs the rules named explicitly,
# or the coordinator runs and does nothing.
#
# Deliberately a space-separated rule list rather than a boolean: a bare
# `--forcerun` means "force EVERYTHING", which on an aligned SHA would re-run
# the whole alignment sweep. Unset adds no flag at all.
#
# Split with `read -ra` rather than an unquoted expansion. Both split on IFS,
# but only the unquoted form also does pathname expansion, and the coordinator's
# working directory is not empty -- a rule list containing `*` would arrive at
# snakemake as filenames. `read -ra` also collapses a whitespace-only value to
# zero words, so the `-n` guard alone is not enough: it is true for a string of
# spaces, which would then emit the bare `--forcerun` this list exists to avoid.
#
# Newlines are folded to spaces first because `read` stops at the first one,
# which would silently drop every rule after it -- the unquoted form split on
# newlines too (they are in the default IFS), so this keeps that behavior.
FORCERUN_ARGS=()
if [[ -n "${FORCERUN:-}" ]]; then
    read -ra forcerun_rules <<<"${FORCERUN//$'\n'/ }"
    if ((${#forcerun_rules[@]} > 0)); then
        FORCERUN_ARGS=(--forcerun "${forcerun_rules[@]}")
    fi
fi

exec snakemake \
    -s /opt/workflow/Snakefile \
    --profile /opt/workflow/profiles/aws-batch \
    --cores 256 \
    --config "${CONFIG_ARGS[@]}" \
    "${FORCERUN_ARGS[@]}" \
    -- "${TARGET}"
