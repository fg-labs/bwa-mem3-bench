#!/bin/bash
# Emit host + run identity as JSON on stdout.
#
# MUST be invoked from inside the rule that does the actual work, never as a
# separate snakemake rule. The previous `emit_meta` rule was in `localrules`, so
# it ran on the COORDINATOR and recorded the coordinator's identity: a job on the
# m7i queue reported `instance_type: c6a.large`, and two different m7i workers
# reported the same instance_id. That is the submitting host, not the aligning
# host — useless for attributing a timing difference to a machine.
#
# Moving it out of localrules would not have helped either: it would then be its
# own Batch job on some other arbitrary instance in the queue. Only running it in
# the same shell body as the aligner guarantees the right machine.
#
# CONTRACT: metadata is diagnostic, never load-bearing. Given well-formed
# arguments this script always writes one valid JSON record and exits 0, however
# hostile the environment (no IMDS, no curl, no python3) — because it runs inside
# the align rule's `set -e` shell body, where a non-zero exit would abort the
# alignment before any work starts. Malformed ARGUMENTS still fail loudly: that
# is a workflow bug, not an environment, and it cannot reach a worker unnoticed.
#
# usage: emit-host-meta <sha> <sample> <arch> <rep>
set -euo pipefail

readonly USAGE="usage: emit-host-meta <sha> <sample> <arch> <rep>"
EXPECTED_ARGS=4
if [ "$#" -ne "$EXPECTED_ARGS" ]; then
    echo "emit-host-meta: expected $EXPECTED_ARGS arguments, got $#. $USAGE" >&2
    exit 2
fi

SHA="$1"; SAMPLE="$2"; ARCH="$3"; REP="$4"

# `rep` is emitted as a JSON *number*, so a non-numeric value would produce a
# record no downstream parser can read. Reject it here rather than in the writer.
if ! [[ "$REP" =~ ^[0-9]+$ ]]; then
    echo "emit-host-meta: rep must be a non-negative integer; got '$REP'. $USAGE" >&2
    exit 2
fi

# The last-resort record: every host field unknown, run identity intact. Used
# when the environment cannot be interrogated at all (see CONTRACT above).
#
# `measured_at` is the one field NOT hardcoded, because it is the one this
# function can still know. It is assigned below before the ERR trap is armed and
# before the JSON writer runs -- the only two paths that reach here -- whereas
# the four host fields may genuinely be unset if the trap fires early. Emitting
# the sentinel anyway would discard a good stamp and push `late_cells` onto the
# artifact-mtime fallback. `:-` guards the read regardless, under `set -u`.
emit_fallback() {
    printf '{"fg_labs_sha": "%s", "sample": "%s", "arch": "%s", "rep": %s, ' \
        "$SHA" "$SAMPLE" "$ARCH" "$REP"
    printf '"instance_type": "unknown", "availability_zone": "unknown", '
    printf '"instance_id": "unknown", "kernel": "unknown", '
    printf '"measured_at": "%s"}' "${MEASURED_AT:-unknown}"
}

# WHEN this cell was measured, in UTC. Not a nicety: artifacts from more than one
# occasion can share a run prefix, and until this existed nothing in the record
# could tell them apart. The v0.8.0 golden's S3 tree holds 23 cells from a control
# run taken THREE DAYS after the release was benched; collecting that SHA folds
# them into the release's medians, which is what every future perf gate compares
# against. Separating them took a manual S3-timestamp audit.
#
# `date -u` and not the shell's own clock, because the reader compares this across
# hosts in different regions. Seconds resolution is ample for a question measured
# in hours. If `date` is unavailable only this field degrades, to the `unknown`
# sentinel: the `|| echo` keeps the substitution successful, and in any case this
# runs before the ERR trap below is armed, so the rest of the record still stands
# either way. The reader then falls back to the artifact's mtime.
MEASURED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)

# Backstop for anything the individual guards below do not cover, so the CONTRACT
# holds even for a failure mode nobody anticipated. Armed only AFTER argument
# validation, so a usage error still fails loudly rather than degrading.
trap 'echo "emit-host-meta: degraded to unknown after an unexpected error" >&2; \
      emit_fallback; exit 0' ERR

# IMDSv2. AL2023 rejects tokenless IMDSv1, and `curl -s` ignores the HTTP status
# and exits 0 — which is how the original version silently wrote empty strings
# for the entire history of the project. `-f` makes an HTTP error a non-zero
# exit so the `|| echo unknown` fallbacks actually fire.
#
# Reaching IMDS from inside the container also needs HttpPutResponseHopLimit >= 2
# on the launch template (see cdk/stacks/batch_stack.py); at the default of 1 the
# token PUT is dropped before it arrives.
#
# The endpoint honours AWS_EC2_METADATA_SERVICE_ENDPOINT, the same variable the
# AWS CLI and SDKs use. Production never sets it; the tests do, so the degraded
# path is exercised deterministically instead of depending on whether the machine
# running them happens to sit on EC2.
IMDS="${AWS_EC2_METADATA_SERVICE_ENDPOINT:-http://169.254.169.254}/latest"
TOKEN=$(curl -sf -X PUT --max-time 2 "$IMDS/api/token" \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)

imds_get() {
    if [ -n "$TOKEN" ]; then
        curl -sf --max-time 2 -H "X-aws-ec2-metadata-token: $TOKEN" \
            "$IMDS/meta-data/$1" || echo unknown
    else
        echo unknown
    fi
}

INSTANCE_TYPE=$(imds_get instance-type)
AZ=$(imds_get placement/availability-zone)
# instance_id is the field that makes attribution possible: it distinguishes
# "these two reps shared a host and therefore contended" from "these two reps
# merely ran in the same AZ".
INSTANCE_ID=$(imds_get instance-id)
KERNEL=$(uname -r 2>/dev/null || echo unknown)

# Buffered, so a writer that dies mid-record cannot emit half a JSON object
# followed by the fallback's whole one.
export SHA SAMPLE ARCH REP INSTANCE_TYPE AZ INSTANCE_ID KERNEL MEASURED_AT
if PAYLOAD=$(python3 -c '
import json, os, sys
json.dump({
    "fg_labs_sha": os.environ["SHA"],
    "sample": os.environ["SAMPLE"],
    "arch": os.environ["ARCH"],
    "rep": int(os.environ["REP"]),
    "instance_type": os.environ["INSTANCE_TYPE"],
    "availability_zone": os.environ["AZ"],
    "instance_id": os.environ["INSTANCE_ID"],
    "kernel": os.environ["KERNEL"],
    "measured_at": os.environ["MEASURED_AT"],
}, sys.stdout)
'); then
    printf '%s' "$PAYLOAD"
else
    echo "emit-host-meta: JSON writer failed; degraded to unknown" >&2
    emit_fallback
fi
