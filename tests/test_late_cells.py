"""Guards against folding foreign measurements into a run's record.

A run's S3 prefix is not proof that everything under it belongs to that run.
Re-running one sample against an old SHA — a control, a bisect, a one-off
reproduction — writes into `runs/<that-sha>/`, and `collect` then folds those
measurements into the release's medians, which is what every future perf gate
compares against.

This is not hypothetical. The v0.8.0 golden's tree holds 23 such cells from a
control run taken three days after the release was benched, on that day's hosts,
where the *same binary* measured 18.7% slower than its own recorded number.
Separating them took a manual audit of S3 timestamps, object by object.

Two of those 23 are the reason this is keyed on TIME rather than on rep number:
`wgs-5M-alt/m7i/rep-1` and `wgs-5M-alt-compat/m7i/rep-1` look like ordinary
release measurements. They cannot be — the ALT samples were added after v0.8.0
was blessed — but nothing about the rep number says so, and a `rep > reps` rule
would have ingested both.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bwa_mem3_bench.storage.ingest import (
    LATE_CELL_THRESHOLD_HOURS,
    _measured_at,
    ingest_accuracy,
    ingest_run,
    late_cells,
)
from bwa_mem3_bench.storage.sqlite import connect

SHA = "abc123"
HOUR = 3600.0

TIMING = "s\th:m:s\tmax_rss\tmax_vms\tmax_uss\tmax_pss\tio_in\tio_out\tmean_load\tcpu_time\n"

# holodeck eval outputs, in the shapes `tests/test_storage_ingest.py` pins. Kept
# minimal here — this module tests WHICH cells get ingested, not how their
# contents parse.
EVAL_TXT = (
    "mapq_bin\ttotal\tcorrect\tmismapped\tunmapped\tpct_correct\tpct_mismapped\tpct_unmapped\n"
    "ALL\t1000\t990\t8\t2\t99.00\t0.80\t0.20\n"
)
VARIANTS_TSV = (
    "class\tconfounded\tn_expected\tn_represented\trepresented_pct\tmean_mapq\tmean_as\n"
    "mirror\tfalse\t20\t18\t90.00\t58.00\t140.50\n"
    "#variant_bearing_reads\t20\n"
    "#md_concordant_pct\t97.50\n"
    "#nm_concordant_pct\tNA\n"
)

# A run's cells all land within minutes of each other; the control arrives days
# later. Offsets are in hours from an arbitrary epoch base.
RUN_HOURS = 0.0
CONTROL_HOURS = 68.0

# `_build_run`'s shape: reps 1-5 are the release, reps 6-7 are the control.
RELEASE_REPS = 5
CONTROL_REPS = 2
ALL_REPS = RELEASE_REPS + CONTROL_REPS
# A rep number the fixture never dates, used for the undatable-cell case.
UNDATABLE_REP = 9


def _write_cell(  # noqa: PLR0913 — a fixture builder; each axis is exercised
    root: Path,
    *,
    sample: str,
    arch: str,
    rep: int,
    wall: float,
    offset_hours: float,
    stamped: bool,
    base_epoch: float,
) -> None:
    """One `rep-N` cell whose measurement time is `offset_hours` from the base.

    `stamped` chooses which of the two dating paths the cell exercises:
    `meta.json`'s own `measured_at` (what workers write now) or the artifact's
    mtime (every historical cell, and the only signal available for them).
    """
    cell = root / SHA / sample / arch / f"rep-{rep}"
    bench = cell / "benchmarks"
    bench.mkdir(parents=True)
    timing = bench / "timing.tsv"
    timing.write_text(TIMING + f"{wall}\t0:00:01\t1.0\t1.0\t1.0\t1.0\t0\t0\t100\t{wall}\n")

    when = base_epoch + offset_hours * HOUR
    meta: dict[str, object] = {
        "fg_labs_sha": SHA,
        "sample": sample,
        "arch": arch,
        "rep": rep,
        "instance_type": "m7i.4xlarge",
        "availability_zone": "us-east-1b",
        "instance_id": f"i-{sample}{rep}",
        "kernel": "6.1.0",
    }
    if stamped:
        # Deliberately NOT derived from the mtime below — a stamped cell must be
        # dated by its stamp, and the two disagree here so the test can tell.
        meta["measured_at"] = _iso(when)
        os.utime(timing, (base_epoch, base_epoch))
    else:
        os.utime(timing, (when, when))
    (bench / "meta.json").write_text(json.dumps(meta))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_run(root: Path, *, stamped: bool, base_epoch: float = 1_770_000_000.0) -> None:
    """Five in-window release cells plus two out-of-window control cells."""
    for rep in range(1, 6):
        _write_cell(
            root,
            sample="wgs-5M",
            arch="m7i",
            rep=rep,
            wall=90.0,
            offset_hours=RUN_HOURS + rep * 0.05,
            stamped=stamped,
            base_epoch=base_epoch,
        )
    for rep in (6, 7):
        _write_cell(
            root,
            sample="wgs-5M",
            arch="m7i",
            rep=rep,
            wall=110.0,
            offset_hours=CONTROL_HOURS,
            stamped=stamped,
            base_epoch=base_epoch,
        )


def test_late_cells_finds_the_control_reps(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    assert [(c.sample, c.arch, c.rep) for c in late] == [("wgs-5M", "m7i", 6), ("wgs-5M", "m7i", 7)]
    assert all(c.hours_late > LATE_CELL_THRESHOLD_HOURS for c in late)
    # In the ordinary shape the foreign cells FOLLOW the run, so none of them is
    # early and `collect` skips all of them. The inverted shape is below.
    assert not any(c.is_early for c in late)


def test_the_runs_own_cells_come_out_early_when_the_control_is_the_majority(
    tmp_path: Path,
) -> None:
    """The inverted shape: more foreign cells than the run has of its own.

    The reference is a MEDIAN, so once the control group is the majority the
    median sits inside the CONTROL group and the run's own legitimate cells are
    the ones that land far from it — in the EARLY direction. Both groups are
    still reported (the smaller one is the group worth looking at either way),
    but only the forward group may be skipped: excluding these would drop the
    release's real measurements and keep the control's, which is worse than
    having no guard at all. `LateCell.is_early` is what carries that distinction
    to `collect`, so pin it here rather than only at the wiring layer.
    """
    root = tmp_path / "runs"
    base = 1_770_000_000.0
    for rep in (1, 2):
        _write_cell(
            root,
            sample="wgs-5M",
            arch="m7i",
            rep=rep,
            wall=90.0,
            offset_hours=RUN_HOURS + rep * 0.05,
            stamped=True,
            base_epoch=base,
        )
    for rep in (3, 4, 5, 6, 7):
        _write_cell(
            root,
            sample="wgs-5M",
            arch="m7i",
            rep=rep,
            wall=110.0,
            offset_hours=CONTROL_HOURS,
            stamped=True,
            base_epoch=base,
        )

    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    assert [c.rep for c in late] == [1, 2], "the smaller group is named either way"
    assert all(c.hours_late < -LATE_CELL_THRESHOLD_HOURS for c in late)
    assert all(c.is_early for c in late), "so `collect` must report but not skip them"


def test_late_cells_works_from_mtime_when_no_stamp_exists(tmp_path: Path) -> None:
    """Every historical artifact predates `measured_at`, so this is the common path.

    `aws s3 sync` sets the local mtime from S3's LastModified, which is what makes
    the fallback trustworthy rather than merely convenient.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=False)
    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    assert [c.rep for c in late] == [6, 7]


def test_the_stamp_wins_over_mtime(tmp_path: Path) -> None:
    """A re-synced file gets a new mtime; the worker's own stamp does not move.

    The fixture writes a control cell whose stamp says "late" but whose mtime says
    "on time". Dating by mtime would clear it, which is the regression this pins.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    # `stamped=True` already sets every mtime to the base epoch, so mtime alone
    # would place all seven cells in one window and report nothing.
    assert [c.rep for c in late_cells(runs_root=root, fg_labs_sha=SHA)] == [6, 7]


def test_a_run_inside_the_window_reports_nothing(tmp_path: Path) -> None:
    """A whole `bless_release` takes ~5h, so it must never trip the check."""
    root = tmp_path / "runs"
    base = 1_770_000_000.0
    for rep in range(1, 6):
        _write_cell(
            root,
            sample="wgs-5M",
            arch="m7i",
            rep=rep,
            wall=90.0,
            offset_hours=rep * 1.0,  # 5 hours end to end
            stamped=True,
            base_epoch=base,
        )
    assert late_cells(runs_root=root, fg_labs_sha=SHA) == []


def test_undatable_cells_are_not_reported(tmp_path: Path) -> None:
    """Absent evidence is not evidence of contamination.

    Reporting cells we simply cannot date would train the reader to ignore the
    list, which costs more than the cells are worth.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    orphan = root / SHA / "wgs-5M" / "m7i" / f"rep-{UNDATABLE_REP}"
    (orphan / "benchmarks").mkdir(parents=True)
    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    assert UNDATABLE_REP not in [c.rep for c in late]


def test_missing_run_tree_is_not_an_error(tmp_path: Path) -> None:
    assert late_cells(runs_root=tmp_path / "runs", fg_labs_sha="nosuchsha") == []


def test_ingest_run_skips_excluded_cells(tmp_path: Path) -> None:
    """The exclusion actually keeps them out of `trials`, not just out of a report."""
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    conn = connect(tmp_path / "db.sqlite")
    n = ingest_run(conn, runs_root=root, fg_labs_sha=SHA, exclude=frozenset(c.key for c in late))
    assert n == RELEASE_REPS
    reps = [r[0] for r in conn.execute("select rep from trials order by rep")]
    assert reps == list(range(1, RELEASE_REPS + 1))


def test_ingest_run_without_an_exclusion_takes_everything(tmp_path: Path) -> None:
    """The default must be unchanged, so every other caller behaves as before."""
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_run(conn, runs_root=root, fg_labs_sha=SHA) == ALL_REPS


def test_excluding_a_cell_also_keeps_its_accuracy_rows_out(tmp_path: Path) -> None:
    """`accuracy` is keyed on its own tuple and does NOT reference `trials.id`.

    So excluding a cell from `trials` alone would leave its accuracy rows behind,
    still read by every accuracy report. Two of the v0.8.0 control's samples are
    truth samples, so this path is live rather than theoretical.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    for rep in (5, 6):
        eval_dir = root / SHA / "wgs-5M" / "m7i" / f"rep-{rep}" / "eval"
        eval_dir.mkdir(parents=True)
        (eval_dir / "fg-labs.eval.txt").write_text(EVAL_TXT)
        (eval_dir / "fg-labs.variants.tsv").write_text(VARIANTS_TSV)
        (eval_dir / "fg-labs.meth.tsv").write_text("")

    conn = connect(tmp_path / "db.sqlite")
    late = late_cells(runs_root=root, fg_labs_sha=SHA)
    exclude = frozenset(c.key for c in late)
    ingest_run(conn, runs_root=root, fg_labs_sha=SHA, exclude=exclude)
    ingest_accuracy(conn, runs_root=root, fg_labs_sha=SHA, exclude=exclude)
    reps = [r[0] for r in conn.execute("select distinct rep from accuracy order by rep")]
    assert reps == [RELEASE_REPS], "the excluded cell's accuracy rows must not survive"


def test_measured_at_reaches_the_trials_table(tmp_path: Path) -> None:
    """The stamp is queryable, which is what makes a later audit cheap.

    Attributing the v0.9.0 perf finding meant pulling meta.json out of S3 by hand,
    one object at a time, because nothing in the DB recorded when a cell ran.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    conn = connect(tmp_path / "db.sqlite")
    ingest_run(conn, runs_root=root, fg_labs_sha=SHA)
    stamps = conn.execute("select rep, measured_at from trials order by rep").fetchall()
    assert all(s.endswith("Z") for _, s in stamps), stamps
    # The control reps are recorded as later than the release reps.
    by_rep = dict(stamps)
    assert by_rep[6] > by_rep[5]


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset() is POSIX-only")
def test_a_stamp_without_an_offset_is_read_as_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader's own timezone must not change how a cell dates.

    `datetime.fromisoformat` accepts an offset-less stamp, and `.timestamp()`
    then reads it in the LOCAL zone — so the same artifact would date differently
    on two laptops and could cross the lateness threshold on one but not the
    other. The stamp is UTC by contract (`date -u` in `emit-host-meta`).

    The local zone is forced away from UTC because that is the only condition
    under which the bug is visible: under `TZ=UTC`, which is what CI runs, the
    naive and the offset-bearing spelling agree and the test proves nothing.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    meta_path = root / SHA / "wgs-5M" / "m7i" / "rep-1" / "benchmarks" / "meta.json"
    meta = json.loads(meta_path.read_text())
    naive = str(meta["measured_at"]).removesuffix("Z")
    meta["measured_at"] = naive
    meta_path.write_text(json.dumps(meta))

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        dated = _measured_at(root / SHA / "wgs-5M" / "m7i" / "rep-1")
    finally:
        # `monkeypatch` restores the env var, but the C library keeps the parsed
        # zone until something calls tzset() again — so undo it here rather than
        # leaking a non-UTC local zone into every later test in the session.
        monkeypatch.undo()
        time.tzset()

    assert dated is not None
    stamp, epoch = dated
    assert stamp == naive, "the stamp is stored verbatim, not renormalised"
    assert epoch == datetime.fromisoformat(naive).replace(tzinfo=UTC).timestamp()


def test_unknown_stamp_becomes_null(tmp_path: Path) -> None:
    """`emit-host-meta` degrades to the `unknown` sentinel; NULL is the honest value."""
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    meta_path = root / SHA / "wgs-5M" / "m7i" / "rep-1" / "benchmarks" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["measured_at"] = "unknown"
    meta_path.write_text(json.dumps(meta))

    conn = connect(tmp_path / "db.sqlite")
    ingest_run(conn, runs_root=root, fg_labs_sha=SHA)
    (value,) = conn.execute("select measured_at from trials where rep = 1").fetchone()
    assert value is None


def test_a_stamp_the_lateness_check_cannot_read_is_not_persisted(tmp_path: Path) -> None:
    """The two readers of this field must agree on which stamps are usable.

    `_measured_at` — what dates a cell for `late_cells` — rejects anything
    `fromisoformat` cannot parse and falls back to the artifact's mtime. If the
    persisted column kept the same text, `trials.measured_at` would hold a value
    the lateness check itself refused to believe. That column is an audit field
    that gets ordered and compared as a date, so non-date text sorts among real
    stamps and is indistinguishable from one — the same reason the `unknown`
    sentinel becomes NULL rather than being stored verbatim.
    """
    root = tmp_path / "runs"
    _build_run(root, stamped=True)
    rep_dir = root / SHA / "wgs-5M" / "m7i" / "rep-1"
    meta_path = rep_dir / "benchmarks" / "meta.json"
    meta = json.loads(meta_path.read_text())
    # Shaped like a stamp rather than obvious garbage: a truncated or hand-edited
    # value is the realistic way this arrives, not a random string.
    meta["measured_at"] = "2026-13-45T99:99:99Z"
    meta_path.write_text(json.dumps(meta))

    dated = _measured_at(rep_dir)
    assert dated is not None
    # Pinned to the mtime itself, not merely "not the corrupt string": any wrong
    # fallback would also satisfy an inequality.
    assert dated[1] == (rep_dir / "benchmarks" / "timing.tsv").stat().st_mtime

    conn = connect(tmp_path / "db.sqlite")
    ingest_run(conn, runs_root=root, fg_labs_sha=SHA)
    (value,) = conn.execute("select measured_at from trials where rep = 1").fetchone()
    assert value is None
