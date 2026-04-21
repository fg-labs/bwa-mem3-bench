"""`collect` — sync S3 run artifacts (no BAMs) to scratch and populate SQLite."""

from __future__ import annotations

import sys
from pathlib import Path

from bwa_mem3_bench import DB_PATH, LOCAL_MIRROR_ROOT, REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.storage.ingest import ingest_baseline, ingest_run
from bwa_mem3_bench.storage.sqlite import connect
from bwa_mem3_bench.workflow_config import load_config

_DEFAULT_BUCKET = aws_config.load().bucket

# Analysis only needs benchmarks/timing.tsv, benchmarks/meta.json, and
# compare/*.json. BAMs are multi-GB and never opened locally — exclude them
# from the scratch mirror to save space and sync time.
_EXCLUDED_PATTERNS = ("*.bam", "*.bam.bai", "*.bam.csi")


def _sync_prefix(remote: str, local: str, *, dry_run: bool) -> None:
    cmd = ["aws", "s3", "sync", remote, local]
    for pat in _EXCLUDED_PATTERNS:
        cmd.extend(["--exclude", pat])
    run_cmd(cmd, dry_run=dry_run)


def collect(
    *,
    fg_labs_sha: str,
    bucket: str = _DEFAULT_BUCKET,
    ingest: bool = True,
    dry_run: bool = False,
) -> None:
    """Pull S3 artifacts (benchmarks, compare, meta) for a completed run.

    Mirrors both ``runs/<sha>/`` and ``baseline/`` into ``LOCAL_MIRROR_ROOT``
    (same layout as S3, override via ``BWA_MEM3_BENCH_LOCAL_MIRROR``), excluding
    large BAM files. Then populates ``benchmark.db``.

    :param fg_labs_sha: fg-labs SHA.
    :param bucket: source bucket. Default from `cdk/outputs.json` /
        `BWA_MEM3_BENCH_S3_BUCKET`.
    :param ingest: after sync, populate SQLite from the pulled artifacts.
    :param dry_run: print commands only.
    """
    runs_root = LOCAL_MIRROR_ROOT / "runs"
    baseline_root = LOCAL_MIRROR_ROOT / "baseline"
    run_dir = runs_root / fg_labs_sha

    if dry_run:
        _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=True)
        _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=True)
        if ingest:
            print(f"[dry-run] ingest {run_dir} → {DB_PATH}")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_root.mkdir(parents=True, exist_ok=True)
    _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=False)
    _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=False)

    if ingest:
        conn = connect(DB_PATH)
        try:
            n = ingest_run(conn, runs_root=runs_root, fg_labs_sha=fg_labs_sha)
            print(f"ingested {n} trials into {DB_PATH}", file=sys.stderr)

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
        finally:
            conn.close()


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
