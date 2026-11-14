"""Serialization benchmark: bwa-mem3's native BAM writer against the SAM-to-samtools route.

`serialization_fidelity` checks, per (sha, arch, sample, mode), that the native BAM (`--bam=0`
and `--bam=1`) holds the same records as the SAM route, and that no route lost a read.
`serialization_timing` times the native and SAM routes beside bwa, minibwa and upstream bwa-mem2
on the arena's on-demand hosts, one job per (sha, arch, sample, endpoint). See
docs/SERIALIZATION.md for the endpoints, settings and outputs.
"""

import json
import re
import shlex

import yaml
from snakemake.exceptions import WorkflowError

from bwa_mem3_bench.serialization_arms import (
    ENDPOINTS as SER_ENDPOINTS,
    INDEXES as SER_INDEXES,
    MODES as SER_MODES,
    TOOLS as SER_TOOLS,
    Context as SerContext,
    arm_groups as ser_arm_groups,
    flag_clash as ser_flag_clash,
    plan_tsv as ser_plan_tsv,
)

_SER_DEFAULTS = yaml.safe_load((Path(workflow.basedir).parent / "config" / "serialization.yaml").read_text())
SER_SMOKE_SAMPLE = "smoke-1M"
SER_SMOKE_ARCH = "m8a"
SER_ARCHS_ALLOWED = ("m8a", "m8g")
SER_SHA_PATTERN = "[0-9a-f]{7,40}"
# Measured rounds per timing output directory: the full target and the smoke target.
SER_KINDS = {"timing": None, "timing-smoke": 1}


def _ser_value(key):
    return config.get(f"serialization_{key}", _SER_DEFAULTS[key])


def _ser_list(key, allowed=None):
    raw = _ser_value(key)
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ValueError(f"serialization_{key} must be a comma-separated string or a list; got {raw!r}")
    items = [i.strip() for i in items if i.strip()]
    if not items and key != "samples":
        raise ValueError(f"serialization_{key} must not be empty")
    for item in items:
        if allowed is not None and item not in allowed:
            raise ValueError(f"serialization_{key}: {item!r} is not one of {list(allowed)}")
    return items


def _ser_int(key):
    value = _ser_value(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"serialization_{key} must be a positive integer; got {value!r}")
    return value


SER_SAMPLES = _ser_list("samples", CONFIG.samples.keys())
SER_MODES_SEL = _ser_list("modes", SER_MODES)
SER_ENDPOINTS_SEL = _ser_list("endpoints", SER_ENDPOINTS)
SER_TOOLS_SEL = _ser_list("tools", SER_TOOLS)
SER_INDEXES_SEL = _ser_list("indexes", SER_INDEXES)
SER_ARCHS = _ser_list("archs", SER_ARCHS_ALLOWED)
SER_BUDGET, SER_HELPERS, SER_REPS, SER_BATCH_K, SER_FIDELITY_THREADS = (
    _ser_int(k) for k in ("budget", "helpers", "reps", "batch_k", "fidelity_threads")
)
SER_ALIGNER = str(_ser_value("aligner"))
if not re.fullmatch(r"[A-Za-z0-9_./+-]+", SER_ALIGNER):
    raise ValueError(f"serialization_aligner is not a plain executable path: {SER_ALIGNER!r}")
if SER_BUDGET < SER_HELPERS + 1 or SER_BUDGET > CONFIG.arena.threads:
    raise ValueError(
        f"serialization_budget {SER_BUDGET} must be at least serialization_helpers + 1 "
        f"({SER_HELPERS + 1}) and at most the arena host's {CONFIG.arena.threads} CPUs"
    )
if {"pinned", "unpinned"} <= set(SER_ENDPOINTS_SEL) and SER_BUDGET >= CONFIG.arena.threads:
    raise ValueError(
        f"serialization_budget {SER_BUDGET} must be below the arena host's {CONFIG.arena.threads} "
        "CPUs when both pinned and unpinned are selected, or the two endpoints are the same"
    )
for _arch in {*SER_ARCHS, SER_SMOKE_ARCH}:
    if not ser_arm_groups(SER_TOOLS_SEL, SER_MODES_SEL, SER_INDEXES_SEL, upstream=_has_upstream_baseline(_arch)):
        raise ValueError(f"serialization: the selected tools/modes/indexes leave {_arch} with no arm to time")


def _ser_flags(sample_name):
    """The sample's bwa-mem3 flags, `mem_flags` then `fg_labs_flags`, as the arena passes them."""
    sample = CONFIG.samples[sample_name]
    return [*sample.mem_flags, *sample.fg_labs_flags]


def _ser_dense_reference(sample_name):
    reference = CONFIG.samples[sample_name].reference
    return reference if ARENA_DENSE_SA_SHIFT >= STOCK_SA_SHIFT else f"{reference}-u{ARENA_DENSE_SA_SHIFT}"


for _name in {*SER_SAMPLES, SER_SMOKE_SAMPLE}:
    _sample = CONFIG.samples[_name]
    if _sample.is_meth or len(_sample.fastq_names) != 2:
        raise ValueError(f"serialization sample {_name!r} must be a paired-end, non-methylation sample")
    for _token in _ser_flags(_name):
        if ser_flag_clash(_token):
            raise ValueError(f"serialization sample {_name!r}: flag {_token!r} {ser_flag_clash(_token)}")
        if _token.startswith("--compat") and "fast" in SER_MODES_SEL:
            raise ValueError(f"serialization sample {_name!r}: {_token!r} cannot be combined with mode fast")
    if "dense" in SER_INDEXES_SEL and _ser_dense_reference(_name) not in CONFIG.references:
        raise ValueError(f"serialization sample {_name!r}: no dense reference {_ser_dense_reference(_name)!r}")


def _ser_sha():
    # snakemake parses a digits-only `--config fg_labs_sha=` as an int (dropping leading zeros).
    sha = str(FG_LABS_SHA)
    if not re.fullmatch(SER_SHA_PATTERN, sha):
        raise WorkflowError(
            f"the serialization targets need `--config fg_labs_sha=<7-40 hex chars>`; got {sha!r} "
            "(a digits-only SHA is read as a number: pass one containing a letter, or quote it)"
        )
    return sha


def _ser_samples():
    if not SER_SAMPLES:
        raise WorkflowError(
            "serialization_samples is empty: pass `--config serialization_samples=<name>[,<name>...]` "
            "or set `samples` in config/serialization.yaml"
        )
    return SER_SAMPLES


def _ser_fidelity_outputs(samples, archs, modes):
    sha = _ser_sha()
    return [f"serialization/{sha}/{a}/{s}/fidelity/{m}/result.tsv" for a in archs for s in samples for m in modes]


def _ser_timing_outputs(samples, archs, endpoints, kind):
    sha = _ser_sha()
    return [f"serialization/{sha}/{a}/{s}/{kind}/{e}/timings.tsv" for a in archs for s in samples for e in endpoints]


def _ser_settings(sample_name, **extra):
    return json.dumps({"batch_k": SER_BATCH_K, "flags": _ser_flags(sample_name), "helpers": SER_HELPERS, **extra})


# Merges `name value` pairs from argv into the JSON object in argv[1]; prints the result.
_SER_JSON_MERGE = shlex.quote(
    "import json, sys; d = json.loads(sys.argv[1]); d.update(zip(sys.argv[2::2], sys.argv[3::2])); "
    "print(json.dumps(d, indent=2, sort_keys=True))"
)
# Shell snippets shared by both rules: the aligner's sha256 and the FASTQ pair count.
_SER_SHELL_COMMON = r"""
ALIGNER_PATH=$(command -v "$ALIGNER")
ALIGNER_SHA256=$( (sha256sum "$ALIGNER_PATH" 2>/dev/null || shasum -a 256 "$ALIGNER_PATH") | cut -d ' ' -f 1)
ALIGNER_VERSION=$("$ALIGNER" version 2>&1 | sed -n 1p)
R1_LINES=$(gzip -cdf "$R1" | wc -l | tr -d ' ')
R2_LINES=$(gzip -cdf "$R2" | wc -l | tr -d ' ')
if [ $((R1_LINES % 4)) -ne 0 ] || [ "$R1_LINES" -ne "$R2_LINES" ]; then
    echo "serialization: FASTQ line counts are not matching multiples of 4 ($R1_LINES, $R2_LINES)" >&2
    exit 1
fi
EXPECTED=$((R1_LINES / 2))
"""

localrules:
    serialization,
    serialization_fidelity,
    serialization_timing,
    serialization_smoke,


rule serialization:
    """Fidelity and timing for every selected sample, arch, mode and endpoint."""
    input:
        lambda wc: _ser_fidelity_outputs(_ser_samples(), SER_ARCHS, SER_MODES_SEL),
        lambda wc: _ser_timing_outputs(_ser_samples(), SER_ARCHS, SER_ENDPOINTS_SEL, "timing"),


rule serialization_fidelity:
    input:
        lambda wc: _ser_fidelity_outputs(_ser_samples(), SER_ARCHS, SER_MODES_SEL),


rule serialization_timing:
    input:
        lambda wc: _ser_timing_outputs(_ser_samples(), SER_ARCHS, SER_ENDPOINTS_SEL, "timing"),


rule serialization_smoke:
    """The smoke sample on m8a: both modes, every endpoint, one measured round."""
    input:
        lambda wc: _ser_fidelity_outputs([SER_SMOKE_SAMPLE], [SER_SMOKE_ARCH], SER_MODES),
        lambda wc: _ser_timing_outputs([SER_SMOKE_SAMPLE], [SER_SMOKE_ARCH], SER_ENDPOINTS, "timing-smoke"),


rule serialization_fidelity_run:
    input:
        ref = lambda wc: _ref_inputs(wc, meth_index="d3"),
        fastqs = _query_fastqs,
    output:
        result = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/result.tsv",
        versions = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/versions.json",
        compare0 = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/compare_bam0.txt",
        compare1 = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/compare_bam1.txt",
    wildcard_constraints:
        sha = SER_SHA_PATTERN,
        arch = "|".join(SER_ARCHS_ALLOWED),
        sample = "[A-Za-z0-9_-]+",
        mode = "|".join(SER_MODES),
    threads: SER_FIDELITY_THREADS
    resources:
        batch_queue = lambda wc: batch_queue_for(wc.arch),
        aws_batch_container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = lambda wc: _mem_mb_for(wc.sample),
    params:
        aligner = shlex.quote(SER_ALIGNER),
        flags = lambda wc: shlex.join(_ser_flags(wc.sample) + (["--fast"] if wc.mode == "fast" else [])),
        batch_k = SER_BATCH_K,
        helpers = SER_HELPERS,
        settings = lambda wc: shlex.quote(_ser_settings(wc.sample)),
        merge = _SER_JSON_MERGE,
        common = _SER_SHELL_COMMON,
    shell:
        r"""
        set -euo pipefail
        OUT_DIR=$(dirname {output.result:q})
        LOGS="$OUT_DIR/logs"
        mkdir -p "$LOGS"
        W=$(mktemp -d "${{TMPDIR:-/tmp}}/serialization.XXXXXX")
        trap 'rm -rf "$W"' EXIT
        ALIGNER={params.aligner}
        REF={input.ref[0]:q}
        R1={input.fastqs[0]:q}
        R2={input.fastqs[1]:q}
        {params.common}
        fail() {{ echo "serialization_fidelity FAILED ($OUT_DIR): $1; logs kept in $LOGS" >&2; exit 1; }}
        mem() {{ "$ALIGNER" mem -t {threads} -K {params.batch_k} {params.flags} "$@"; }}

        mem "$REF" "$R1" "$R2" 2> "$LOGS/sam.log" | samtools view -u -o "$W/sam.bam" - \
            || fail "SAM route alignment"
        mem --bam=0 -o "$W/bam0.bam" "$REF" "$R1" "$R2" 2> "$LOGS/bam0.log" || fail "native --bam=0 alignment"
        mem --bam=1 --bam-threads {params.helpers} -o "$W/bam1.bam" "$REF" "$R1" "$R2" 2> "$LOGS/bam1.log" \
            || fail "native --bam=1 alignment"

        FAILED=""
        for route in sam bam0 bam1; do
            n=$(samtools view -c -F 0x900 "$W/$route.bam" || echo NA)
            eval "PRIMARY_$route=$n"
            [ "$n" = "$EXPECTED" ] || FAILED="$FAILED $route-primary-count($n!=$EXPECTED)"
        done
        printf 'compression\texpected_primary\tsam_primary\tnative_primary\tfgumi_exit\tfgumi_result_line\n' > "$W/result.tsv"
        for level in 0 1; do
            set +e
            fgumi compare bams "$W/sam.bam" "$W/bam$level.bam" --mode content --threads 4 --max-diffs 20 \
                > "$LOGS/compare_bam$level.txt" 2>&1
            rc=$?
            set -e
            cat "$LOGS/compare_bam$level.txt" >&2
            line=$(grep -m 1 'RESULT' "$LOGS/compare_bam$level.txt" | tr '\t' ' ' || true)
            case "$rc:$line" in
                0:*IDENTICAL*) ;;
                *) FAILED="$FAILED fgumi-bam$level(exit $rc)" ;;
            esac
            native="PRIMARY_bam$level"
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$level" "$EXPECTED" "$PRIMARY_sam" "${{!native}}" "$rc" \
                "${{line:-NA}}" >> "$W/result.tsv"
        done
        [ -z "$FAILED" ] || fail "checks failed:$FAILED"

        python3 -c {params.merge} {params.settings} aligner_sha256 "$ALIGNER_SHA256" \
            bwa_mem3_version "$ALIGNER_VERSION" samtools_version "$(samtools --version | sed -n 1p)" \
            fgumi_version "$(fgumi --version 2>&1 | sed -n 1p)" > "$W/versions.json"
        cp "$LOGS/compare_bam0.txt" {output.compare0:q}
        cp "$LOGS/compare_bam1.txt" {output.compare1:q}
        mv "$W/versions.json" {output.versions:q}
        mv "$W/result.tsv" {output.result:q}
        """


def _ser_timing_ref_inputs(wc):
    """Stock bwa-mem2-format index, plus the bwa and minibwa indexes when those tools are timed."""
    paths = list(_ref_inputs(wc, meth_index="d3"))
    paths += _bwa_ref_inputs(wc) if "bwa" in SER_TOOLS_SEL else []
    paths += _minibwa_ref_inputs(wc) if "minibwa" in SER_TOOLS_SEL else []
    return list(dict.fromkeys(paths))


def _ser_dense_ref_inputs(wc):
    if "bwa-mem3" not in SER_TOOLS_SEL or "dense" not in SER_INDEXES_SEL:
        return _ref_inputs(wc, meth_index="d3")[:1]
    return _ref_inputs(wc, meth_index="d3", reference=_ser_dense_reference(wc.sample))


def _ser_plan(wc):
    sample = CONFIG.samples[wc.sample]
    ctx = SerContext(
        aligner=SER_ALIGNER, upstream=BASELINE_BINARY, batch_k=SER_BATCH_K, budget=SER_BUDGET,
        helpers=SER_HELPERS, mem_flags=tuple(sample.mem_flags),
        fg_labs_flags=tuple(sample.fg_labs_flags), minibwa_flags=tuple(sample.minibwa_flags),
    )
    groups = ser_arm_groups(SER_TOOLS_SEL, SER_MODES_SEL, SER_INDEXES_SEL, upstream=_has_upstream_baseline(wc.arch))
    reps = SER_KINDS[wc.kind] or SER_REPS
    return shlex.quote(ser_plan_tsv(groups, wc.endpoint, ctx, reps, seed=wc.sha))


rule serialization_timing_run:
    input:
        ref = _ser_timing_ref_inputs,
        ref_dense = _ser_dense_ref_inputs,
        fastqs = _query_fastqs,
    output:
        tsv = "serialization/{sha}/{arch}/{sample}/{kind}/{endpoint}/timings.tsv",
        meta = "serialization/{sha}/{arch}/{sample}/{kind}/{endpoint}/meta.json",
        settings = "serialization/{sha}/{arch}/{sample}/{kind}/{endpoint}/settings.json",
    wildcard_constraints:
        sha = SER_SHA_PATTERN,
        arch = "|".join(a for a in SER_ARCHS_ALLOWED if a in CONFIG.arena.archs),
        sample = "[A-Za-z0-9_-]+",
        kind = "|".join(SER_KINDS),
        endpoint = "|".join(SER_ENDPOINTS),
    # The whole arena host, as `align_arena` reserves it, so nothing runs beside the job.
    threads: CONFIG.arena.threads
    resources:
        batch_queue = lambda wc: arena_queue_for(wc.arch),
        aws_batch_container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = ARENA_MEM_MB,
        runtime = 14400,
    params:
        plan = _ser_plan,
        aligner = shlex.quote(SER_ALIGNER),
        settings = lambda wc: shlex.quote(_ser_settings(wc.sample, budget=SER_BUDGET, endpoint=wc.endpoint)),
        merge = _SER_JSON_MERGE,
        common = _SER_SHELL_COMMON,
    shell:
        r"""
        set -euo pipefail
        OUT_DIR=$(dirname {output.tsv:q})
        RUNS="$OUT_DIR/runs"
        mkdir -p "$RUNS"
        W=$(mktemp -d "${{TMPDIR:-/tmp}}/serialization-timing.XXXXXX")
        trap 'rm -rf "$W"' EXIT
        ALIGNER={params.aligner}
        export R1={input.fastqs[0]:q} R2={input.fastqs[1]:q}
        {params.common}
        emit-host-meta {wildcards.sha:q} {wildcards.sample:q} {wildcards.arch:q} 0 > {output.meta:q}
        python3 -c {params.merge} {params.settings} aligner_sha256 "$ALIGNER_SHA256" \
            bwa_mem3_version "$ALIGNER_VERSION" > {output.settings:q}
        printf '%s\n' {params.plan} > "$W/plan.tsv"

        # One column of tricorder's two-line timing.tsv, by header name (as in arena.smk);
        # tricorder's `-` (not measured) and a missing file both read as NA.
        timing_column() {{
            v=$(mawk -F '\t' -v name="$2" \
                'NR == 1 {{ for (i = 1; i <= NF; i++) if ($i == name) col = i }}
                 NR == 2 {{ print (col ? $col : "NA") }}' "$1" 2>/dev/null || true)
            case "$v" in ""|-) echo NA ;; *) echo "$v" ;; esac
        }}

        printf 'label\ttool\tmode\tindex\tendpoint\troute\tround\tposition\tstatus\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\tindex_read_s\n' \
            > {output.tsv:q}
        while IFS=$'\t' read -r -u 3 round position label tool mode index route writes prewarm cmd; do
            export IDX={input.ref[0]:q}
            [ "$index" = dense ] && IDX={input.ref_dense[0]:q}
            bash -c "$prewarm"
            run="$RUNS/$label.round$round"
            export RUN_OUT="$run.bam" RUN_ERR="$run.stderr.log"
            set +e
            tricorder --interval 0.1 --out "$run.timing.tsv" -- bash -c "set -o pipefail; $cmd"
            rc=$?
            set -e
            status=ok
            if [ "$rc" -ne 0 ]; then
                status=failed
            elif [ "$writes" = 1 ]; then
                n=$(samtools view -c -F 0x900 "$RUN_OUT" 2>/dev/null || echo NA)
                [ "$n" = "$EXPECTED" ] || status=failed
            fi
            rm -f "$RUN_OUT"
            proc=$(grep -oE 'PROCESS\(\).*?:[[:space:]]*[0-9.]+' "$RUN_ERR" 2>/dev/null \
                | grep -oE '[0-9.]+$' | head -1 || true)
            idx=$(grep -oE 'Index read time avg: [0-9.]+' "$RUN_ERR" 2>/dev/null \
                | grep -oE '[0-9.]+$' | head -1 || true)
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$label" "$tool" "$mode" "$index" {wildcards.endpoint:q} "$route" "$round" "$position" \
                "$status" "$(timing_column "$run.timing.tsv" s)" "$(timing_column "$run.timing.tsv" cpu_time)" \
                "$(timing_column "$run.timing.tsv" max_rss)" "${{proc:-NA}}" "${{idx:-NA}}" >> {output.tsv:q}
            echo "RESULT: label=$label round=$round status=$status exit=$rc" >&2
        done 3< "$W/plan.tsv"

        echo "serialization_timing status summary:" >&2
        cut -f 9 {output.tsv:q} | tail -n +2 | sort | uniq -c >&2
        OK_RUNS=$(mawk -F '\t' 'NR > 1 && $9 == "ok" {{ n++ }} END {{ print n + 0 }}' {output.tsv:q})
        if [ "$OK_RUNS" -eq 0 ]; then
            echo "serialization_timing FAILED: every run failed" >&2
            exit 1
        fi
        """
