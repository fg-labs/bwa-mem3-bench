"""Guards for reconciling the local mirror against S3.

`collect` runs `aws s3 sync` WITHOUT `--delete` — deliberately, since the
`baseline/` and `minibwa/` caches are shared across runs and must survive a
single-run sync — and ingest then walks the LOCAL tree. So an object deleted from
S3 stays in the mirror forever and keeps being ingested, and `benchmark.db` can
hold rows the authoritative bucket has no evidence for.

Measured on the real mirror: 20 of 5,849 cells are in that state, 6 of them under
the v0.8.0 golden, all dated the day before that release was blessed — consistent
with a pre-bless smoke run whose S3 outputs were deleted so the bless would
reproduce them cleanly. `aws cleanup-s3` is not the cause; it reaps only
`aligned.bam` and preserves exactly the small artifacts `collect` ingests.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.commands import _collect as collect_mod
from bwa_mem3_bench.commands._collect import (
    _EXCLUDED_PATTERNS,
    _LOCALLY_WRITTEN,
    _MAX_ROWS_LISTED,
    _orphaned_files,
    _report_late_cells,
)
from bwa_mem3_bench.storage.ingest import LateCell
from bwa_mem3_bench.storage.sqlite import connect

SHA = "abc123"
_TIMING_HEADER = (
    "s\th:m:s\tmax_rss\tmax_vms\tmax_uss\tmax_pss\tio_in\tio_out\tmean_load\tcpu_time\n"
)
_LATE_OFFSET_HOURS = 72.0
# Two cells inside the run's window, one three days later — the v0.8.0 shape.
_TWO_WINDOW_OFFSETS = (0.0, 0.1, _LATE_OFFSET_HOURS)
# The inverted shape: one cell of the run's own, three of a later control run.
# The median then sits in the CONTROL group, so it is the release's own rep-1
# that reads as far from the reference — see `test_collect_ingests_early_cells`.
_INVERTED_OFFSETS = (0.0, _LATE_OFFSET_HOURS, _LATE_OFFSET_HOURS, _LATE_OFFSET_HOURS)

BENCH_PY = Path(REPO_ROOT) / "bwa_mem3_bench" / "commands" / "bench.py"


def _build_run(
    runs_root: Path,
    sha: str,
    offsets: tuple[float, ...],
    base_epoch: float = 1_770_000_000.0,
) -> None:
    """One `rep-N` cell per entry, measured `offsets[N - 1]` hours from the base."""
    for rep, offset in enumerate(offsets, start=1):
        bench = runs_root / sha / "wgs-5M" / "m7i" / f"rep-{rep}" / "benchmarks"
        bench.mkdir(parents=True)
        (bench / "timing.tsv").write_text(
            _TIMING_HEADER + "9.0\t0:00:09\t1\t1\t1\t1\t0\t0\t100\t9.0\n"
        )
        when = datetime.fromtimestamp(base_epoch + offset * 3600.0, tz=UTC)
        (bench / "meta.json").write_text(
            json.dumps(
                {
                    "fg_labs_sha": sha,
                    "sample": "wgs-5M",
                    "arch": "m7i",
                    "rep": rep,
                    "instance_type": "m7i.4xlarge",
                    "availability_zone": "us-east-1b",
                    "instance_id": f"i-{rep}",
                    "kernel": "6.1.0",
                    "measured_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        )


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


def test_a_file_missing_from_s3_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "run"
    _touch(root, "wgs-5M/m7i/rep-1/benchmarks/timing.tsv")
    _touch(root, "smoke-1M/c6a/rep-1/benchmarks/timing.tsv")
    orphans = _orphaned_files(root, {"wgs-5M/m7i/rep-1/benchmarks/timing.tsv"})
    assert [p.as_posix() for p in orphans] == ["smoke-1M/c6a/rep-1/benchmarks/timing.tsv"]


def test_a_fully_mirrored_tree_reports_nothing(tmp_path: Path) -> None:
    root = tmp_path / "run"
    keys = {
        "wgs-5M/m7i/rep-1/benchmarks/timing.tsv",
        "wgs-5M/m7i/rep-1/benchmarks/meta.json",
    }
    for key in keys:
        _touch(root, key)
    assert _orphaned_files(root, keys) == []


def test_bench_reports_are_not_orphans(tmp_path: Path) -> None:
    """`bench` writes these into the mirror, so S3 never has them.

    Without this exclusion every `collect` of a SHA that has ever been reported
    on would flag a stale `regression.md` — noise that teaches the reader to skip
    the list, which is worse than not having one. Confirmed against the real
    mirror: `regression.md` was reported for all six run trees that hold one,
    including two with no genuine orphans at all.
    """
    root = tmp_path / "run"
    _touch(root, "regression.md")
    _touch(root, "summary.md")
    _touch(root, "compare.md")
    # Directory-valued outputs must exclude their whole subtree, not just a
    # top-level file of that name.
    _touch(root, "report/index.html")
    _touch(root, "full-report/wgs-5M.md")
    _touch(root, "wgs-5M/m7i/rep-1/benchmarks/timing.tsv")
    orphans = _orphaned_files(root, set())
    assert [p.as_posix() for p in orphans] == ["wgs-5M/m7i/rep-1/benchmarks/timing.tsv"]


def test_bams_are_never_orphans(tmp_path: Path) -> None:
    """BAMs are excluded from the mirror, so a reaped one must not be reported.

    `aws cleanup-s3` deletes `aligned.bam` from older run trees by design. If a
    BAM ever did land locally, treating it as an orphan would report the normal
    operation of another command as a fault.
    """
    root = tmp_path / "run"
    _touch(root, "wgs-5M/m7i/rep-1/aligned.bam")
    _touch(root, "wgs-5M/m7i/rep-1/aligned.bam.bai")
    assert _orphaned_files(root, set()) == []


def test_directories_are_not_reported(tmp_path: Path) -> None:
    """S3 has no directories, so every local dir would otherwise be an orphan."""
    root = tmp_path / "run"
    (root / "wgs-5M" / "m7i" / "rep-1" / "benchmarks").mkdir(parents=True)
    assert _orphaned_files(root, set()) == []


def test_locally_written_names_match_bench_defaults() -> None:
    """`_LOCALLY_WRITTEN` is a hand-kept mirror of `bench.py`'s default outputs.

    A new `bench` subcommand that writes into `runs/<sha>/` and is not listed
    would have its output reported as an orphan on every subsequent collect. The
    reverse — a stale entry here — silently stops reporting a real orphan of that
    name. Derived from the source rather than restated, so the two cannot drift.
    """
    source = BENCH_PY.read_text()
    # Matches e.g. `LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "summary.md"` and
    # the `/ "runs" / "docs"` form, capturing the final path component.
    defaults = set(re.findall(r'LOCAL_MIRROR_ROOT\s*/\s*"runs"[^\n]*?/\s*"([^"]+)"', source))
    assert defaults, "no default output paths found in bench.py — did the pattern rot?"
    missing = defaults - _LOCALLY_WRITTEN
    assert not missing, (
        f"bench.py writes {sorted(missing)} into the mirror but collect does not "
        "exclude them; every collect would report them as orphans."
    )
    stale = _LOCALLY_WRITTEN - defaults
    assert not stale, (
        f"collect excludes {sorted(stale)} but bench.py no longer writes them; a "
        "real orphan of that name would go unreported."
    )


def test_both_reports_cap_their_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Neither report may bury the ingest summary that follows it.

    The two are siblings printing to the same stream in the same run, so they
    share the cap. Pinned together rather than one each: the orphan report had it
    from the start and the late-cell report did not, which is exactly the drift
    a per-function test would keep letting through. The real motivating case is
    already over the cap — the v0.8.0 control is 23 cells.
    """
    over = _MAX_ROWS_LISTED + 5
    reports = 2  # the orphan report and the late-cell report, both driven below

    root = tmp_path / "run"
    for i in range(over):
        _touch(root, f"wgs-5M/m7i/rep-{i}/benchmarks/timing.tsv")
    collect_mod._report_orphans(_orphaned_files(root, set()), root)

    cells = [LateCell("wgs-5M", "m7i", i, "2026-01-01T00:00:00Z", 72.0) for i in range(over)]
    _report_late_cells(cells, excluded=frozenset(c.key for c in cells), overridden=False)

    err = capsys.readouterr().err
    assert err.count("rep-") == reports * _MAX_ROWS_LISTED, "each report lists at most the cap"
    dropped = err.count(f"... and {over - _MAX_ROWS_LISTED} more")
    assert dropped == reports, "and each says what it dropped"
    # The count in each header stays exact — truncating the list must not
    # understate how many cells the operator actually has to deal with.
    assert f"{over} file(s)" in err
    assert f"SKIPPING {over}" in err


def test_excluded_patterns_and_locally_written_are_disjoint_concerns() -> None:
    """The two skip-lists must not silently overlap.

    `_EXCLUDED_PATTERNS` is about what the mirror never downloads; `_LOCALLY_WRITTEN`
    is about what it creates itself. An entry in both would mean one of the two
    reasons is dead and the next reader cannot tell which.
    """
    for name in _LOCALLY_WRITTEN:
        assert not any(name.endswith(pattern.lstrip("*")) for pattern in _EXCLUDED_PATTERNS), (
            f"{name} is covered by both skip-lists"
        )


# --------------------------------------------------------------------------- #
# Wiring. The unit pieces above are individually correct; these assert that
# `collect` actually CALLS them, which is the failure mode this whole area keeps
# producing — `vs_x86` was computed and written for the project's entire history
# and simply never reached the DB.
# --------------------------------------------------------------------------- #


def test_collect_skips_late_cells_and_survives_a_listing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end, with S3 and the sync stubbed: late cells stay out of `trials`.

    Also pins the degrade path: a ListObjects failure must warn, not abort, since
    the sync has already succeeded and the artifacts are on disk and ingestable.
    The warning is asserted, not just the ingest — degrading SILENTLY would leave
    the operator believing the mirror was reconciled when it never was, and that
    passes an ingest-only assertion.
    """
    mirror = tmp_path / "mirror"
    runs_root = mirror / "runs"
    _build_run(runs_root, SHA, _TWO_WINDOW_OFFSETS)

    monkeypatch.setattr(collect_mod, "LOCAL_MIRROR_ROOT", mirror)
    monkeypatch.setattr(collect_mod, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(collect_mod, "_sync_prefix", lambda *a, **k: None)
    monkeypatch.setattr(collect_mod, "_baseline_tool_versions", list)

    def _boom(bucket: str, prefix: str) -> set[str]:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")

    monkeypatch.setattr(collect_mod, "_s3_keys", _boom)

    collect_mod.collect(fg_labs_sha=SHA, bucket="b")

    conn = connect(tmp_path / "db.sqlite")
    reps = [r[0] for r in conn.execute("select rep from trials order by rep")]
    assert reps == [1, 2], "the out-of-window cell must not be ingested"

    err = capsys.readouterr().err
    assert "could not reconcile the mirror against S3" in err
    assert "AccessDenied" in err, "the operator needs the cause, not just the fact"


def test_collect_reports_a_local_artifact_s3_no_longer_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reconciliation's SUCCESS path, which the degrade test above cannot reach.

    `_orphaned_files` is unit-tested above, but nothing asserted that `collect`
    calls it once the listing succeeds — drop the call and every other test here
    stays green, since the failure test stubs the listing to raise and the ingest
    tests never look at the orphan report.

    The mirrored keys are derived from the tree rather than restated so the test
    cannot drift from `_build_run`, and the count is asserted as well as the name:
    reporting the run's own files too would be as useless as reporting nothing.
    """
    mirror = tmp_path / "mirror"
    run_dir = mirror / "runs" / SHA
    _build_run(mirror / "runs", SHA, _TWO_WINDOW_OFFSETS[:2])
    # A leftover from a cell whose S3 outputs were deliberately deleted — the
    # v0.8.0 shape this whole reconciliation exists to surface.
    orphan = "smoke-1M/c6a/rep-1/benchmarks/timing.tsv"
    _touch(run_dir, orphan)
    mirrored = {
        path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()
    } - {orphan}

    monkeypatch.setattr(collect_mod, "LOCAL_MIRROR_ROOT", mirror)
    monkeypatch.setattr(collect_mod, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(collect_mod, "_sync_prefix", lambda *a, **k: None)
    monkeypatch.setattr(collect_mod, "_s3_keys", lambda bucket, prefix: mirrored)

    collect_mod.collect(fg_labs_sha=SHA, bucket="b", ingest=False)

    err = capsys.readouterr().err
    assert "1 file(s)" in err, "the run's own mirrored files must not be reported"
    assert orphan in err
    assert "could not reconcile" not in err, "the listing succeeded; nothing to degrade"


def test_collect_reports_a_local_arena_artifact_s3_no_longer_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`arena/<sha>/` gets the same reconciliation as `runs/<sha>/` -- the gap
    a real CodeRabbit review caught: `ingest_arena` walks the local mirror the
    same way `ingest_run` does, so an `arena.tsv` S3 no longer has would be
    silently re-ingested forever without this.

    Distinguishes the two prefixes in the `_s3_keys` stub (unlike the plain
    `runs/` test above, which can get away with one fixed return value) so
    this test cannot pass by accident from the `runs/` side's mirrored set
    also happening to satisfy the `arena/` side.
    """
    mirror = tmp_path / "mirror"
    run_dir = mirror / "runs" / SHA
    _build_run(mirror / "runs", SHA, _TWO_WINDOW_OFFSETS[:2])
    run_mirrored = {
        path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()
    }

    arena_dir = mirror / "arena" / SHA
    _touch(arena_dir, "c8g/arena.tsv")
    _touch(arena_dir, "c7i/arena.tsv")
    # A leftover from an arch S3 no longer has -- e.g. the arena was re-run
    # with a narrower `arena.archs` and the old arch's outputs were deleted.
    orphan = "c6a/arena.tsv"
    _touch(arena_dir, orphan)
    arena_mirrored = {"c8g/arena.tsv", "c7i/arena.tsv"}

    def _s3_keys(bucket: str, prefix: str) -> set[str]:
        if prefix == f"runs/{SHA}/":
            return run_mirrored
        if prefix == f"arena/{SHA}/":
            return arena_mirrored
        raise AssertionError(f"unexpected prefix: {prefix!r}")

    monkeypatch.setattr(collect_mod, "LOCAL_MIRROR_ROOT", mirror)
    monkeypatch.setattr(collect_mod, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(collect_mod, "_sync_prefix", lambda *a, **k: None)
    monkeypatch.setattr(collect_mod, "_s3_keys", _s3_keys)

    collect_mod.collect(fg_labs_sha=SHA, bucket="b", ingest=False)

    err = capsys.readouterr().err
    assert f"1 file(s) under {arena_dir}" in err, (
        "the run's own two mirrored arena.tsv files must not be reported"
    )
    assert orphan in err


def test_collect_ingests_early_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cell that PRECEDES the run's median is reported but never skipped.

    The reference is a median, so when the foreign cells are the majority the
    median sits inside the FOREIGN group and the run's own cells are what read as
    far from it. Skipping those would drop the release's real measurements and
    ingest the control's instead — an inverted guard, worse than none. So the
    report stays symmetric and the exclusion does not.
    """
    mirror = tmp_path / "mirror"
    _build_run(mirror / "runs", SHA, _INVERTED_OFFSETS)

    monkeypatch.setattr(collect_mod, "LOCAL_MIRROR_ROOT", mirror)
    monkeypatch.setattr(collect_mod, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(collect_mod, "_sync_prefix", lambda *a, **k: None)
    monkeypatch.setattr(collect_mod, "_baseline_tool_versions", list)
    monkeypatch.setattr(collect_mod, "_s3_keys", lambda bucket, prefix: set())

    collect_mod.collect(fg_labs_sha=SHA, bucket="b")

    conn = connect(tmp_path / "db.sqlite")
    reps = [r[0] for r in conn.execute("select rep from trials order by rep")]
    assert reps == [1, 2, 3, 4], "the early cell is the run's own; it must survive"
    # The label has to match what the DB actually got, not merely appear: a
    # report that says "skip" beside a row that was ingested is worse than none.
    assert "ingest  wgs-5M/m7i/rep-1" in capsys.readouterr().err


def test_collect_ingests_late_cells_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The override must actually override — otherwise a legitimate resumed run
    could never be collected at all.

    Asserts the warning too: it is the only thing standing between this flag and
    a quietly poisoned baseline, and an ingest-only assertion passes with it
    deleted.
    """
    mirror = tmp_path / "mirror"
    _build_run(mirror / "runs", SHA, _TWO_WINDOW_OFFSETS)

    monkeypatch.setattr(collect_mod, "LOCAL_MIRROR_ROOT", mirror)
    monkeypatch.setattr(collect_mod, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(collect_mod, "_sync_prefix", lambda *a, **k: None)
    monkeypatch.setattr(collect_mod, "_baseline_tool_versions", list)
    monkeypatch.setattr(collect_mod, "_s3_keys", lambda bucket, prefix: set())

    collect_mod.collect(fg_labs_sha=SHA, bucket="b", ingest_late_cells=True)

    conn = connect(tmp_path / "db.sqlite")
    reps = [r[0] for r in conn.execute("select rep from trials order by rep")]
    assert reps == [1, 2, 3]

    err = capsys.readouterr().err
    assert "SKIPPING 0, INGESTING 1" in err
    assert "--ingest-late-cells was passed" in err, "the reason must be named"
