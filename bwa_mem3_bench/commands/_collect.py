"""`collect` — sync S3 run artifacts (no BAMs) to scratch and populate SQLite."""

from __future__ import annotations

import sys
from fnmatch import fnmatch
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from bwa_mem3_bench import DB_PATH, LOCAL_MIRROR_ROOT, REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.storage.ingest import (
    LATE_CELL_THRESHOLD_HOURS,
    LateCell,
    ingest_accuracy,
    ingest_arena,
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

# Cap shared by both per-row reports below, so neither a pathological mirror nor
# a heavily contaminated run tree can bury the ingest summary that follows them.
# The count in each header is always exact — only the listing is truncated.
_MAX_ROWS_LISTED = 20

# Top-level names under `runs/<sha>/` that `bench` WRITES into the mirror and S3
# therefore never has. Without this every `collect` would report a stale
# `regression.md` as an orphan on every run that has ever been reported on --
# noise that teaches the reader to skip the list, which is worse than not having
# it. Kept in sync with the default output paths in `commands/bench.py` by
# `test_locally_written_names_match_bench_defaults`.
_LOCALLY_WRITTEN = frozenset(
    {"summary.md", "compare.md", "regression.md", "report", "full-report", "docs"}
)


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


def _s3_keys(bucket: str, prefix: str) -> set[str]:
    """Every object key under `prefix`, as paths relative to it.

    Paginated: a run tree is thousands of objects and `list_objects_v2` caps a
    page at 1000, so an unpaginated call would silently report the tail of the
    run as orphaned.
    """
    paginator = boto3.client("s3", region_name=aws_config.load().region).get_paginator(
        "list_objects_v2"
    )
    keys: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.update(obj["Key"][len(prefix) :] for obj in page.get("Contents", ()))
    return keys


def _orphaned_files(local_root: Path, remote_keys: set[str]) -> list[Path]:
    """Local mirror files with no counterpart in S3, as paths relative to the root.

    `aws s3 sync` is deliberately run WITHOUT `--delete` (the `baseline/` and
    `minibwa/` caches are shared across runs and must survive a single-run sync),
    and ingest walks the LOCAL tree. So an object deleted from S3 stays in the
    mirror forever and keeps being ingested — the DB can hold rows the
    authoritative bucket has no evidence for.

    BAMs are excluded from the mirror by `_EXCLUDED_PATTERNS`, so they are never
    local and cannot show up here. The same patterns are re-checked below against
    each LOCAL file's name anyway — belt and braces, since `aws cleanup-s3` deliberately
    reaps `aligned.bam` from S3, and a mirror that ever did hold one would
    otherwise report it as an orphan of another command's normal operation.
    """
    orphans: list[Path] = []
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root)
        # `bench` writes its reports here; they are local by design, not orphans.
        # Matched on the FIRST path component so the directory-valued outputs
        # (`report/`, `full-report/`) exclude their whole subtree.
        if rel.parts[0] in _LOCALLY_WRITTEN:
            continue
        if any(fnmatch(rel.name, pattern) for pattern in _EXCLUDED_PATTERNS):
            continue
        if rel.as_posix() not in remote_keys:
            orphans.append(rel)
    return orphans


def _report_orphans(orphans: list[Path], local_root: Path) -> None:
    """Report local files S3 no longer has. Report only — never delete."""
    print(
        f"\n!! {len(orphans)} file(s) under {local_root} have no counterpart in S3.",
        file=sys.stderr,
    )
    for rel in orphans[:_MAX_ROWS_LISTED]:
        print(f"     {rel}", file=sys.stderr)
    if len(orphans) > _MAX_ROWS_LISTED:
        print(f"     ... and {len(orphans) - _MAX_ROWS_LISTED} more", file=sys.stderr)
    print(
        "   `aws s3 sync` never deletes and ingest walks the LOCAL tree, so these\n"
        "   were ingested anyway. They are usually leftovers from a run whose S3\n"
        "   outputs were deliberately removed. Delete them from the mirror if so.",
        file=sys.stderr,
    )


def _report_late_cells(
    cells: list[LateCell],
    *,
    excluded: frozenset[tuple[str, str, int]],
    overridden: bool,
) -> None:
    """Print the late-cell warning. Loud by design — it is the whole mechanism.

    :param excluded: the set `_ingest_all` will actually pass to the ingests.
        Taken rather than re-derived so the report cannot claim one disposition
        while the DB gets another.
    :param overridden: whether `--ingest-late-cells` was passed. Not inferable
        from an empty `excluded`, which is also what a run with only early cells
        produces — and those two need opposite advice.
    """
    print(
        f"\n!! {len(cells)} cell(s) were measured more than "
        f"{LATE_CELL_THRESHOLD_HOURS:g}h from this run's median time — "
        f"SKIPPING {len(excluded)}, INGESTING {len(cells) - len(excluded)}.",
        file=sys.stderr,
    )
    for cell in cells[:_MAX_ROWS_LISTED]:
        disposition = "skip  " if cell.key in excluded else "ingest"
        print(
            f"     {disposition}  {cell.sample}/{cell.arch}/rep-{cell.rep}  "
            f"{cell.measured_at}  ({cell.hours_late:+.1f}h)",
            file=sys.stderr,
        )
    if len(cells) > _MAX_ROWS_LISTED:
        print(f"     ... and {len(cells) - _MAX_ROWS_LISTED} more", file=sys.stderr)
    if any(cell.is_early for cell in cells):
        print(
            "   The (-) cells PRECEDE this run's median, so they are INGESTED, not\n"
            "   skipped. The reference is a median: when foreign cells outnumber the\n"
            "   run's own, the median lands among THEM and the run's real cells are\n"
            "   what read as early. Skipping those would keep the foreign\n"
            "   measurements and drop the good ones, so which side is foreign here\n"
            "   is your call — check the (-) cells' dates against when the run ran.",
            file=sys.stderr,
        )
    if excluded:
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
    if overridden:
        print(
            "   Nothing is skipped because --ingest-late-cells was passed. If any of\n"
            "   these are from a control or reproduction run, they will shift the\n"
            "   medians every future perf gate compares against.",
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

    Cells measured far outside the run's own time window are always reported —
    see `late_cells`. The v0.8.0 golden's tree holds 23 such cells from a control
    run taken three days later, and ingesting them shifts the medians every
    future perf gate reads. Only cells measured AFTER the run's median are
    skipped: the reference is a median, so when foreign cells are the majority it
    is the run's OWN cells that read as far from it, and skipping those would
    keep the foreign measurements and drop the good ones. Early cells are
    therefore reported for the operator to judge, and ingested.

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
    arena_root = LOCAL_MIRROR_ROOT / "arena"
    run_dir = runs_root / fg_labs_sha

    if dry_run:
        _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=True)
        _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=True)
        _sync_prefix(f"s3://{bucket}/minibwa/", str(minibwa_root), dry_run=True)
        _sync_prefix(
            f"s3://{bucket}/scaling/{fg_labs_sha}/", str(scaling_root / fg_labs_sha), dry_run=True
        )
        _sync_prefix(
            f"s3://{bucket}/arena/{fg_labs_sha}/", str(arena_root / fg_labs_sha), dry_run=True
        )
        if ingest:
            print(f"[dry-run] ingest {run_dir} → {DB_PATH}")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_root.mkdir(parents=True, exist_ok=True)
    minibwa_root.mkdir(parents=True, exist_ok=True)
    (scaling_root / fg_labs_sha).mkdir(parents=True, exist_ok=True)
    (arena_root / fg_labs_sha).mkdir(parents=True, exist_ok=True)
    _sync_prefix(f"s3://{bucket}/runs/{fg_labs_sha}/", str(run_dir), dry_run=False)
    _sync_prefix(f"s3://{bucket}/baseline/", str(baseline_root), dry_run=False)
    _sync_prefix(f"s3://{bucket}/minibwa/", str(minibwa_root), dry_run=False)
    # Thread-scaling ladders and arena runs are per-SHA (unlike the
    # SHA-independent baseline and minibwa caches), so sync only this run's
    # subtree of each.
    _sync_prefix(
        f"s3://{bucket}/scaling/{fg_labs_sha}/", str(scaling_root / fg_labs_sha), dry_run=False
    )
    _sync_prefix(
        f"s3://{bucket}/arena/{fg_labs_sha}/", str(arena_root / fg_labs_sha), dry_run=False
    )

    _reconcile_mirror(bucket=bucket, fg_labs_sha=fg_labs_sha, run_dir=run_dir)

    if ingest:
        _ingest_all(
            fg_labs_sha=fg_labs_sha,
            runs_root=runs_root,
            baseline_root=baseline_root,
            minibwa_root=minibwa_root,
            scaling_root=scaling_root,
            arena_root=arena_root,
            ingest_late_cells=ingest_late_cells,
        )


def _reconcile_mirror(*, bucket: str, fg_labs_sha: str, run_dir: Path) -> None:
    """Report local files S3 no longer has. Report only; never delete.

    Diagnostic, never load-bearing — the same contract `emit-host-meta` and
    `emit-host-probe` hold. The sync has already succeeded by the time this runs,
    so the artifacts are on disk and ingestable; letting a transient ListObjects
    failure abort the command would throw that work away over a report.
    """
    try:
        orphans = _orphaned_files(run_dir, _s3_keys(bucket, f"runs/{fg_labs_sha}/"))
    except (BotoCoreError, ClientError) as err:
        print(f"warning: could not reconcile the mirror against S3: {err}", file=sys.stderr)
        return
    if orphans:
        _report_orphans(orphans, run_dir)


def _ingest_all(  # noqa: PLR0913 — one argument per synced prefix, all required
    *,
    fg_labs_sha: str,
    runs_root: Path,
    baseline_root: Path,
    minibwa_root: Path,
    scaling_root: Path,
    arena_root: Path,
    ingest_late_cells: bool,
) -> None:
    """Populate every table from the freshly-synced mirror."""
    # Resolved from the tree before anything is written to the DB, so the operator
    # sees the warning even if ingest later fails.
    late = late_cells(runs_root=runs_root, fg_labs_sha=fg_labs_sha)
    # Annotated because the key shape is the contract between `late_cells` and
    # both ingest functions, and `c.key` alone does not show it at the call site.
    # Early cells are reported but never skipped — see `LateCell.is_early`.
    exclude: frozenset[tuple[str, str, int]] = (
        frozenset() if ingest_late_cells else frozenset(c.key for c in late if not c.is_early)
    )
    if late:
        _report_late_cells(late, excluded=exclude, overridden=ingest_late_cells)

    conn = connect(DB_PATH)
    try:
        n = ingest_run(conn, runs_root=runs_root, fg_labs_sha=fg_labs_sha, exclude=exclude)
        print(f"ingested {n} trials into {DB_PATH}", file=sys.stderr)

        # Truth-based accuracy rows (holodeck eval) live alongside the timing
        # trees under runs/<sha>/.../eval/; only present for sim samples run
        # via the `accuracy` / `accuracy_smoke` targets. Takes the same exclusion:
        # `accuracy` does not reference `trials.id`, so a cell kept out of
        # `trials` alone would leave its accuracy rows behind.
        a = ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=fg_labs_sha, exclude=exclude)
        if a:
            print(f"ingested {a} accuracy rows into {DB_PATH}", file=sys.stderr)

        # Thread-scaling ladder (--target thread_scaling). Absent for runs
        # that did not request it, hence the truthiness guard.
        sc = ingest_scaling(conn, scaling_root=scaling_root, fg_labs_sha=fg_labs_sha)
        if sc:
            print(f"ingested {sc} scaling rows into {DB_PATH}", file=sys.stderr)

        # Arena (--target arena). Absent for runs that did not request it,
        # hence the truthiness guard. `sample` comes from config rather than
        # the tree layout (arena/<sha>/<arch>/ carries no sample component --
        # see `ingest_arena`'s docstring); falls back to skipping arena
        # ingestion entirely if the config can't be read, same contract as
        # `_baseline_tool_versions` below.
        arena_sample = _arena_sample()
        if arena_sample:
            ar = ingest_arena(
                conn, arena_root=arena_root, fg_labs_sha=fg_labs_sha, sample=arena_sample
            )
            if ar:
                print(f"ingested {ar} arena rows into {DB_PATH}", file=sys.stderr)

        # Ingest baselines from each upstream tag known to the workflow
        # config so that `bench report`/`bench speedup` can join fg-labs
        # walls against baseline walls without an extra CLI step.
        for tool_version in _baseline_tool_versions():
            m = ingest_baseline(conn, baseline_root=baseline_root, tool_version=tool_version)
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


def _arena_sample() -> str | None:
    """The sample `arena.smk` measures (`arena.sample` in config/defaults.yaml).

    Falls back to None if the config can't be read (e.g. in environments where
    the YAMLs are not present), same contract as `_baseline_tool_versions` —
    `collect --ingest` never fails purely on missing arena ingestion.
    """
    try:
        cfg = load_config(Path(REPO_ROOT) / "config")
    except (FileNotFoundError, KeyError, OSError):
        return None
    return cfg.arena.sample


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
