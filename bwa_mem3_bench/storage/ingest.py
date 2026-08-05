"""Walk a local `runs/<sha>/` tree and populate SQLite."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from bwa_mem3_bench.storage import VS_BASELINE, VS_DEFAULT, VS_GOLDEN
from bwa_mem3_bench.storage.sqlite import (
    upsert_accuracy,
    upsert_comparison,
    upsert_run,
    upsert_scaling,
    upsert_trial,
)

_MIN_TSV_LINES = 2  # header + at least one data row (timing, meth, etc.)

# compare-bams NON-PRIMARY divergence fields, stored as a JSON blob in
# comparisons.supp_json. Absent on older comparison JSON (pre-supp-metrics), and
# the `sec_*` / `*_matched` / `*_content_diffs` entries are absent on anything
# written before compare-bams gained the secondary axis and content comparison.
#
# This is a WHITELIST and it is a hand-maintained mirror of `ConcordanceReport`
# in tools/compare-bams/src/report.rs. A field the comparator emits but this
# tuple omits is dropped here and never reaches `benchmark.db` or any report --
# silently, because the numbers still land in S3, so the loss only surfaces when
# someone tries to read them back out, by which point recovering them means
# re-running the benchmark. `test_supp_keys_mirror_the_report_struct` reads the
# Rust source and fails if the two drift, which is the only reason this
# duplication is safe to keep.
_SUPP_KEYS = (
    "total_templates",
    "supp_query_total",
    "supp_baseline_total",
    "supp_count_mismatch_templates",
    "supp_count_mismatch_pct",
    "supp_unmatched",
    "supp_unmatched_pct",
    "supp_matched",
    "supp_content_diffs",
    "supp_by_class",
    "sec_query_total",
    "sec_baseline_total",
    "sec_count_mismatch_templates",
    "sec_count_mismatch_pct",
    "sec_unmatched",
    "sec_unmatched_pct",
    "sec_matched",
    "sec_content_diffs",
    "sec_by_class",
)


def _supp_json(comp: dict[str, Any]) -> str | None:
    """Extract the supplementary-metric subset of a comparison JSON, or None."""
    supp = {k: comp[k] for k in _SUPP_KEYS if k in comp}
    return json.dumps(supp) if supp else None


# bwa-mem2's profiling output (printed to stderr by both upstream v2.2.1 and
# fg-labs in identical format — see src/profiling.cpp). PROCESS() is the
# total compute time excluding index loading; that's the apples-to-apples
# kernel speedup metric, immune to host page-cache state. Index read time is
# diagnostic only — useful for telling cold-cache from warm-cache runs.
_PROCESS_RE = re.compile(r"^\s*PROCESS\(\).*?:\s*([0-9.]+)\s*$", re.MULTILINE)
_INDEX_READ_RE = re.compile(r"^\s*Index read time avg:\s*([0-9.]+),", re.MULTILINE)

# Baseline trials are stored in the same `trials` table as fg-labs trials,
# distinguished by a synthetic `fg_labs_sha` of the form
# `baseline-bwa-mem2-<upstream_tag>` (e.g. `baseline-bwa-mem2-v2.2.1`).
# This keeps a single query path for both fg-labs and baseline timings.
BASELINE_SHA_PREFIX = "baseline-bwa-mem2-"


# scaling.tsv columns: threads, rep, wall_s, cpu_s, max_rss_mb, process_s,
# main_mem_s, read_io_s, sam_io_s, kernel_s. The first six are required; the
# phase-breakdown columns were added later, so a shorter row is still ingested
# (older ladders simply have NULL phases rather than being rejected).
_SCALING_COLUMNS = 6
_SCALING_COLUMNS_FULL = 10


def _maybe_float(cell: str) -> float | None:
    """Parse a TSV cell to float, or None for the rule's "NA" sentinel."""
    try:
        return float(cell)
    except ValueError:
        return None


def baseline_sha_for(tool_version: str) -> str:
    """Return the synthetic `fg_labs_sha` used for baseline trials of `tool_version`."""
    return f"{BASELINE_SHA_PREFIX}{tool_version}"


def _parse_timing_tsv(path: Path) -> dict[str, float]:
    """Parse Snakemake's benchmark directive output (single data row after header)."""
    text = path.read_text().strip().splitlines()
    if len(text) < _MIN_TSV_LINES:
        raise ValueError(f"malformed timing.tsv: {path}")
    header = text[0].split("\t")
    values = text[1].split("\t")
    out: dict[str, float] = {}
    for col, val in zip(header, values, strict=True):
        if col == "h:m:s":
            continue
        try:
            out[col] = float(val)
        except ValueError:
            out[col] = 0.0
    return out


def _parse_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _parse_bwa_stderr(path: Path) -> tuple[float | None, float | None]:
    """Parse PROCESS() and Index read time from a bwa-mem2 stderr log.

    Returns (process_seconds, index_read_seconds), either of which may be
    None when the corresponding line is absent or malformed (e.g. bwa
    crashed mid-init, a piped consumer closed early, or the log was
    truncated). PROCESS() is the kernel-only compute time; Index read time
    is diagnostic for cache state.
    """
    if not path.exists():
        return (None, None)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return (None, None)
    process_match = _PROCESS_RE.search(text)
    index_match = _INDEX_READ_RE.search(text)
    process = float(process_match.group(1)) if process_match else None
    index_read = float(index_match.group(1)) if index_match else None
    return (process, index_read)


def ingest_run(
    conn: sqlite3.Connection,
    *,
    runs_root: Path,
    fg_labs_sha: str,
) -> int:
    """Walk `runs_root/<fg_labs_sha>/<sample>/<arch>/rep-<n>/` and populate DB."""
    sha_dir = runs_root / fg_labs_sha
    if not sha_dir.is_dir():
        raise FileNotFoundError(f"no run tree at {sha_dir}")

    upsert_run(conn, fg_labs_sha=fg_labs_sha, status="complete", commit=False)

    count = 0
    for sample_dir in sorted(d for d in sha_dir.iterdir() if d.is_dir()):
        sample = sample_dir.name
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            arch = arch_dir.name
            for rep_dir in sorted(d for d in arch_dir.iterdir() if d.is_dir()):
                if not rep_dir.name.startswith("rep-"):
                    continue
                rep = int(rep_dir.name.split("-", 1)[1])

                timing_path = rep_dir / "benchmarks" / "timing.tsv"
                meta_path = rep_dir / "benchmarks" / "meta.json"
                bwa_stderr_path = rep_dir / "benchmarks" / "bwa.stderr.log"
                if not timing_path.exists():
                    continue
                timing = _parse_timing_tsv(timing_path)
                meta: dict[str, Any] = _parse_json_file(meta_path) if meta_path.exists() else {}
                process_seconds, index_read_seconds = _parse_bwa_stderr(bwa_stderr_path)

                reads_processed = 0
                vs_baseline_path = rep_dir / "compare" / "vs-baseline.json"
                if vs_baseline_path.exists():
                    reads_processed = int(_parse_json_file(vs_baseline_path).get("total_reads", 0))

                trial_id = upsert_trial(
                    conn,
                    fg_labs_sha=fg_labs_sha,
                    sample=sample,
                    arch=arch,
                    rep=rep,
                    wall_seconds=float(timing.get("s", 0.0)),
                    max_rss_mb=float(timing.get("max_rss", 0.0)),
                    cpu_time=float(timing.get("cpu_time", 0.0)),
                    io_read_mb=float(timing.get("io_in", 0.0)),
                    io_write_mb=float(timing.get("io_out", 0.0)),
                    mean_load=float(timing.get("mean_load", 0.0)),
                    reads_processed=reads_processed,
                    instance_type=(
                        str(meta.get("instance_type")) if meta.get("instance_type") else None
                    ),
                    availability_zone=(
                        str(meta.get("availability_zone"))
                        if meta.get("availability_zone")
                        else None
                    ),
                    spot_price=None,
                    status="ok",
                    process_seconds=process_seconds,
                    index_read_seconds=index_read_seconds,
                    commit=False,
                )

                for kind in (VS_BASELINE, VS_GOLDEN, VS_DEFAULT):
                    path = rep_dir / "compare" / f"{kind}.json"
                    if not path.exists():
                        continue
                    comp = _parse_json_file(path)
                    upsert_comparison(
                        conn,
                        trial_id=trial_id,
                        kind=kind,
                        concordant=int(comp.get("concordant", 0)),
                        total=int(comp.get("total_reads", 0)),
                        concordance_pct=float(comp.get("concordance_pct", 0.0)),
                        by_class_json=json.dumps(comp.get("by_class", {})),
                        supp_json=_supp_json(comp),
                        commit=False,
                    )

                count += 1

    conn.commit()
    return count


def ingest_baseline(
    conn: sqlite3.Connection,
    *,
    baseline_root: Path,
    tool_version: str,
) -> int:
    """Walk `<baseline_root>/bwa-mem2-<tool_version>/<sample>/<arch>/rep-<n>/` and populate DB.

    Baseline trials are stored in the existing `trials` table with a synthetic
    `fg_labs_sha` of `baseline-bwa-mem2-<tool_version>` so all existing queries
    keep working. There is no `compare/*.json` for baseline trials — only the
    `benchmarks/timing.tsv` (and optional `benchmarks/meta.json`) are read.

    Returns the number of trial rows upserted. Returns 0 (without raising) if
    the baseline tree for `tool_version` is missing, since baseline runs are
    optional inputs to ``cli collect``.

    :param conn: SQLite connection.
    :param baseline_root: local mirror of the S3 ``baseline/`` prefix (i.e.
        ``LOCAL_MIRROR_ROOT / "baseline"``).
    :param tool_version: upstream tag (e.g. ``"v2.2.1"``).
    """
    tool_dir = baseline_root / f"bwa-mem2-{tool_version}"
    if not tool_dir.is_dir():
        return 0

    synthetic_sha = baseline_sha_for(tool_version)
    upsert_run(
        conn,
        fg_labs_sha=synthetic_sha,
        upstream_tag=tool_version,
        status="baseline",
        commit=False,
    )

    count = 0
    for sample_dir in sorted(d for d in tool_dir.iterdir() if d.is_dir()):
        sample = sample_dir.name
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            arch = arch_dir.name
            for rep_dir in sorted(d for d in arch_dir.iterdir() if d.is_dir()):
                if not rep_dir.name.startswith("rep-"):
                    continue
                rep = int(rep_dir.name.split("-", 1)[1])

                timing_path = rep_dir / "benchmarks" / "timing.tsv"
                meta_path = rep_dir / "benchmarks" / "meta.json"
                bwa_stderr_path = rep_dir / "benchmarks" / "bwa.stderr.log"
                if not timing_path.exists():
                    continue
                timing = _parse_timing_tsv(timing_path)
                meta: dict[str, Any] = _parse_json_file(meta_path) if meta_path.exists() else {}
                process_seconds, index_read_seconds = _parse_bwa_stderr(bwa_stderr_path)

                upsert_trial(
                    conn,
                    fg_labs_sha=synthetic_sha,
                    sample=sample,
                    arch=arch,
                    rep=rep,
                    wall_seconds=float(timing.get("s", 0.0)),
                    max_rss_mb=float(timing.get("max_rss", 0.0)),
                    cpu_time=float(timing.get("cpu_time", 0.0)),
                    io_read_mb=float(timing.get("io_in", 0.0)),
                    io_write_mb=float(timing.get("io_out", 0.0)),
                    mean_load=float(timing.get("mean_load", 0.0)),
                    reads_processed=0,
                    instance_type=(
                        str(meta.get("instance_type")) if meta.get("instance_type") else None
                    ),
                    availability_zone=(
                        str(meta.get("availability_zone"))
                        if meta.get("availability_zone")
                        else None
                    ),
                    spot_price=None,
                    status="ok",
                    process_seconds=process_seconds,
                    index_read_seconds=index_read_seconds,
                    commit=False,
                )
                count += 1

    conn.commit()
    return count


# minibwa trials share the `trials` table with fg-labs and baseline trials,
# distinguished by a synthetic `fg_labs_sha` of `minibwa-<minibwa_sha>`. This
# keeps a single query path; `bench speedup --minibwa-sha` joins on it.
MINIBWA_SHA_PREFIX = "minibwa-"


def minibwa_sha_for(minibwa_sha: str) -> str:
    """Return the synthetic `fg_labs_sha` used for minibwa trials of `minibwa_sha`."""
    return f"{MINIBWA_SHA_PREFIX}{minibwa_sha}"


def ingest_minibwa(
    conn: sqlite3.Connection,
    *,
    minibwa_root: Path,
    minibwa_sha: str,
) -> int:
    """Walk `<minibwa_root>/<minibwa_sha>/<sample>/<arch>/rep-<n>/` and populate DB.

    minibwa trials are stored in the existing `trials` table with a synthetic
    `fg_labs_sha` of `minibwa-<minibwa_sha>` so all existing queries keep
    working. minibwa is a wall-time-only probe — there is no `compare/*.json`,
    and its stderr is a different format than bwa-mem2's so no `PROCESS()` /
    index-read time is parsed. Only `benchmarks/timing.minibwa.tsv` (the
    tricorder TSV, same schema as `timing.tsv`) and optional
    `benchmarks/meta.json` are read.

    Returns the number of trial rows upserted; 0 (without raising) if the
    minibwa tree for `minibwa_sha` is missing, since minibwa runs are optional
    inputs to ``cli collect``.

    :param conn: SQLite connection.
    :param minibwa_root: local mirror of the S3 ``minibwa/`` prefix (i.e.
        ``LOCAL_MIRROR_ROOT / "minibwa"``).
    :param minibwa_sha: vendored lh3/minibwa commit SHA.
    """
    sha_dir = minibwa_root / minibwa_sha
    if not sha_dir.is_dir():
        return 0

    synthetic_sha = minibwa_sha_for(minibwa_sha)
    upsert_run(conn, fg_labs_sha=synthetic_sha, status="minibwa", commit=False)

    count = 0
    for sample_dir in sorted(d for d in sha_dir.iterdir() if d.is_dir()):
        sample = sample_dir.name
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            arch = arch_dir.name
            for rep_dir in sorted(d for d in arch_dir.iterdir() if d.is_dir()):
                if not rep_dir.name.startswith("rep-"):
                    continue
                rep = int(rep_dir.name.split("-", 1)[1])

                timing_path = rep_dir / "benchmarks" / "timing.minibwa.tsv"
                meta_path = rep_dir / "benchmarks" / "meta.json"
                if not timing_path.exists():
                    continue
                timing = _parse_timing_tsv(timing_path)
                meta: dict[str, Any] = _parse_json_file(meta_path) if meta_path.exists() else {}

                upsert_trial(
                    conn,
                    fg_labs_sha=synthetic_sha,
                    sample=sample,
                    arch=arch,
                    rep=rep,
                    wall_seconds=float(timing.get("s", 0.0)),
                    max_rss_mb=float(timing.get("max_rss", 0.0)),
                    cpu_time=float(timing.get("cpu_time", 0.0)),
                    io_read_mb=float(timing.get("io_in", 0.0)),
                    io_write_mb=float(timing.get("io_out", 0.0)),
                    mean_load=float(timing.get("mean_load", 0.0)),
                    reads_processed=0,
                    instance_type=(
                        str(meta.get("instance_type")) if meta.get("instance_type") else None
                    ),
                    availability_zone=(
                        str(meta.get("availability_zone"))
                        if meta.get("availability_zone")
                        else None
                    ),
                    spot_price=None,
                    status="ok",
                    process_seconds=None,
                    index_read_seconds=None,
                    commit=False,
                )
                count += 1

    conn.commit()
    return count


_VARIANTS_SUFFIX = ".variants.tsv"


def _na_float(cell: str) -> float | None:
    """Parse a holodeck eval numeric cell, mapping the literal `NA` to None."""
    return None if cell == "NA" else float(cell)


def _parse_eval_txt(path: Path) -> dict[str, Any]:
    """Parse a holodeck `<tool>.eval.txt` placement table.

    Columns: ``mapq_bin total correct mismapped unmapped pct_correct
    pct_mismapped pct_unmapped``; one row per MAPQ bin plus an ``ALL`` row.
    Returns ``{"bins": {label: {...}}, "all": {...}}`` — the per-bin breakdown
    (kept as JSON) plus the ``ALL`` row surfaced for the headline columns.
    """
    lines = path.read_text().strip().splitlines()
    if len(lines) < _MIN_TSV_LINES:
        raise ValueError(f"malformed eval.txt: {path}")
    header = lines[0].split("\t")
    bins: dict[str, dict[str, float]] = {}
    all_row: dict[str, float] | None = None
    for line in lines[1:]:
        row = dict(zip(header, line.split("\t"), strict=True))
        parsed = {
            "total": int(row["total"]),
            "correct": int(row["correct"]),
            "mismapped": int(row["mismapped"]),
            "unmapped": int(row["unmapped"]),
            "pct_correct": float(row["pct_correct"]),
            "pct_mismapped": float(row["pct_mismapped"]),
            "pct_unmapped": float(row["pct_unmapped"]),
        }
        if row["mapq_bin"] == "ALL":
            all_row = parsed
        else:
            bins[row["mapq_bin"]] = parsed
    # The ALL row carries the headline placement metrics; a header-only or
    # ALL-less file must fail rather than upsert NULL headline columns.
    if all_row is None:
        raise ValueError(f"malformed eval.txt missing ALL row: {path}")
    return {"bins": bins, "all": all_row}


def _parse_variants_tsv(path: Path) -> dict[str, Any]:
    """Parse a holodeck `<tool>.variants.tsv`.

    Columns: ``class confounded n_expected n_represented represented_pct
    mean_mapq mean_as`` per class, then ``#``-prefixed footer lines
    (``variant_bearing_reads``, ``md_concordant_pct``, ``nm_concordant_pct``).
    ``mean_as`` and the concordance footers may be the literal ``NA``.
    """
    lines = path.read_text().strip().splitlines()
    if not lines:
        raise ValueError(f"empty variants.tsv: {path}")
    header = lines[0].split("\t")
    by_class: dict[str, dict[str, Any]] = {}
    footer: dict[str, str] = {}
    for line in lines[1:]:
        if line.startswith("#"):
            key, _, val = line[1:].partition("\t")
            footer[key] = val
            continue
        row = dict(zip(header, line.split("\t"), strict=True))
        by_class[row["class"]] = {
            "confounded": row["confounded"] == "true",
            "n_expected": int(row["n_expected"]),
            "n_represented": int(row["n_represented"]),
            "represented_pct": float(row["represented_pct"]),
            "mean_mapq": float(row["mean_mapq"]),
            "mean_as": _na_float(row["mean_as"]),
        }
    # These footer metrics feed the headline accuracy table, so a truncated or
    # format-drifted file must fail loudly rather than masquerade as real data
    # with placeholder zeros.
    required = ("variant_bearing_reads", "md_concordant_pct", "nm_concordant_pct")
    missing = [key for key in required if key not in footer]
    if missing:
        raise ValueError(f"malformed variants.tsv missing footer keys {missing}: {path}")
    return {
        "by_class": by_class,
        "variant_bearing_reads": int(footer["variant_bearing_reads"]),
        "md_concordant_pct": _na_float(footer["md_concordant_pct"]),
        "nm_concordant_pct": _na_float(footer["nm_concordant_pct"]),
    }


def _parse_meth_tsv(path: Path) -> dict[str, Any] | None:
    """Parse a holodeck `<tool>.meth.tsv`, or None for the non-meth placeholder.

    Columns: ``n_cpg pearson_r rmse`` with a single data row (``pearson_r`` /
    ``rmse`` may be ``NA``). Returns None for the empty file the eval rule
    writes for non-meth samples (holodeck emits no `.meth.tsv` without --meth).
    """
    # Only a truly empty file is the non-meth placeholder the eval rule writes;
    # a header-only/truncated file is a corrupt meth artifact and must fail
    # rather than silently drop the methylation metrics.
    text = path.read_text().strip()
    if not text:
        return None
    lines = text.splitlines()
    if len(lines) < _MIN_TSV_LINES:
        raise ValueError(f"malformed meth.tsv: {path}")
    cells = lines[1].split("\t")
    return {
        "n_cpg": int(cells[0]),
        "pearson_r": _na_float(cells[1]),
        "rmse": _na_float(cells[2]),
    }


def ingest_accuracy(
    conn: sqlite3.Connection,
    *,
    runs_root: Path,
    fg_labs_sha: str,
) -> int:
    """Walk `runs_root/<sha>/<sample>/<arch>/rep-<n>/eval/` and populate `accuracy`.

    Each `eval/<tool>.variants.tsv` (with its sibling `.eval.txt` and `.meth.tsv`)
    is one aligner-arm accuracy cell; all arms of a run share `fg_labs_sha` and
    are disambiguated by the `tool` taken from the filename. Returns the number
    of rows upserted; 0 (without raising) if the run tree is missing, since
    accuracy is an optional input to ``cli collect`` (only the `accuracy` /
    `accuracy_smoke` targets produce it).

    :param conn: SQLite connection.
    :param runs_root: local mirror of the S3 ``runs/`` prefix.
    :param fg_labs_sha: fg-labs SHA whose run tree to ingest.
    """
    sha_dir = runs_root / fg_labs_sha
    if not sha_dir.is_dir():
        return 0

    upsert_run(conn, fg_labs_sha=fg_labs_sha, status="complete", commit=False)

    count = 0
    for sample_dir in sorted(d for d in sha_dir.iterdir() if d.is_dir()):
        sample = sample_dir.name
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            arch = arch_dir.name
            for rep_dir in sorted(d for d in arch_dir.iterdir() if d.is_dir()):
                if not rep_dir.name.startswith("rep-"):
                    continue
                rep = int(rep_dir.name.split("-", 1)[1])
                eval_dir = rep_dir / "eval"
                if not eval_dir.is_dir():
                    continue
                for variants_path in sorted(eval_dir.glob(f"*{_VARIANTS_SUFFIX}")):
                    tool = variants_path.name[: -len(_VARIANTS_SUFFIX)]
                    variants = _parse_variants_tsv(variants_path)
                    eval_txt = eval_dir / f"{tool}.eval.txt"
                    # `eval_accuracy` declares `.eval.txt` for every arm and
                    # `upsert_accuracy` overwrites on conflict, so a missing sibling
                    # means a partial/corrupt sync — fail rather than overwrite an
                    # existing row with NULL placement metrics.
                    if not eval_txt.exists():
                        raise FileNotFoundError(f"missing eval output for accuracy arm: {eval_txt}")
                    placement = _parse_eval_txt(eval_txt)
                    # The eval rule writes a (possibly empty) `.meth.tsv` for every
                    # arm, so a missing sibling is a partial sync — fail rather than
                    # treat it as None and overwrite stored meth metrics with NULL.
                    meth_path = eval_dir / f"{tool}.meth.tsv"
                    if not meth_path.exists():
                        raise FileNotFoundError(
                            f"missing meth output for accuracy arm: {meth_path}"
                        )
                    meth = _parse_meth_tsv(meth_path)
                    all_row = placement["all"]

                    upsert_accuracy(
                        conn,
                        fg_labs_sha=fg_labs_sha,
                        sample=sample,
                        arch=arch,
                        rep=rep,
                        tool=tool,
                        placement_total=all_row.get("total"),
                        placement_correct_pct=all_row.get("pct_correct"),
                        placement_mismapped_pct=all_row.get("pct_mismapped"),
                        placement_unmapped_pct=all_row.get("pct_unmapped"),
                        placement_json=json.dumps(placement),
                        variant_bearing_reads=variants["variant_bearing_reads"],
                        md_concordant_pct=variants["md_concordant_pct"],
                        nm_concordant_pct=variants["nm_concordant_pct"],
                        by_class_json=json.dumps(variants["by_class"]),
                        meth_n_cpg=(meth["n_cpg"] if meth else None),
                        meth_pearson_r=(meth["pearson_r"] if meth else None),
                        meth_rmse=(meth["rmse"] if meth else None),
                        commit=False,
                    )
                    count += 1

    conn.commit()
    return count


def ingest_scaling(
    conn: sqlite3.Connection,
    *,
    scaling_root: Path,
    fg_labs_sha: str,
) -> int:
    """Ingest thread-scaling ladders under ``scaling/<sha>/<sample>/<arch>/``.

    Each ``scaling.tsv`` is written by `align_thread_scaling` (one Batch job on
    one host) with a header plus one row per (threads, rep):
    ``threads, rep, wall_s, cpu_s, max_rss_mb, process_s, main_mem_s, read_io_s,
    sam_io_s, kernel_s``. Only the first six are required — the phase columns
    were added later, so a six-column row from an older ladder still loads, with
    NULL phases (see `_SCALING_COLUMNS`).

    :param conn: open benchmark DB connection.
    :param scaling_root: local mirror of the ``scaling/`` prefix.
    :param fg_labs_sha: run whose ladders should be ingested.
    :return: number of rows ingested (0 when the run has no ladder).
    """
    run_dir = scaling_root / fg_labs_sha
    if not run_dir.is_dir():
        return 0

    # The scaling ladder may be ingested for a SHA whose standard sweep was
    # never collected, so ensure the runs row exists; `complete` matches what
    # ingest_run stamps, and the ON CONFLICT upsert leaves it alone if the
    # sweep already created it.
    upsert_run(conn, fg_labs_sha=fg_labs_sha, status="complete", commit=False)

    ingested = 0
    for tsv in sorted(run_dir.glob("*/*/scaling.tsv")):
        sample = tsv.parent.parent.name
        arch = tsv.parent.name
        for line in tsv.read_text().splitlines()[1:]:  # skip header
            fields = line.split("\t")
            if len(fields) < _SCALING_COLUMNS:
                continue
            threads, rep, wall, cpu, rss, proc = fields[:_SCALING_COLUMNS]
            phases = fields[_SCALING_COLUMNS:_SCALING_COLUMNS_FULL]
            mainmem, readio, samio, kernel = (list(phases) + ["NA"] * 4)[:4]
            upsert_scaling(
                conn,
                fg_labs_sha=fg_labs_sha,
                sample=sample,
                arch=arch,
                threads=int(threads),
                rep=int(rep),
                wall_seconds=_maybe_float(wall),
                cpu_time=_maybe_float(cpu),
                max_rss_mb=_maybe_float(rss),
                # The rule writes the literal "NA" when PROCESS() could not be
                # parsed from the aligner's stderr; store NULL rather than
                # crashing, so one unparseable rung does not lose the ladder.
                process_seconds=_maybe_float(proc),
                main_mem_seconds=_maybe_float(mainmem),
                read_io_seconds=_maybe_float(readio),
                sam_io_seconds=_maybe_float(samio),
                kernel_seconds=_maybe_float(kernel),
                commit=False,
            )
            ingested += 1
    conn.commit()
    return ingested
