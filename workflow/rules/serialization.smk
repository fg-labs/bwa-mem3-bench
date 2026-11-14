"""Serialization benchmark: bwa-mem3's native BAM writer against the SAM-to-samtools route.

`serialization_fidelity` checks, per (sha, arch, sample, mode), that the native BAM (`--bam=0`
and `--bam=1`) holds the same records as the SAM route, and that no route lost a read.
`serialization_timing` times the native and SAM routes beside bwa, minibwa and upstream bwa-mem2
on the arena's on-demand hosts, one job per (sha, arch, sample, endpoint). See
docs/SERIALIZATION.md for the endpoints, settings and outputs.
"""

import functools
import json
import re
import shlex

import yaml
from snakemake.exceptions import WorkflowError

from bwa_mem3_bench.serialization_arms import (
    COMPARATORS as SER_COMPARATORS,
    ENDPOINTS as SER_ENDPOINTS,
    INDEXES as SER_INDEXES,
    MODES as SER_MODES,
    TOOLS as SER_TOOLS,
    Context as SerContext,
    arm_groups as ser_arm_groups,
    bwa_mem3_argv as ser_bwa_mem3_argv,
    flag_clashes as ser_flag_clashes,
    plan_tsv as ser_plan_tsv,
)

_SER_DEFAULTS = yaml.safe_load((Path(workflow.basedir).parent / "config" / "serialization.yaml").read_text())
SER_SMOKE_SAMPLE = "smoke-1M"
SER_SMOKE_ARCH = "m8a"
# `<sha>`, or `<sha>-<make_target>` as `submit --make-target` composes it.
SER_SHA_PATTERN = r"[0-9a-f]{7,40}(?:-[A-Za-z0-9_.]+)*"
SER_SAMPLE_PATTERN = "[A-Za-z0-9_-]+"
# Measured rounds per timing output directory: the full target and the smoke target.
SER_KINDS = {"timing": None, "timing-smoke": 1}

# Parse time rejects only a `serialization_*` value that is bad on its own. Checks against other
# config (arena.*, samples, references) wait for `_ser_validate` and `_ser_validate_target`, so they
# fail only the serialization targets, each against what it runs, and never `all`, `smoke` or `arena`.
_ser_unknown = sorted(k for k in config if k.startswith("serialization_") and k.removeprefix("serialization_") not in _SER_DEFAULTS)
if _ser_unknown:
    raise ValueError(f"unknown serialization settings {_ser_unknown}; the keys are {sorted(_SER_DEFAULTS)}")


def _ser_value(key):
    return config.get(f"serialization_{key}", _SER_DEFAULTS[key])


def _ser_list(key, allowed=None):
    """The `serialization_<key>` list; `allowed=None` defers the membership check to `_ser_member`."""
    raw = _ser_value(key)
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ValueError(f"serialization_{key} must be a comma-separated string or a list; got {raw!r}")
    items = [i.strip() for i in items if i.strip()]
    if not items and key != "samples":
        raise ValueError(f"serialization_{key} must not be empty")
    for item in items:
        if allowed is not None:
            _ser_member(key, [item], allowed)
        if items.count(item) > 1:
            raise ValueError(f"serialization_{key} lists {item!r} more than once; list each value once")
    return items


def _ser_member(key, items, allowed):
    for item in items:
        if item not in allowed:
            raise ValueError(f"serialization_{key}: {item!r} is not one of {list(allowed)}")


def _ser_int(key):
    value = _ser_value(key)
    if key == "batch_k" and value is None:
        value = CONFIG.batch_bases
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"serialization_{key} must be a positive integer; got {value!r}")
    return value


SER_SAMPLES = _ser_list("samples")
SER_MODES_SEL = _ser_list("modes", SER_MODES)
SER_ENDPOINTS_SEL = _ser_list("endpoints", SER_ENDPOINTS)
SER_TOOLS_SEL = _ser_list("tools", SER_TOOLS)
SER_INDEXES_SEL = _ser_list("indexes", SER_INDEXES)
SER_ARCHS = _ser_list("archs")
SER_BUDGET, SER_HELPERS, SER_REPS, SER_BATCH_K, SER_FIDELITY_THREADS = (
    _ser_int(k) for k in ("budget", "helpers", "reps", "batch_k", "fidelity_threads")
)
SER_ALIGNER = str(_ser_value("aligner"))
if not re.fullmatch(r"[A-Za-z0-9_./+-]+", SER_ALIGNER):
    raise ValueError(f"serialization_aligner is not a plain executable path: {SER_ALIGNER!r}")
if SER_BUDGET < SER_HELPERS + 1:
    raise ValueError(
        f"serialization_budget {SER_BUDGET} must be at least serialization_helpers + 1 "
        f"({SER_HELPERS + 1}) and at most arena.threads ({CONFIG.arena.threads})"
    )
_ser_dropped = [t for t in SER_TOOLS_SEL if t in SER_COMPARATORS]
if _ser_dropped and not ("default" in SER_MODES_SEL and "stock" in SER_INDEXES_SEL):
    raise ValueError(
        f"serialization_tools {_ser_dropped} run only in default mode on the stock index: add default "
        "to serialization_modes and stock to serialization_indexes, or remove them from serialization_tools"
    )


def _ser_check_budget(endpoints):
    if {"pinned", "unpinned"} <= set(endpoints) and SER_BUDGET >= CONFIG.arena.threads:
        raise ValueError(
            f"serialization_budget {SER_BUDGET} must be below arena.threads ({CONFIG.arena.threads}) "
            "when pinned and unpinned both run, or the two endpoints are the same"
        )


def _ser_groups(arch):
    return ser_arm_groups(SER_TOOLS_SEL, SER_MODES_SEL, SER_INDEXES_SEL, upstream=_has_upstream_baseline(arch))


def _ser_arms(arch):
    return [arm for group in _ser_groups(arch) for arm in group]


def _ser_flags(sample_name):
    """The sample's bwa-mem3 flags, `mem_flags` then `fg_labs_flags`, as the arena passes them."""
    sample = CONFIG.samples[sample_name]
    return [*sample.mem_flags, *sample.fg_labs_flags]


def _ser_dense_reference(sample_name):
    return f"{CONFIG.samples[sample_name].reference}-u{ARENA_DENSE_SA_SHIFT}"


@functools.cache
def _ser_check_sample(name, modes):
    """Reject a sample the benchmark cannot run in `modes`; shared by the config and `{sample}`."""
    if name not in CONFIG.samples:
        raise ValueError(f"serialization: unknown sample {name!r}")
    sample = CONFIG.samples[name]
    if sample.is_meth or sample.layout != "paired":
        raise ValueError(f"serialization sample {name!r} must be a paired-end, non-methylation sample")
    problems = ser_flag_clashes(_ser_flags(name))
    if not problems and "minibwa" in SER_TOOLS_SEL:
        problems = ser_flag_clashes(sample.minibwa_flags)  # raises on a mem_flag minibwa cannot take
    if problems:
        raise ValueError(f"serialization sample {name!r} sets an option the benchmark controls: {'; '.join(problems)}")
    if sample.is_compat and "fast" in modes:
        raise ValueError(f"serialization sample {name!r}: --compat={sample.compat_target} cannot run in mode fast")
    if "dense" in SER_INDEXES_SEL and _ser_dense_reference(name) not in CONFIG.references:
        raise ValueError(f"serialization sample {name!r}: no dense reference {_ser_dense_reference(name)!r}")


@functools.cache
def _ser_validate():
    """The checks against arena and index config that hold for every serialization job."""
    if SER_BUDGET > CONFIG.arena.threads:
        raise ValueError(
            f"serialization_budget {SER_BUDGET} must be at least serialization_helpers + 1 "
            f"({SER_HELPERS + 1}) and at most arena.threads ({CONFIG.arena.threads})"
        )
    if SER_FIDELITY_THREADS > CONFIG.arena.threads:
        raise ValueError(f"serialization_fidelity_threads must be at most arena.threads ({CONFIG.arena.threads})")
    if "dense" in SER_INDEXES_SEL and ARENA_DENSE_SA_SHIFT >= STOCK_SA_SHIFT:
        raise ValueError(
            f"serialization_indexes has dense, but arena.dense_sa_shift is {ARENA_DENSE_SA_SHIFT} (stock), "
            "so there is no dense index: pass serialization_indexes=stock"
        )


@functools.cache
def _ser_check_arch(arch):
    if not _ser_groups(arch):
        raise ValueError(
            f"serialization: the selected tools leave {arch} with no arm to time: upstream bwa-mem2 "
            "runs only on x86; add bwa-mem3, bwa or minibwa to serialization_tools, or drop the arch"
        )


@functools.cache
def _ser_validate_target(samples, archs, modes, endpoints):
    """The checks for one target, against only the samples, archs, modes and endpoints it requests."""
    _ser_validate()
    _ser_member("samples", samples, CONFIG.samples)
    _ser_member("archs", archs, CONFIG.arena.archs)
    _ser_check_budget(endpoints)
    for arch in archs:
        _ser_check_arch(arch)
    for name in samples:
        _ser_check_sample(name, modes)


def _ser_sha():
    # snakemake parses a digits-only `--config fg_labs_sha=` as an int (dropping leading zeros).
    sha = str(FG_LABS_SHA)
    if not re.fullmatch(SER_SHA_PATTERN, sha):
        raise WorkflowError(
            f"the serialization targets need `--config fg_labs_sha=<7-40 hex chars>[-<make target>]`; got "
            f"{sha!r} (a digits-only SHA is read as a number: pass one containing a letter, or quote it)"
        )
    return sha


def _ser_check_sha(wc):
    """Refuse an output `{sha}` other than `fg_labs_sha`: the job runs that SHA's image either way."""
    if wc.sha != _ser_sha():
        raise WorkflowError(
            f"serialization output sha {wc.sha!r} differs from the configured fg_labs_sha {_ser_sha()!r}, "
            "whose image would run the job"
        )


def _ser_samples():
    if not SER_SAMPLES:
        raise WorkflowError(
            "serialization_samples is empty: pass `--config serialization_samples=<name>[,<name>...]` "
            "or set `samples` in config/serialization.yaml"
        )
    _ser_validate_target(tuple(SER_SAMPLES), tuple(SER_ARCHS), tuple(SER_MODES_SEL), tuple(SER_ENDPOINTS_SEL))
    return SER_SAMPLES


def _ser_fidelity_outputs(samples, archs, modes):
    sha = _ser_sha()
    return [f"serialization/{sha}/{a}/{s}/fidelity/{m}/result.tsv" for a in archs for s in samples for m in modes]


def _ser_timing_outputs(samples, archs, endpoints, kind):
    sha = _ser_sha()
    return [f"serialization/{sha}/{a}/{s}/{kind}/{e}/timings.tsv" for a in archs for s in samples for e in endpoints]


def _ser_smoke_inputs(wc):
    if SER_SMOKE_ARCH not in CONFIG.arena.archs:
        raise ValueError(f"serialization: the smoke arch {SER_SMOKE_ARCH} is not in arena.archs {CONFIG.arena.archs}")
    # The smoke always runs both modes and every endpoint.
    _ser_validate_target((SER_SMOKE_SAMPLE,), (SER_SMOKE_ARCH,), tuple(SER_MODES), tuple(SER_ENDPOINTS))
    return [
        *_ser_fidelity_outputs([SER_SMOKE_SAMPLE], [SER_SMOKE_ARCH], SER_MODES),
        *_ser_timing_outputs([SER_SMOKE_SAMPLE], [SER_SMOKE_ARCH], SER_ENDPOINTS, "timing-smoke"),
    ]


def _ser_stock_ref(wc, modes):
    """The stock index; every serialization job's inputs go through here, so it also validates."""
    _ser_check_sha(wc)
    _ser_validate()
    _ser_check_sample(wc.sample, tuple(modes))
    return _ref_inputs(wc, meth_index="d3")


def _ser_context(sample_name):
    sample = CONFIG.samples[sample_name]
    return SerContext(
        aligner=SER_ALIGNER, upstream=BASELINE_BINARY, batch_k=SER_BATCH_K, budget=SER_BUDGET,
        helpers=SER_HELPERS, mem_flags=tuple(sample.mem_flags), fg_labs_flags=tuple(sample.fg_labs_flags),
        minibwa_flags=tuple(sample.minibwa_flags) if "minibwa" in SER_TOOLS_SEL else (),
    )


def _ser_fidelity_command(wc, threads, level):
    return shlex.join(ser_bwa_mem3_argv(_ser_context(wc.sample), wc.mode, threads, level))


def _ser_settings(sample_name, **extra):
    return json.dumps({"batch_k": SER_BATCH_K, "flags": _ser_flags(sample_name), "helpers": SER_HELPERS, **extra})


# Merges `name value` pairs from argv into the JSON object in argv[1]; prints the result.
_SER_JSON_MERGE = shlex.quote(
    "import json, sys; d = json.loads(sys.argv[1]); d.update(zip(sys.argv[2::2], sys.argv[3::2])); "
    "print(json.dumps(d, indent=2, sort_keys=True))"
)
# Shell snippets shared by both rules: the aligner's sha256 and the FASTQ pair count.
_SER_SHELL_COMMON = r"""
ALIGNER_PATH=$(command -v "$ALIGNER") || {
    echo "serialization: aligner '$ALIGNER' not found on PATH (see serialization_aligner)" >&2
    exit 1
}
ALIGNER_SHA256=$( (sha256sum "$ALIGNER_PATH" 2>/dev/null || shasum -a 256 "$ALIGNER_PATH") | cut -d ' ' -f 1)
ALIGNER_VERSION=$("$ALIGNER" version 2>&1 | sed -n 1p)
R1_LINES=$(gzip -cdf "$R1" | wc -l | tr -d ' ')
R2_LINES=$(gzip -cdf "$R2" | wc -l | tr -d ' ')
if [ "$R1_LINES" -eq 0 ] || [ $((R1_LINES % 4)) -ne 0 ] || [ "$R1_LINES" -ne "$R2_LINES" ]; then
    echo "serialization: FASTQ line counts are not matching non-zero multiples of 4 ($R1_LINES, $R2_LINES)" >&2
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
        _ser_smoke_inputs,


rule serialization_fidelity_run:
    input:
        ref = lambda wc: _ser_stock_ref(wc, [wc.mode]),
        fastqs = _query_fastqs,
    output:
        result = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/result.tsv",
        versions = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/versions.json",
        compare0 = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/compare_bam0.txt",
        compare1 = "serialization/{sha}/{arch}/{sample}/fidelity/{mode}/compare_bam1.txt",
    wildcard_constraints:
        sha = SER_SHA_PATTERN,
        arch = "|".join(CONFIG.arena.archs),
        sample = SER_SAMPLE_PATTERN,
        mode = "|".join(SER_MODES),
    threads: SER_FIDELITY_THREADS
    # The arena's on-demand queue: Biffix registers only the `-arena` classes for m8a and m8g.
    resources:
        batch_queue = lambda wc: arena_queue_for(wc.arch),
        aws_batch_container_image = lambda wc: image_for_arch(wc.arch),
        mem_mb = ARENA_MEM_MB,
    params:
        aligner = shlex.quote(SER_ALIGNER),
        sam = lambda wc, threads: _ser_fidelity_command(wc, threads, None),
        bam0 = lambda wc, threads: _ser_fidelity_command(wc, threads, 0),
        bam1 = lambda wc, threads: _ser_fidelity_command(wc, threads, 1),
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
        # Reports the failure, with the last lines of the failing step's log ($2) if there is one.
        fail() {{
            echo "serialization_fidelity FAILED ($OUT_DIR): $1" >&2
            if [ -f "${{2:-}}" ]; then tail -n 30 "$2" >&2; fi
            exit 1
        }}

        {params.sam} "$REF" "$R1" "$R2" 2> "$LOGS/sam.log" | samtools view -u -o "$W/sam.bam" - \
            || fail "SAM route alignment" "$LOGS/sam.log"
        {params.bam0} -o "$W/bam0.bam" "$REF" "$R1" "$R2" 2> "$LOGS/bam0.log" \
            || fail "native --bam=0 alignment" "$LOGS/bam0.log"
        {params.bam1} -o "$W/bam1.bam" "$REF" "$R1" "$R2" 2> "$LOGS/bam1.log" \
            || fail "native --bam=1 alignment" "$LOGS/bam1.log"

        FAILED=""
        # Indexed, not associative (Bash 3.2 has no `declare -A`): sam, bam0, bam1 are 0, 1, 2.
        ROUTES=(sam bam0 bam1)
        PRIMARY=()
        for i in 0 1 2; do
            route=${{ROUTES[$i]}}
            PRIMARY[$i]=$(samtools view -@ {threads} -c -F 0x900 "$W/$route.bam" || echo NA)
            [ "${{PRIMARY[$i]}}" = "$EXPECTED" ] || FAILED="$FAILED $route-primary-count(${{PRIMARY[$i]}}!=$EXPECTED)"
        done
        printf 'compression\texpected_primary\tsam_primary\tnative_primary\tfgumi_exit\tfgumi_result_line\n' > "$W/result.tsv"
        for level in 0 1; do
            report="$W/compare_bam$level.txt"
            set +e
            fgumi compare bams "$W/sam.bam" "$W/bam$level.bam" --mode content --threads 4 --max-diffs 20 > "$report" 2>&1
            rc=$?
            set -e
            line=$(mawk '/RESULT/ {{ gsub(/\t/, " "); print; exit }}' "$report")
            case "$rc:$line" in
                0:*IDENTICAL*) ;;
                *)
                    FAILED="$FAILED fgumi-bam$level(exit $rc)"
                    echo "--- fgumi compare, SAM route vs --bam=$level ---" >&2
                    cat "$report" >&2
                    ;;
            esac
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$level" "$EXPECTED" "${{PRIMARY[0]}}" "${{PRIMARY[$((level + 1))]}}" "$rc" \
                "${{line:-NA}}" >> "$W/result.tsv"
        done
        [ -z "$FAILED" ] || fail "checks failed:$FAILED"

        python3 -c {params.merge} {params.settings} aligner_sha256 "$ALIGNER_SHA256" \
            bwa_mem3_version "$ALIGNER_VERSION" samtools_version "$(samtools --version | sed -n 1p)" \
            fgumi_version "$(fgumi --version 2>&1 | sed -n 1p)" > "$W/versions.json"
        mv "$W/compare_bam0.txt" {output.compare0:q}
        mv "$W/compare_bam1.txt" {output.compare1:q}
        mv "$W/versions.json" {output.versions:q}
        mv "$W/result.tsv" {output.result:q}
        """


def _ser_timing_ref_inputs(wc):
    """The index families the arch's arms read, as `_arena_ref_inputs` stages them (stock only)."""
    _ser_validate()
    _ser_check_arch(wc.arch)
    arms = _ser_arms(wc.arch)
    stock = _ser_stock_ref(wc, SER_MODES_SEL)
    reads_stock = any(a.tool in ("bwa-mem3", "bwa-mem2") and a.index == "stock" for a in arms)
    paths = list(stock) if reads_stock else stock[:1]
    tools = {a.tool for a in arms}
    paths += _bwa_ref_inputs(wc) if "bwa" in tools else []
    paths += _minibwa_ref_inputs(wc) if "minibwa" in tools else []
    return list(dict.fromkeys(paths))


def _ser_dense_ref_inputs(wc):
    _ser_validate()
    if not any(a.index == "dense" for a in _ser_arms(wc.arch)):
        return _ref_inputs(wc, meth_index="d3")[:1]
    return _ref_inputs(wc, meth_index="d3", reference=_ser_dense_reference(wc.sample))


def _ser_plan(wc):
    _ser_validate()
    reps = SER_KINDS[wc.kind] or SER_REPS
    return shlex.quote(ser_plan_tsv(_ser_groups(wc.arch), wc.endpoint, _ser_context(wc.sample), reps, seed=wc.sha))


# Comparator version probes for settings.json; `|| true` because bwa exits non-zero on usage.
_SER_VERSION_PROBES = {
    "bwa": "bwa 2>&1 | grep -m 1 '^Version' || true",
    "minibwa": "minibwa --version 2>&1 | sed -n 1p || true",
    "bwa-mem2": f"{shlex.quote(BASELINE_BINARY)} version 2>&1 | sed -n 1p || true",
}


def _ser_version_args(wc):
    tools = {a.tool for a in _ser_arms(wc.arch)}
    return " ".join(f'{t}_version "$({p})"' for t, p in _SER_VERSION_PROBES.items() if t in tools)


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
        arch = "|".join(CONFIG.arena.archs),
        sample = SER_SAMPLE_PATTERN,
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
        budget = SER_BUDGET,
        prewarm_passes = CONFIG.arena.prewarm_passes,
        settings = lambda wc: shlex.quote(
            _ser_settings(
                wc.sample, budget=SER_BUDGET, endpoint=wc.endpoint, dense_sa_shift=ARENA_DENSE_SA_SHIFT,
                prewarm_passes=CONFIG.arena.prewarm_passes,
            )
        ),
        versions = _ser_version_args,
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
            bwa_mem3_version "$ALIGNER_VERSION" samtools_version "$(samtools --version | sed -n 1p)" \
            {params.versions} > {output.settings:q}
        printf '%s\n' {params.plan} > "$W/plan.tsv"

        # One column of tricorder's two-line timing.tsv, by header name (as in arena.smk);
        # tricorder's `-` (not measured) and a missing file both read as NA.
        timing_column() {{
            if [ ! -s "$1" ]; then echo NA; return; fi
            mawk -F '\t' -v name="$2" \
                'NR == 1 {{ for (i = 1; i <= NF; i++) if ($i == name) col = i }}
                 NR == 2 {{ v = col ? $col : "NA"; print (v == "" || v == "-" ? "NA" : v) }}' "$1"
        }}
        # The first number after `key` in a run's stderr, else NA.
        stderr_number() {{
            if [ ! -f "$1" ]; then echo NA; return; fi
            mawk -v key="$2" \
                'i = index($0, key) {{ s = substr($0, i + length(key))
                     if (match(s, /[0-9]+(\.[0-9]+)?/)) {{ print substr(s, RSTART, RLENGTH); found = 1; exit }} }}
                 END {{ if (!found) print "NA" }}' "$1"
        }}

        printf 'label\ttool\tmode\tindex\tendpoint\troute\tblock\tround\tposition\tstatus\texit_code\tprimary_count\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\tindex_read_s\n' \
            > {output.tsv:q}
        while IFS=$'\t' read -r -u 3 block round position label tool mode index route writes prewarm cmd; do
            export IDX={input.ref[0]:q}
            if [ "$index" = dense ]; then IDX={input.ref_dense[0]:q}; fi
            for _ in $(seq {params.prewarm_passes}); do bash -c "$prewarm"; done
            run="$RUNS/$label.round$round"
            export RUN_OUT="$W/run.bam" RUN_ERR="$run.stderr.log"
            set +e
            tricorder --interval 0.1 --out "$run.timing.tsv" -- bash -c "set -o pipefail; $cmd"
            rc=$?
            set -e
            count=NA
            if [ "$rc" -ne 0 ]; then
                status=failed
            elif [ "$writes" = 0 ]; then
                status=unchecked
            else
                count=$(samtools view -@ {params.budget} -c -F 0x900 "$RUN_OUT" 2>/dev/null || echo NA)
                status=ok
                [ "$count" = "$EXPECTED" ] || status=failed
            fi
            rm -f "$RUN_OUT"
            if [ "$status" = failed ]; then
                metrics="NA\tNA\tNA\tNA\tNA"
                echo "serialization_timing: $label round $round failed (exit $rc, primary $count of $EXPECTED)" >&2
                if [ -f "$RUN_ERR" ]; then tail -n 20 "$RUN_ERR" >&2; fi
            else
                metrics="$(timing_column "$run.timing.tsv" s)\t$(timing_column "$run.timing.tsv" cpu_time)"
                metrics="$metrics\t$(timing_column "$run.timing.tsv" max_rss)\t$(stderr_number "$RUN_ERR" 'PROCESS()')"
                metrics="$metrics\t$(stderr_number "$RUN_ERR" 'Index read time avg:')"
            fi
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t$metrics\n" \
                "$label" "$tool" "$mode" "$index" {wildcards.endpoint:q} "$route" "$block" "$round" "$position" \
                "$status" "$rc" "$count" >> {output.tsv:q}
            echo "RESULT: label=$label round=$round status=$status exit=$rc" >&2
        done 3< "$W/plan.tsv"

        # Measured rounds only (round >= 1); `unchecked` (practical) counts as a completed run. Exits
        # non-zero only when no measured run completed, so one bad arm keeps the others' rows.
        mawk -F '\t' 'NR > 1 && $8 >= 1 {{ n[$10]++; k = $1 " (" $6 ")"; seen[k] = 1; if ($10 != "failed") good[k]++ }}
            END {{ for (s in n) print "serialization_timing: " n[s] " measured runs " s
                   for (k in seen) if (!good[k]) print "serialization_timing: no completed measured run for " k
                   if (n["ok"] + n["unchecked"] == 0) {{ print "serialization_timing FAILED: no measured run completed"; exit 1 }} }}' \
            {output.tsv:q} >&2
        """
