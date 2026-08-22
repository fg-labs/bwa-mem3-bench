"""Walk a local `runs/<sha>/` tree and populate SQLite."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, NamedTuple

from bwa_mem3_bench.storage import VS_BASELINE, VS_DEFAULT, VS_GOLDEN, VS_X86
from bwa_mem3_bench.storage.sqlite import (
    upsert_accuracy,
    upsert_arena,
    upsert_comparison,
    upsert_host_probe,
    upsert_run,
    upsert_scaling,
    upsert_trial,
)

_MIN_TSV_LINES = 2  # header + at least one data row (timing, meth, etc.)

# The comparison kinds that produce a compare-bams JSON report, and so have
# something to ingest. A SUBSET of `workflow_config.COMPARE_KINDS`: `vs_bwa` is
# gated by `fgumi compare bams` and emits `bwa-identity.txt`, a pass/fail text
# report with no metrics.
#
# Ordered as the concordance chain reads: against upstream, against the previous
# release, ARM against x86, then the `--fast` preset against its own default.
# `test_ingest_covers_every_compare_json_the_workflow_produces` derives the same
# set from the rule outputs and fails if the two drift — a kind missing here is
# computed, paid for, and written to S3, then silently dropped on the way to the
# DB, which is how `vs-x86` went unrecorded for the project's whole history.
INGESTED_COMPARE_KINDS = (VS_BASELINE, VS_GOLDEN, VS_X86, VS_DEFAULT)

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

# arena.tsv: label, mode, rep, wall_s, cpu_s, max_rss_mb, process_s (see
# arena.smk's `printf` header).
_ARENA_COLUMNS = 7

# tachyon reports its working set in bytes; `host_probes` stores MB.
_BYTES_PER_MB = 1024 * 1024

_SECONDS_PER_HOUR = 3600.0

# The one sentinel `emit-host-meta` degrades ANY field to. Two fields map it to
# NULL, for two different reasons — the string is shared, the rationale is not,
# so do not collapse them. Only the first COMPARES against this constant.
#
# `_host_instance_id` (host attribution, written when IMDS is unreachable): mapped
# to NULL rather than stored verbatim, because that column exists to be JOINed on.
# Two unattributed cells both carrying the string "unknown" would MATCH each other
# and claim they shared a machine, which is the opposite of the truth. NULL never
# equals NULL in SQL, so an unattributed row correctly fails to join instead.
#
# `_meta_measured_at` (the measurement stamp, written when `date` or the JSON
# writer fails): never joined on, but NULL is still the honest value. The column
# is an audit field that gets ordered and compared as a date; the literal string
# would sort among real stamps and be indistinguishable from one. That is true of
# ANY non-date text, so it does not test for this constant — `_parse_stamp`
# rejects the sentinel and a corrupt stamp alike. Note this is only about what
# reaches `trials` — `late_cells` does not read the column at all, it re-reads
# `meta.json` via `_measured_at` and falls back to artifact mtime there.
#
# NOTE the instance_id case diverges from `trials.instance_id`, which stores the
# sentinel verbatim (see `ingest_run`). Left alone deliberately — changing it would
# rewrite the semantics of existing rows and belongs in its own change — so a
# trials-to-trials join on instance_id can still produce that false match.
_UNKNOWN_HOST = "unknown"

# How far a cell's measurement time may sit from its run's median before
# `late_cells` reports it. Sized against the real extremes: the v0.9.0
# `bless_release` coordinator ran 298 minutes end to end, so a whole release
# bench fits inside a quarter of this window and cannot trip it; the v0.8.0
# control that motivated the check sits 3 DAYS out, so it clears the threshold by
# two orders of magnitude. A resumed run that genuinely straddles a day boundary
# will trip it -- and should, since its cells were measured on other hosts on
# another day, which is precisely the comparability question this exists to
# raise. The operator then re-runs with the override.
LATE_CELL_THRESHOLD_HOURS = 24.0

# Bounds for validating probe-record numbers before they reach SQLite. A JSON
# integer has no width limit, so both of these are reachable from a corrupt
# producer and both raise rather than storing something wrong.
_FLOAT_MAX = sys.float_info.max
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


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


def _parse_stamp(value: str) -> datetime | None:
    """A `measured_at` stamp as an aware UTC datetime, or None if it is not one.

    Shared by this field's two readers — `_meta_measured_at`, which persists it,
    and `_measured_at`, which dates a cell by it — so that what reaches
    `trials.measured_at` cannot be text the lateness check itself refuses to
    read. The `unknown` sentinel fails here like any other non-date.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # `fromisoformat` accepts an offset-less stamp, and `.timestamp()` would then
    # read it in the READER's local zone -- so the same artifact would date
    # differently on two laptops, shifting a cell by the UTC offset across the
    # lateness threshold. The stamp is UTC by contract (`date -u` in
    # `emit-host-meta`), so say so.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _meta_measured_at(meta: dict[str, Any]) -> str | None:
    """`meta.json`'s `measured_at`, or None when absent or not a readable date.

    Stored verbatim rather than normalised: it is already UTC ISO-8601 from the
    worker, and reparsing to re-render it would only add a way to be wrong. It is
    still PARSED first, though — a truncated or hand-edited value would otherwise
    land in a column that gets ordered and compared as a date, indistinguishable
    from a real stamp, while `_measured_at` had already rejected it and dated the
    cell from mtime instead. The `unknown` sentinel is one such value; see the
    note above `_UNKNOWN_HOST` for why NULL is the honest answer for all of them.
    """
    value = meta.get("measured_at")
    if not isinstance(value, str) or _parse_stamp(value) is None:
        return None
    return value


class LateCell(NamedTuple):
    """A cell whose artifacts were written far outside the run's own window."""

    sample: str
    arch: str
    rep: int
    measured_at: str
    hours_late: float

    @property
    def key(self) -> tuple[str, str, int]:
        """The `(sample, arch, rep)` identity `ingest_run`'s `exclude` set uses."""
        return (self.sample, self.arch, self.rep)

    @property
    def is_early(self) -> bool:
        """Whether the cell PRECEDES the run's median rather than following it.

        Load-bearing rather than descriptive: `collect` reports both directions
        but skips only the forward one. The reference is a MEDIAN, so if the
        foreign cells are ever the majority the median sits inside the FOREIGN
        group and it is the run's own cells that read as far from it — in this
        direction. Skipping those would drop the release's real measurements and
        keep the control's, which is worse than having no guard at all.

        Detection stays symmetric because an early outlier is real evidence: the
        v0.8.0 golden holds six cells dated the day BEFORE it was blessed. But
        which side of a split is the foreign one is genuinely ambiguous, so this
        direction is the operator's call, not an automatic exclusion.
        """
        return self.hours_late < 0


def _measured_at(rep_dir: Path) -> tuple[str, float] | None:
    """When a cell was measured, as ``(display, epoch_seconds)``, or None.

    Prefers ``meta.json``'s ``measured_at``, which the worker stamps in its own
    shell and is therefore authoritative. Falls back to the mtime of
    ``timing.tsv`` for cells written before that field existed — which is every
    historical artifact, so the fallback is the common path today, not an edge
    case. It is trustworthy here because `aws s3 sync` sets the local mtime from
    S3's LastModified: spot-checked against the S3 listing for three cells of the
    v0.8.0 golden, all three matched to the second.
    """
    meta_path = rep_dir / "benchmarks" / "meta.json"
    # Guarded, not assumed: `_parse_json_file` raises on a missing or malformed
    # file, and the cells this function most needs to date are exactly the old
    # ones that predate `meta.json` entirely.
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = _parse_json_file(meta_path)
        except (OSError, ValueError):
            meta = {}
    stamped = meta.get("measured_at")
    if isinstance(stamped, str):
        parsed = _parse_stamp(stamped)
        # None for the `unknown` sentinel or a corrupt stamp; fall through to mtime.
        if parsed is not None:
            return stamped, parsed.timestamp()
    timing = rep_dir / "benchmarks" / "timing.tsv"
    if not timing.exists():
        return None
    mtime = timing.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), mtime


def late_cells(
    *,
    runs_root: Path,
    fg_labs_sha: str,
    threshold_hours: float = LATE_CELL_THRESHOLD_HOURS,
) -> list[LateCell]:
    """Cells measured more than ``threshold_hours`` from the run's median time.

    WHY. A run's S3 prefix is not proof that everything under it belongs to that
    run. Re-running one sample against an old SHA — a control, a bisect, a
    one-off reproduction — writes into `runs/<that-sha>/`, and `collect` then
    folds those measurements into the release's record, shifting the medians
    every future perf gate compares against. This is not hypothetical: the v0.8.0
    golden's tree holds 23 such cells from a control run taken three days later,
    on that day's hosts, where the same binary measured 18.7% slower than its own
    recorded number.

    Compared against the MEDIAN rather than the earliest or latest cell, so a
    handful of late arrivals cannot drag the reference with them, and so the
    check still works if the contamination is the majority (the smaller group is
    flagged either way — which is the useful outcome, since it names a group to
    look at rather than silently picking one).

    That is why cells on BOTH sides of the reference are returned, and why
    `hours_late` is signed. Reporting is symmetric; the automatic exclusion in
    `collect` is not, because in the majority case the flagged group is the
    run's own — see `LateCell.is_early`.

    Cells whose time cannot be determined at all are NOT reported: absent
    evidence is not evidence of contamination, and flagging them would train the
    reader to ignore this list.
    """
    sha_dir = runs_root / fg_labs_sha
    if not sha_dir.is_dir():
        return []

    stamped: list[tuple[str, str, int, str, float]] = []
    for sample_dir in sorted(d for d in sha_dir.iterdir() if d.is_dir()):
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            for rep_dir in sorted(d for d in arch_dir.iterdir() if d.is_dir()):
                if not rep_dir.name.startswith("rep-"):
                    continue
                when = _measured_at(rep_dir)
                if when is None:
                    continue
                display, epoch = when
                rep = int(rep_dir.name.split("-", 1)[1])
                stamped.append((sample_dir.name, arch_dir.name, rep, display, epoch))

    if not stamped:
        return []
    reference = median(epoch for *_, epoch in stamped)
    threshold_seconds = threshold_hours * _SECONDS_PER_HOUR
    return sorted(
        (
            LateCell(sample, arch, rep, display, (epoch - reference) / _SECONDS_PER_HOUR)
            for sample, arch, rep, display, epoch in stamped
            if abs(epoch - reference) > threshold_seconds
        ),
        key=lambda cell: (cell.sample, cell.arch, cell.rep),
    )


def ingest_run(
    conn: sqlite3.Connection,
    *,
    runs_root: Path,
    fg_labs_sha: str,
    exclude: frozenset[tuple[str, str, int]] = frozenset(),
) -> int:
    """Walk `runs_root/<fg_labs_sha>/<sample>/<arch>/rep-<n>/` and populate DB.

    `benchmarks/host-probe.jsonl` is read when present, same as
    `ingest_scaling`'s handling of the ladder's job-level probe — but keyed
    per rep here, since each rep of the regular sweep is an independent Batch
    job that may land on a different host, unlike the ladder's one-job-one-host
    design. Older cells (collected before per-cell probing existed) simply
    lack the file and are skipped without complaint.

    :param exclude: `(sample, arch, rep)` cells to skip — used by `collect` to
        keep cells measured AFTER the run's window out of the release record.
        The forward-only subset of `late_cells`, not all of it: an early cell is
        reported but ingested, since the median can sit inside a contaminating
        group (see `LateCell.is_early`). Empty by default, so every other caller
        is unchanged.
    """
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
                if (sample, arch, rep) in exclude:
                    continue

                timing_path = rep_dir / "benchmarks" / "timing.tsv"
                meta_path = rep_dir / "benchmarks" / "meta.json"
                bwa_stderr_path = rep_dir / "benchmarks" / "bwa.stderr.log"
                if not timing_path.exists():
                    continue
                timing = _parse_timing_tsv(timing_path)
                meta: dict[str, Any] = _parse_json_file(meta_path) if meta_path.exists() else {}
                process_seconds, index_read_seconds = _parse_bwa_stderr(bwa_stderr_path)

                # Read count comes from whichever comparison the cell actually
                # ran. ARM cells have no `vs-baseline` (no upstream ARM build),
                # so keying on it alone left `reads_processed` at 0 for every
                # ARM trial. Ordered by preference: `vs-baseline` first so an
                # x86 cell keeps reporting the count it always has.
                reads_processed = 0
                for kind in INGESTED_COMPARE_KINDS:
                    path = rep_dir / "compare" / f"{kind}.json"
                    if path.exists():
                        reads_processed = int(_parse_json_file(path).get("total_reads", 0))
                        break

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
                    instance_id=(str(meta.get("instance_id")) if meta.get("instance_id") else None),
                    measured_at=_meta_measured_at(meta),
                    spot_price=None,
                    status="ok",
                    process_seconds=process_seconds,
                    index_read_seconds=index_read_seconds,
                    commit=False,
                )

                # Per-cell tachyon contention readings, present from the release
                # that added them onward — older cells simply have no file, same
                # as ingest_scaling's own host-probe handling.
                _ingest_host_probes(
                    conn,
                    probe_path=rep_dir / "benchmarks" / "host-probe.jsonl",
                    fg_labs_sha=fg_labs_sha,
                    sample=sample,
                    arch=arch,
                    rep=rep,
                    instance_id=_host_instance_id(meta_path),
                )

                for kind in INGESTED_COMPARE_KINDS:
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
                    instance_id=(str(meta.get("instance_id")) if meta.get("instance_id") else None),
                    measured_at=_meta_measured_at(meta),
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
                    measured_at=_meta_measured_at(meta),
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
    exclude: frozenset[tuple[str, str, int]] = frozenset(),
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
    :param exclude: `(sample, arch, rep)` cells to skip, same set `ingest_run`
        takes. `accuracy` is keyed on its own tuple and does NOT reference
        `trials.id`, so excluding a cell from `trials` alone would leave its
        accuracy rows behind, still read by every accuracy report. Two of the
        v0.8.0 control's samples are truth samples, so this path is live.
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
                if (sample, arch, rep) in exclude:
                    continue
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


def _host_instance_id(meta_path: Path) -> str | None:
    """The EC2 instance id from a ``meta.json``, or None if unavailable.

    None covers all three ways it can be missing: no file at all (a ladder
    collected before the rule emitted one), no key, and the ``unknown`` sentinel
    a worker writes when IMDS is unreachable.
    """
    if not meta_path.exists():
        return None
    value = _parse_json_file(meta_path).get("instance_id")
    if not value or value == _UNKNOWN_HOST:
        return None
    return str(value)


def _probe_text(record: dict[str, Any], key: str) -> str | None:
    """A string field of a probe record, or None if it is not a string.

    Type-checked rather than coerced with ``str()``: stringifying a stray dict
    would persist ``"{}"`` as though the probe had reported it.
    """
    value = record.get(key)
    return value if isinstance(value, str) else None


def _probe_number(record: dict[str, Any], key: str) -> float | None:
    """A finite numeric field of a probe record, or None.

    Rejects four things that all reach SQLite as something other than a usable
    measurement: non-numeric types (a dict raises on parameter binding), bools
    (``True`` is an int in Python and would store as 1), non-finite floats —
    `json.loads` accepts the ``NaN`` / ``Infinity`` literals by default, and
    SQLite silently stores NaN as NULL while keeping inf as inf, so neither
    survives as the number it claimed to be — and integers too large to convert.

    That last one is the non-obvious case: a JSON integer is UNBOUNDED, so both
    ``float()`` and ``math.isfinite()`` raise ``OverflowError`` on one that
    exceeds a double. The magnitude check has to come first, and it compares
    against a float without converting, because Python's int/float comparison is
    exact and cannot overflow. A `float` operand needs no such check —
    `json.loads` already maps an over-large literal to ``inf``, which the
    finiteness test rejects.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and not -_FLOAT_MAX <= value <= _FLOAT_MAX:
        return None
    return float(value) if math.isfinite(value) else None


def _probe_int(record: dict[str, Any], key: str) -> int | None:
    """An integer field of a probe record, or None.

    Bools are not integers here (``True`` would store as 1, indistinguishable
    from a real single-threaded reading), and neither is an integer outside
    SQLite's signed 64-bit INTEGER range — binding one raises ``OverflowError``,
    which would abort the whole ingest over a diagnostic field.
    """
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None


def _ingest_host_probes(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    probe_path: Path,
    fg_labs_sha: str,
    sample: str,
    arch: str,
    instance_id: str | None,
    rep: int = 0,
) -> None:
    """Ingest tachyon readings from a ``host-probe.jsonl``.

    One JSON record per line, as appended by `emit-host-probe`. A record whose
    probe could not run carries ``status: unavailable`` with null measurements;
    it is still stored, because "we tried and could not measure" is a different
    fact from "we never looked" and only the row distinguishes them.

    :param rep: 0 (default) for the thread-scaling ladder's job-level probe;
        the real rep for a per-cell probe from the regular sweep, where each
        rep is an independent Batch job that may land on a different host.

    Nothing here may be fatal: these readings are diagnostic, and one malformed
    record must not cost the ladder its rungs. That takes more than catching a
    JSON decode error — a *well-formed* record whose field holds an object or list
    (``{"threads": {}}``) raises on SQLite parameter binding, which aborts the
    whole `ingest_scaling` call and persists ZERO rungs of a ~45-minute ladder.
    Measured, not theorised. So every field is type-checked before the upsert, and
    a field that fails becomes NULL — the same shape an `unavailable` reading
    already produces, which is a state every consumer must already handle.

    A record is skipped outright only when ``phase`` is unusable, because that is
    the row's identity and its column is NOT NULL.
    """
    if not probe_path.exists():
        return
    for line in probe_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        phase = _probe_text(record, "phase")
        if not phase:
            continue
        working_set = _probe_number(record, "working_set_bytes_per_thread")
        upsert_host_probe(
            conn,
            fg_labs_sha=fg_labs_sha,
            sample=sample,
            arch=arch,
            rep=rep,
            phase=phase,
            instance_id=instance_id,
            probe_version=_probe_text(record, "probe_version"),
            rustc=_probe_text(record, "rustc"),
            m_accesses_per_sec=_probe_number(record, "million_accesses_per_sec"),
            ns_per_access=_probe_number(record, "ns_per_access"),
            threads=_probe_int(record, "threads"),
            # Stored in MB because that is the unit the probe's own docs reason
            # in ("64 MB per thread clears the LLC of every instance type this
            # project benchmarks"), and comparing a recorded working set to that
            # guidance should not require dividing by 2^20 first.
            working_set_mb_per_thread=(
                working_set / _BYTES_PER_MB if working_set is not None else None
            ),
            seconds=_probe_number(record, "seconds"),
            status=_probe_text(record, "status"),
            commit=False,
        )


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

    Two sibling artifacts are read when present, and skipped without complaint
    when not — ladders collected before they existed still ingest:
    ``meta.json`` (the host the whole ladder ran on) and ``host-probe.jsonl``
    (tachyon contention readings, one JSON record per line).

    :param conn: open benchmark DB connection.
    :param scaling_root: local mirror of the ``scaling/`` prefix.
    :param fg_labs_sha: run whose ladders should be ingested.
    :return: number of rows ingested (0 when the run has no ladder). Counts
        ladder rungs only; host probes are attached to the cell, not rungs, and
        are not benchmark measurements.
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
        # One host for the whole ladder, by construction (see the rule docstring),
        # so one lookup serves every rung below.
        instance_id = _host_instance_id(tsv.parent / "meta.json")
        _ingest_host_probes(
            conn,
            probe_path=tsv.parent / "host-probe.jsonl",
            fg_labs_sha=fg_labs_sha,
            sample=sample,
            arch=arch,
            instance_id=instance_id,
        )
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
                instance_id=instance_id,
                commit=False,
            )
            ingested += 1
    conn.commit()
    return ingested


def ingest_arena(
    conn: sqlite3.Connection,
    *,
    arena_root: Path,
    fg_labs_sha: str,
    sample: str,
) -> int:
    """Ingest arena runs under ``arena/<sha>/<arch>/``.

    Each ``arena.tsv`` is written by `align_arena` (one on-demand Batch job on
    one host per arch) with a header plus one row per (label, mode, rep):
    ``label, mode, rep, wall_s, cpu_s, max_rss_mb, process_s``. A SKIPPED arm
    (arena.smk's "Never hard-fail on an old binary") writes the literal "NA"
    for every numeric field, which loads as NULL -- see `upsert_arena`.

    Two sibling artifacts are read when present, skipped without complaint
    when not, exactly as `ingest_scaling` handles its own: ``meta.json`` (the
    host the whole arena job ran on) and ``host-probe.jsonl`` (tachyon
    contention readings).

    :param conn: open benchmark DB connection.
    :param arena_root: local mirror of the ``arena/`` prefix.
    :param fg_labs_sha: run whose arena outputs should be ingested.
    :param sample: the sample the arena measured (``arena.sample`` in
        config/defaults.yaml) -- not recoverable from the tree layout itself
        (``arena/<sha>/<arch>/``, unlike the regular sweep's
        ``runs/<sha>/<sample>/<arch>/``), so the caller supplies it. Recorded
        onto ``host_probes`` only, so a probe taken during the arena can be
        joined against other runs against the same sample.
    :return: number of rows ingested (0 when the run has no arena).
    """
    run_dir = arena_root / fg_labs_sha
    if not run_dir.is_dir():
        return 0

    # The arena may be ingested for a SHA whose standard sweep was never
    # collected (e.g. a targeted `--target arena` re-run), so ensure the runs
    # row exists -- mirrors `ingest_scaling`'s own upsert-first pattern.
    upsert_run(conn, fg_labs_sha=fg_labs_sha, status="complete", commit=False)

    ingested = 0
    for tsv in sorted(run_dir.glob("*/arena.tsv")):
        arch = tsv.parent.name
        # One host for the whole arena job, by construction (see the rule
        # docstring), so one lookup serves every arm+rep below.
        instance_id = _host_instance_id(tsv.parent / "meta.json")
        _ingest_host_probes(
            conn,
            probe_path=tsv.parent / "host-probe.jsonl",
            fg_labs_sha=fg_labs_sha,
            sample=sample,
            arch=arch,
            instance_id=instance_id,
        )
        for line in tsv.read_text().splitlines()[1:]:  # skip header
            fields = line.split("\t")
            if len(fields) != _ARENA_COLUMNS:
                continue
            label, mode, rep, wall, cpu, rss, proc = fields
            upsert_arena(
                conn,
                fg_labs_sha=fg_labs_sha,
                arch=arch,
                label=label,
                mode=mode,
                rep=int(rep),
                wall_seconds=_maybe_float(wall),
                cpu_time=_maybe_float(cpu),
                max_rss_mb=_maybe_float(rss),
                process_seconds=_maybe_float(proc),
                instance_id=instance_id,
                commit=False,
            )
            ingested += 1
    conn.commit()
    return ingested
