#!/bin/bash
# Measure this host's memory access rate with tachyon; emit one JSON record on
# stdout.
#
# WHY THIS EXISTS. Wall times on this fleet are not comparable across runs. On
# the v0.9.0 bless the perf gate failed `hic-1M-compat`/m7i at +13.0%, and
# re-running the PREVIOUS release's unchanged binary the same day measured it
# 18.7% slower than its own recorded number — the host had changed, not the code.
# `cpu_time / wall` held at 14.1-14.7 of 16 vCPUs throughout, so no vCPU was
# lost; identical work simply burned ~20% more CPU-seconds waiting on memory that
# co-tenants were consuming. A probe reading recorded next to the timing is what
# lets that be diagnosed at query time rather than re-litigated by hand.
#
# MUST be invoked from inside the rule that does the actual work, for the same
# reason `emit-host-meta` must (see that script's header): only the aligning
# shell is guaranteed to be on the aligning machine. A probe of some other
# instance in the queue is worse than no probe, because it looks like data.
#
# CONTRACT: the reading is diagnostic, never load-bearing. Given well-formed
# arguments this script always writes one valid JSON record and exits 0, however
# hostile the environment (no tachyon, no python3) — because it runs inside a
# rule's `set -e` shell body, where a non-zero exit would abort a ~45-minute
# thread-scaling ladder over a diagnostic. An unavailable probe emits
# `"status": "unavailable"` with null measurements, which reads as "not measured"
# rather than as a fast host. Malformed ARGUMENTS still fail loudly: that is a
# workflow bug, not an environment, and it must not reach a worker unnoticed.
#
# usage: emit-host-probe <phase> [seconds]
set -euo pipefail

readonly USAGE="usage: emit-host-probe <phase> [seconds]"
MIN_ARGS=1
MAX_ARGS=2
if [ "$#" -lt "$MIN_ARGS" ] || [ "$#" -gt "$MAX_ARGS" ]; then
    echo "emit-host-probe: expected $MIN_ARGS-$MAX_ARGS arguments, got $#. $USAGE" >&2
    exit 2
fi

PHASE="$1"
SECONDS_BUDGET="${2:-10}"

# `phase` labels the reading ("pre" / "post" around the timed work) and is part
# of the ingest key, so a malformed one would silently create a bogus row.
if ! [[ "$PHASE" =~ ^[a-z][a-z0-9-]*$ ]]; then
    echo "emit-host-probe: phase must be lowercase alphanumeric/dash; got '$PHASE'. $USAGE" >&2
    exit 2
fi
# Validated here rather than left to tachyon, which would reject it and leave the
# probe degrading to `unavailable` — turning a workflow bug into a silent row of
# nulls, the exact failure mode the CONTRACT's argument/environment split exists
# to prevent. Two patterns: shaped like a number, and not numerically zero.
if ! [[ "$SECONDS_BUDGET" =~ ^[0-9]+([.][0-9]+)?$ ]] || [[ "$SECONDS_BUDGET" =~ ^0+([.]0+)?$ ]]; then
    echo "emit-host-probe: seconds must be a positive number; got '$SECONDS_BUDGET'. $USAGE" >&2
    exit 2
fi

# Build-time provenance for the probe binary. The runtime image has NO Rust
# toolchain, so `rustc --version` cannot be recovered here — it is captured in
# the builder stage and copied in (see docker/Dockerfile). Without it a score
# recorded months apart cannot be told apart from a codegen change in the probe
# itself, which is the whole objection raised on
# fg-labs/bwa-mem3-bench#56.
#
# The location is overridable so the tests can exercise the merge deterministically
# instead of depending on an installed image, exactly as `emit-host-meta` does with
# AWS_EC2_METADATA_SERVICE_ENDPOINT. Production never sets it.
_DEFAULT_PROVENANCE=/usr/local/share/bwa-mem3-bench/tachyon-provenance.json
PROVENANCE="${BWA_MEM3_BENCH_TACHYON_PROVENANCE:-$_DEFAULT_PROVENANCE}"

# The last-resort record: run identity intact, measurement explicitly absent.
#
# Carries EVERY field the healthy record does, all null. Same shape either way, so
# a consumer never has to tell "field absent" apart from "field null" — and
# `working_set_bytes_per_thread` belongs here for exactly that reason, even though
# an unavailable probe has no working set to report.
emit_unavailable() {
    printf '{"phase": "%s", "status": "unavailable", "million_accesses_per_sec": null, ' \
        "$PHASE"
    printf '"ns_per_access": null, "threads": null, '
    printf '"working_set_bytes_per_thread": null, "seconds": null, '
    printf '"probe_version": null, "rustc": null}\n'
}

# Backstop for anything the guards below do not cover, so the CONTRACT holds for
# a failure mode nobody anticipated. Armed only AFTER argument validation, so a
# usage error still fails loudly rather than degrading.
trap 'echo "emit-host-probe: degraded to unavailable after an unexpected error" >&2; \
      emit_unavailable; exit 0' ERR

if ! command -v tachyon >/dev/null 2>&1; then
    echo "emit-host-probe: tachyon not on PATH; degraded to unavailable" >&2
    emit_unavailable
    exit 0
fi

# `|| true` so a probe crash degrades instead of aborting the caller. tachyon's
# own JSON carries version / rate / latency / threads / working set.
#
# stderr is passed through to the caller's log rather than discarded: an
# `unavailable` record says only THAT the probe failed, and tachyon's own message
# is the only thing that says why. Only stdout is captured, so a diagnostic
# cannot corrupt the JSON.
RAW=$(tachyon --seconds "$SECONDS_BUDGET" --json || true)
if [ -z "$RAW" ]; then
    echo "emit-host-probe: tachyon produced no output; degraded to unavailable" >&2
    emit_unavailable
    exit 0
fi

# Buffered, so a writer that dies mid-record cannot emit half a JSON object
# followed by the fallback's whole one.
export PHASE RAW PROVENANCE SECONDS_BUDGET
if PAYLOAD=$(python3 -c '
import json, os, sys

raw = json.loads(os.environ["RAW"])

# rustc is optional: an image built before the provenance file existed still
# yields a usable reading, just an unattributable one.
rustc = None
try:
    with open(os.environ["PROVENANCE"]) as handle:
        rustc = json.load(handle).get("rustc")
except (OSError, ValueError):
    pass

json.dump({
    "phase": os.environ["PHASE"],
    "status": "ok",
    "million_accesses_per_sec": raw.get("million_accesses_per_sec"),
    "ns_per_access": raw.get("ns_per_access"),
    "threads": raw.get("threads"),
    "working_set_bytes_per_thread": raw.get("working_set_bytes_per_thread"),
    "seconds": raw.get("elapsed_s"),
    # tachyon reports its own release in the payload; that is the authority on
    # what was measured, not the version the image intended to install.
    "probe_version": raw.get("version"),
    "rustc": rustc,
}, sys.stdout)
'); then
    # Newline-terminated: callers APPEND these records to a .jsonl, so two
    # readings run back to back must not concatenate into one unparseable line.
    # (`$(...)` strips trailing newlines, so the writer cannot supply it itself.)
    printf '%s\n' "$PAYLOAD"
else
    echo "emit-host-probe: JSON writer failed; degraded to unavailable" >&2
    emit_unavailable
fi
