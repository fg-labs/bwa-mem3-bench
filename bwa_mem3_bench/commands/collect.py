"""`collect` — sync S3 run artifacts (no BAMs) to scratch and populate SQLite."""

from __future__ import annotations

import sys
from pathlib import Path

from bwa_mem3_bench import DB_PATH, LOCAL_MIRROR_ROOT, REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.storage.ingest import (
    LATE_CELL_THRESHOLD_HOURS,
    LateCell,
    ingest_accuracy,
    ingest_baseline,
    ingest_minibwa,
    ingest_run,
    ingest_scaling,
    late_cells,
)
from bwa_mem3_bench.storage.sqlite import connect
from bwa_mem3_bench.workflow_config import load_config

_DEFAULT_BUCKET = aws_config.load().bucket

# Analysis only needs benchmarks/timing.tsv, benchmarks/meta.json, and
# compare/*.json. BAMs are multi-GB and never opened locally — exclude them
# from the scratch mirror to save space and sync time.
_EXCLUDED_PATTERNS = ("*.bam", "*.bam.bai", "*.bam.csi")


def _sync_prefix(remote: str, local: str, *, dry_run: bool) -> None:
    # `--exact-timestamps`: re-download whenever the S3 timestamp differs, even
    # when the size is unchanged. Re-running a rule against a fixed SHA (e.g. a
    # new holodeck build re-emitting eval TSVs) commonly produces a same-sized
    # file with different content; without this, `aws s3 sync`'s default
    # size+newer-than heuristic silently keeps the stale local copy and ingest
    # reports old numbers. S3 is authoritative here, so always pull the latest.
    cmd = ["aws", "s3", "sync", remote, local, "--exact-timestamps"]
    for pat in _EXCLUDED_PATTERNS:
        cmd.extend(["--exclude", pat])
    run_cmd(cmd, dry_run=dry_run)


def _report_late_cells(cells: list[LateCell], *, ingesting: bool) -> None:
    """Print the late-cell warning. Loud by design — it is the whole mechanism."""
    verb = "INGESTING" if ingesting else "SKIPPING"
    print(
        f"\n!! {len(cells)} cell(s) were measured more than "
        f"{LATE_CELL_THRESHOLD_HOURS:g}h from this run's median time — {verb} them.",
        file=sys.stderr,
    )
    for cell in cells:
        print(
            f"     {cell.sample}/{cell.arch}/rep-{cell.rep}  "
            f"{cell.measured_at}  ({cell.hours_late:+.1f}h)",
            file=sys.stderr,
        )
    if ingesting:
        print(
            "   These are folded into the run's record because --ingest-late-cells\n"
            "   was passed. If they are from a control or reproduction run, they will\n"
            "   shift the medians every future perf gate compares against.",
            file=sys.stderr,
        )
    else:
        print(
            "   A run's S3 prefix is not proof that everything under it belongs to\n"
            "   that run: re-running one sample against an old SHA writes into that\n"
            "   SHA's tree. Pass --ingest-late-cells if they really are part of it.\n"
            "   NOTE this skips the WRITE only. Both ingests upsert, so rows an\n"
            "   earlier collect already wrote for these cells are still in the DB\n"
            "   and every report still reads them. Clear them once, by key:\n"
            "     DELETE FROM trials   WHERE fg_labs_sha=? AND sample=? AND arch=? AND rep=?;\n"
            "     DELETE FROM accuracy WHERE fg_labs_sha=? AND sample=? AND arch=? AND rep=?;",
            file=sys.stderr,
        )


def collect(
    *,
    fg_labs_sha: str,
    bucket: str = _DEFAULT_BUCKET,
    ingest: bool = True,
    ingest_late_cells: bool = False,
    dry_run: bool = False,
) -> None:
    """Pull S3 artifacts (benchmarks, compare, meta) for a completed run.

    Mirrors both ``runs/<sha>/`` and ``baseline/`` into ``LOCAL_MIRROR_ROOT``
    (same layout as S3, override via ``BWA_MEM3_BENCH_LOCAL_MIRROR``), excluding
    large BAM files. Then populates ``benchmark.db``.

    Cells measured far outside the run's own time window are reported and
    SKIPPED by default — see `late_cells`. The v0.8.0 golden's tree holds 23 such
    cells from a control run taken three days later, and ingesting them shifts
    the medians every future perf gate reads.

    :param fg_labs_sha: fg-labs SHA.
    :param bucket: source bucket. Default from `cdk/outputs.json` /
        `BWA_MEM3_BENCH_S3_BUCKET`.
    :param ingest: after sync, populate SQLite from the pulled artifacts.
    :param ingest_late_cells: also ingest cells measured outside the run's
        window. Use when a run legitimately spans days (e.g. resumed after a
        failure), NOT to silence the warning on a control run.
    :param dry_run: print commands only.
    """
    runs_root = LOCAL_MIRROR_ROOT / "runs"
    baseline_root = LOCAL_MIRROR_ROOT / "baseline"
    minibwa_root = LOCAL_MIRROR_ROOT / "minibwa"
    scaling_root = LOCAL_MIRROR_ROOT / "scaling"
    run_dir = runs_root / fg_labs_sha

    if dry_run:
        _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=True)
        _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=True)
        _sync_prefix(f"s3://{bucket}/minibwa/", str(minibwa_root), dry_run=True)
        _sync_prefix(
            f"s3://{bucket}/scaling/{fg_labs_sha}/", str(scaling_root / fg_labs_sha), dry_run=True
        )
        if ingest:
            print(f"[dry-run] ingest {run_dir} → {DB_PATH}")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_root.mkdir(parents=True, exist_ok=True)
    minibwa_root.mkdir(parents=True, exist_ok=True)
    (scaling_root / fg_labs_sha).mkdir(parents=True, exist_ok=True)
    _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=False)
    _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=False)
    _sync_prefix(f"s3://{bucket}/minibwa/", str(minibwa_root), dry_run=False)
    # Thread-scaling ladders are per-SHA (unlike the SHA-independent baseline
    # and minibwa caches), so sync only this run's subtree.
    _sync_prefix(
        f"s3://{bucket}/scaling/{fg_labs_sha}/", str(scaling_root / fg_labs_sha), dry_run=False
    )

    if ingest:
        # Resolved from the freshly-synced tree, before anything is written to
        # the DB, so the operator sees the warning even if ingest later fails.
        late = late_cells(runs_root=runs_root, fg_labs_sha=fg_labs_sha)
        if late:
            _report_late_cells(late, ingesting=ingest_late_cells)
        exclude = frozenset() if ingest_late_cells else frozenset(c.key for c in late)

        conn = connect(DB_PATH)
        try:
            n = ingest_run(conn, runs_root=runs_root, fg_labs_sha=fg_labs_sha, exclude=exclude)
            print(f"ingested {n} trials into {DB_PATH}", file=sys.stderr)

            # Truth-based accuracy rows (holodeck eval) live alongside the timing
            # trees under runs/<sha>/.../eval/; only present for sim samples run
            # via the `accuracy` / `accuracy_smoke` targets.
            a = ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=fg_labs_sha, exclude=exclude)
            if a:
                print(f"ingested {a} accuracy rows into {DB_PATH}", file=sys.stderr)

            # Thread-scaling ladder (--target thread_scaling). Absent for runs
            # that did not request it, hence the truthiness guard.
            sc = ingest_scaling(conn, scaling_root=scaling_root, fg_labs_sha=fg_labs_sha)
            if sc:
                print(f"ingested {sc} scaling rows into {DB_PATH}", file=sys.stderr)

            # Ingest baselines from each upstream tag known to the workflow
            # config so that `bench report`/`bench speedup` can join fg-labs
            # walls against baseline walls without an extra CLI step.
            for tool_version in _baseline_tool_versions():
                m = ingest_baseline(
                    conn,
                    baseline_root=baseline_root,
                    tool_version=tool_version,
                )
                if m:
                    print(
                        f"ingested {m} baseline trials (bwa-mem2-{tool_version}) into {DB_PATH}",
                        file=sys.stderr,
                    )

            # Ingest every minibwa SHA present in the synced cache. Each
            # `minibwa/<sha>/` subtree is an independent tool pin; discovering
            # them here (rather than threading a single SHA) means `collect`
            # picks up whatever minibwa runs exist without extra arguments.
            for sha in _minibwa_shas(minibwa_root):
                k = ingest_minibwa(conn, minibwa_root=minibwa_root, minibwa_sha=sha)
                if k:
                    print(
                        f"ingested {k} minibwa trials (minibwa-{sha}) into {DB_PATH}",
                        file=sys.stderr,
                    )
        finally:
            conn.close()


def _minibwa_shas(minibwa_root: Path) -> list[str]:
    """Discover minibwa SHAs present under the synced `minibwa/` mirror.

    Each immediate subdirectory is a `<minibwa_sha>/` cache tree. Returns an
    empty list when the mirror is absent (no minibwa runs collected yet).
    """
    if not minibwa_root.is_dir():
        return []
    return sorted(d.name for d in minibwa_root.iterdir() if d.is_dir())


def _baseline_tool_versions() -> list[str]:
    """Upstream tags whose `baseline/bwa-mem2-<tag>/` should be ingested.

    Reads `config/defaults.yaml` via `workflow_config.load_config`. Falls back
    to an empty list if the config can't be read (e.g. in environments where
    the YAMLs are not present), so `collect --ingest` never fails purely on
    missing baseline ingestion.
    """
    try:
        cfg = load_config(Path(REPO_ROOT) / "config")
    except (FileNotFoundError, KeyError, OSError):
        return []
    return [cfg.upstream_tag]
