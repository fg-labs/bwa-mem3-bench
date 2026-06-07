"""Walk a local `runs/<sha>/` tree and populate SQLite."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from bwa_mem3_bench.storage import VS_BASELINE, VS_GOLDEN
from bwa_mem3_bench.storage.sqlite import (
    upsert_comparison,
    upsert_run,
    upsert_trial,
)

_TIMING_MIN_LINES = 2  # header + one data row

# compare-bams supplementary-disagreement fields, stored as a JSON blob in
# comparisons.supp_json. Absent on older comparison JSON (pre-supp-metrics).
_SUPP_KEYS = (
    "total_templates",
    "supp_query_total",
    "supp_baseline_total",
    "supp_count_mismatch_templates",
    "supp_count_mismatch_pct",
    "supp_unmatched",
    "supp_unmatched_pct",
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


def baseline_sha_for(tool_version: str) -> str:
    """Return the synthetic `fg_labs_sha` used for baseline trials of `tool_version`."""
    return f"{BASELINE_SHA_PREFIX}{tool_version}"


def _parse_timing_tsv(path: Path) -> dict[str, float]:
    """Parse Snakemake's benchmark directive output (single data row after header)."""
    text = path.read_text().strip().splitlines()
    if len(text) < _TIMING_MIN_LINES:
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

                for kind in (VS_BASELINE, VS_GOLDEN):
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
