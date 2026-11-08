"""Gate #1b: truth meth cells graded no worse than the prior release."""

import json
import math
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.report.meth_accuracy_gate import (
    ABS_TOL_PPM,
    PP_TOL,
    REL_TOL,
    meth_accuracy_gate,
    placement_metrics,
    truth_meth_samples,
)
from bwa_mem3_bench.report.regression import check_regression
from bwa_mem3_bench.storage.sqlite import connect, upsert_accuracy, upsert_run, upsert_trial
from bwa_mem3_bench.workflow_config import load_config

CONFIG = load_config(Path(REPO_ROOT) / "config")
TOTAL = 1_000_000
SAMPLE = "sim-meth-place"


def _placement(mismapped_hi: int, correct_hi: int) -> str:
    """An eval.txt table: ``mismapped_hi`` / ``correct_hi`` reads at MAPQ >= 20
    (split over two confident bins), the rest of TOTAL correct at MAPQ 0."""
    low = TOTAL - mismapped_hi - correct_hi
    bins = {
        "0": {"total": low, "correct": low, "mismapped": 0},
        "1-9": {"total": 0, "correct": 0, "mismapped": 0},
        "20-29": {"total": 1000, "correct": 1000 - mismapped_hi, "mismapped": mismapped_hi},
        "60+": {
            "total": correct_hi + mismapped_hi - 1000,
            "correct": correct_hi + mismapped_hi - 1000,
            "mismapped": 0,
        },
    }
    for b in bins.values():
        b.update({"unmapped": 0, "pct_correct": 0.0, "pct_mismapped": 0.0, "pct_unmapped": 0.0})
    totals = {k: sum(b[k] for b in bins.values()) for k in ("total", "correct", "mismapped")}
    return json.dumps({"bins": bins, "all": {**totals, "unmapped": 0}})


def _trial(conn, sha: str, sample: str, rep: int) -> None:  # noqa: ANN001
    """Record one m7i trial (a requested rep) of ``sample`` on ``sha``."""
    upsert_trial(
        conn,
        fg_labs_sha=sha,
        sample=sample,
        arch="m7i",
        rep=rep,
        wall_seconds=100.0,
        max_rss_mb=1.0,
        cpu_time=1.0,
        io_read_mb=1.0,
        io_write_mb=1.0,
        mean_load=1.0,
        reads_processed=TOTAL,
        instance_type=None,
        availability_zone=None,
        spot_price=None,
        status="ok",
    )


def _grade(  # noqa: PLR0913
    conn,  # noqa: ANN001
    sha: str,
    sample: str,
    rep: int,
    blob: str,
    tool: str = "fg-labs",
) -> None:
    """Record one holodeck grade (an `accuracy` row) of ``sample`` on ``sha``."""
    upsert_accuracy(
        conn,
        fg_labs_sha=sha,
        sample=sample,
        arch="m7i",
        rep=rep,
        tool=tool,
        placement_total=TOTAL,
        placement_correct_pct=None,
        placement_mismapped_pct=None,
        placement_unmapped_pct=None,
        placement_json=blob,
        variant_bearing_reads=None,
        md_concordant_pct=None,
        nm_concordant_pct=None,
        by_class_json=None,
        meth_n_cpg=None,
        meth_pearson_r=None,
        meth_rmse=None,
    )


def _seed(  # noqa: PLR0913
    db: Path,
    *,
    prev: tuple[int, int] | None,
    new: tuple[int, int] | None,
    reps: int = 2,
    new_graded_reps: int | None = None,
    sample: str = SAMPLE,
) -> None:
    """One truth meth cell per run. ``prev`` / ``new`` are ``(mismapped_hi,
    correct_hi)`` read counts, or None to run the cell without grading it."""
    conn = connect(db)
    for sha, counts, graded in (
        ("old", prev, reps),
        ("new", new, reps if new_graded_reps is None else new_graded_reps),
    ):
        upsert_run(conn, fg_labs_sha=sha, status="complete")
        for rep in range(1, reps + 1):
            _trial(conn, sha, sample, rep)
            if counts is not None and rep <= graded:
                _grade(conn, sha, sample, rep, _placement(*counts))
        # A comparator arm graded worse never enters the gate.
        _grade(conn, sha, sample, 1, _placement(9000, 1000), tool="baseline")
    conn.close()


def _gate(db: Path):  # noqa: ANN202
    """Gate #1b as a routine sweep sees it (no golden, so nothing is expected)."""
    return meth_accuracy_gate(db, new_sha="new", prev_sha="old", config=CONFIG)


def test_scope_is_derived_from_config() -> None:
    """The gate covers exactly the configured truth + meth samples."""
    samples = truth_meth_samples(CONFIG)
    assert {"sim-meth-place", "sim-meth-place-genomic", "sim-meth-place-fast"} <= samples
    assert {"sim-meth-vars", "sim-smoke-meth-vars"} <= samples
    assert "sim-wgs-place" not in samples  # truth, not meth
    assert "meth-twist-emseq-trim-5M" not in samples  # meth, not truth


def test_metrics_are_normalised_by_all_reads() -> None:
    """Both metrics divide by the ALL total, not the MAPQ >= 20 subset."""
    mismapped_ppm, correct_pct = placement_metrics(_placement(84, 913_740))
    assert mismapped_ppm == pytest.approx(84.0)
    assert correct_pct == pytest.approx(91.374)


def test_improved_cell_passes(tmp_path: Path) -> None:
    """Fewer confident errors and more confident correct reads pass."""
    db = tmp_path / "b.db"
    # The v0.16.0 -> v0.17.0 sim-meth-place shape: fewer confident errors, more
    # confident correct reads.
    _seed(db, prev=(203, 903_886), new=(84, 913_740))
    gate = _gate(db)
    assert not gate.failing
    row = gate.cells.iloc[0]
    assert row["verdict"] == "ok"
    assert row["delta_mismapped_ppm"] == pytest.approx(-119.0)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True, report
    assert "Gate #1b" in report
    assert "1 cells, worst Δmismapped -119.00 ppm" in report


def test_equal_cell_passes(tmp_path: Path) -> None:
    """An unchanged cell passes."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=(84, 913_740))
    assert not _gate(db).failing


def test_change_within_tolerance_passes(tmp_path: Path) -> None:
    """Changes inside REL_TOL + ABS_TOL_PPM and PP_TOL pass."""
    db = tmp_path / "b.db"
    limit = int(100 * (1 + REL_TOL) + ABS_TOL_PPM)  # 103 reads of 10^6 = 103 ppm
    # One read short of PP_TOL, to stay clear of float equality at the boundary.
    dropped = int(PP_TOL / 100 * TOTAL) - 1
    _seed(db, prev=(100, 900_000), new=(limit, 900_000 - dropped))
    assert not _gate(db).failing


def test_more_confident_errors_fail(tmp_path: Path) -> None:
    """More mismapped reads at MAPQ >= 20 than the tolerance allows fail."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=(120, 913_740))
    gate = _gate(db)
    assert gate.failing
    assert gate.cells.iloc[0]["verdict"] == "REGRESSION"
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "# Regression gate: FAIL" in report


def test_fewer_confident_correct_reads_fail(tmp_path: Path) -> None:
    """Demoting correct reads below MAPQ 20 must not pass as 'fewer errors'."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=(80, 900_000))
    gate = _gate(db)
    assert gate.failing
    assert gate.cells.iloc[0]["verdict"] == "REGRESSION"


def test_cell_graded_in_prior_but_not_candidate_fails_closed(tmp_path: Path) -> None:
    """A cell the prior graded must be graded in the candidate."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=None)
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["side"]) == ["candidate"]
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "Gate #1b failures — missing sim-truth grades" in report


def test_cell_the_prior_ran_but_did_not_grade_fails_closed(tmp_path: Path) -> None:
    """A cell the prior ran but did not grade fails rather than reading new_only."""
    db = tmp_path / "b.db"
    _seed(db, prev=None, new=(84, 913_740))
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["side"]) == ["prior"]
    assert gate.cells.empty  # not mislabeled new_only


def test_rep_missing_its_grade_fails_closed(tmp_path: Path) -> None:
    """Every candidate trial rep must carry a grade."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=(84, 913_740), reps=3, new_graded_reps=2)
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["detail"]) == ["2 of 3 reps graded (missing 3)"]


def test_cell_new_in_the_candidate_is_shown_not_gated(tmp_path: Path) -> None:
    """A graded cell the prior never ran is shown as new_only and not gated."""
    db = tmp_path / "b.db"
    conn = connect(db)
    for sha in ("old", "new"):
        upsert_run(conn, fg_labs_sha=sha, status="complete")
    _trial(conn, "new", SAMPLE, 1)
    _grade(conn, "new", SAMPLE, 1, _placement(84, 913_740))
    conn.close()
    gate = _gate(db)
    assert not gate.failing
    assert list(gate.cells["verdict"]) == ["new_only"]


def test_candidate_without_truth_meth_samples_is_not_evaluated(tmp_path: Path) -> None:
    """The routine `rule all` sweep requests no sim sample; say so, don't fail."""
    db = tmp_path / "b.db"
    _seed(db, prev=(84, 913_740), new=None)
    conn = connect(db)
    conn.execute("DELETE FROM trials WHERE fg_labs_sha = 'new'")
    conn.commit()
    conn.close()
    _trial(connect(db), "new", "wgs-5M", 1)  # the sweep ran, just no sim sample
    gate = _gate(db)
    assert not gate.evaluated
    assert not gate.failing
    _, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert "Not evaluated: the candidate requested no truth meth sample" in report


def _bless_gate(db: Path):  # noqa: ANN202
    """Gate #1b as a bless sees it (`--golden-ref-sha` given: grades expected)."""
    return meth_accuracy_gate(db, new_sha="new", prev_sha="old", config=CONFIG, expect_graded=True)


def _sweep_without_sim(db: Path) -> None:
    """Prior graded; the candidate ran only a non-truth sample."""
    _seed(db, prev=(84, 913_740), new=None)
    conn = connect(db)
    conn.execute("DELETE FROM trials WHERE fg_labs_sha = 'new'")
    conn.commit()
    _trial(conn, "new", "wgs-5M", 1)
    conn.close()


def test_bless_with_no_graded_truth_meth_cell_fails(tmp_path: Path) -> None:
    """On a bless (`--golden-ref-sha`) an empty accuracy table fails, not skips."""
    db = tmp_path / "b.db"
    _sweep_without_sim(db)
    gate = _bless_gate(db)
    assert not gate.evaluated
    assert gate.failing
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old", golden_ref_sha="old")
    assert ok is False
    assert "the candidate has no graded truth meth cell" in report
    assert "Not evaluated" not in report


def test_requested_but_ungraded_cells_fail_without_a_prior(tmp_path: Path) -> None:
    """A cell the candidate ran must be graded even with nothing in the prior to
    miss -- on a bless and on a sweep alike."""
    db = tmp_path / "b.db"
    _seed(db, prev=None, new=None)
    conn = connect(db)
    conn.execute("DELETE FROM trials WHERE fg_labs_sha = 'old'")
    conn.commit()
    conn.close()
    assert _bless_gate(db).failing
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["detail"]) == ["0 of 2 reps graded (missing 1, 2)"]


def test_bless_with_full_coverage_passes(tmp_path: Path) -> None:
    """A fully graded, improved candidate passes on a bless."""
    db = tmp_path / "b.db"
    _seed(db, prev=(203, 903_886), new=(84, 913_740))
    gate = _bless_gate(db)
    assert gate.evaluated
    assert not gate.failing


def test_sweep_with_no_graded_truth_meth_cell_is_not_evaluated(tmp_path: Path) -> None:
    """Without a golden, a candidate with no sim cell is not evaluated, not failed."""
    db = tmp_path / "b.db"
    _sweep_without_sim(db)
    gate = _gate(db)  # no golden: expect_graded=False
    assert not gate.evaluated
    assert not gate.failing


def _drop_bin(blob: str, key: str) -> str:
    """``blob`` with one MAPQ bin removed, its ALL row left as it was."""
    table = json.loads(blob)
    del table["bins"][key]
    return json.dumps(table)


_ZERO_TOTAL = json.dumps(
    {"bins": {"60+": {"total": 0, "correct": 0, "mismapped": 0}}, "all": {"total": 0}}
)


def _seed_reps(db: Path, *, sha: str, trial_reps: list[int], grades: dict[int, str]) -> None:
    """One sim-meth-place/m7i cell on ``sha``: trials on ``trial_reps``, and a
    graded table per rep in ``grades``."""
    conn = connect(db)
    upsert_run(conn, fg_labs_sha=sha, status="complete")
    for rep in trial_reps:
        _trial(conn, sha, SAMPLE, rep)
    for rep, blob in grades.items():
        _grade(conn, sha, SAMPLE, rep, blob)
    conn.close()


def test_median_is_taken_across_reps(tmp_path: Path) -> None:
    """900 / 84 / 100 ppm has median 100 -- not the mean (361) or an extreme."""
    db = tmp_path / "b.db"
    prior = _placement(100, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2, 3], grades={1: prior, 2: prior, 3: prior})
    _seed_reps(
        db,
        sha="new",
        trial_reps=[1, 2, 3],
        grades={1: _placement(900, 913_740), 2: _placement(84, 913_740), 3: prior},
    )
    row = _gate(db).cells.iloc[0]
    assert row["mismapped_ppm_new"] == pytest.approx(100.0)
    assert row["verdict"] == "ok"  # the median equals the prior; the 900 outlier does not decide


def test_prior_median_uses_prior_trial_reps(tmp_path: Path) -> None:
    """The prior's median is over its own reps, not restricted to the candidate's."""
    db = tmp_path / "b.db"
    _seed_reps(
        db,
        sha="old",
        trial_reps=[1, 2, 3],
        grades={
            1: _placement(84, 913_740),
            2: _placement(203, 913_740),
            3: _placement(203, 913_740),
        },
    )
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _placement(150, 913_740)})
    gate = _gate(db)
    row = gate.cells.iloc[0]
    assert row["n_prev"] == 3  # noqa: PLR2004
    assert row["mismapped_ppm_prev"] == pytest.approx(203.0)
    assert row["verdict"] == "ok"
    assert not gate.failing


def test_a_zero_total_rep_fails_its_cell(tmp_path: Path) -> None:
    """An unreadable rep must not drop out of the median while still counted."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2, 3], grades={1: good, 2: good, 3: good})
    _seed_reps(db, sha="new", trial_reps=[1, 2, 3], grades={1: good, 2: good, 3: _ZERO_TOTAL})
    gate = _gate(db)
    assert gate.failing
    assert gate.cells.iloc[0]["verdict"] == "candidate_unreadable"


def test_grades_are_matched_to_trial_reps(tmp_path: Path) -> None:
    """Trials on reps 1-2, grades on 1 and 3: rep 2 is missing, and rep 3's
    (much worse) grade never enters the median."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2], grades={1: good, 2: good})
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 3: _placement(9_000, 900_000)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["detail"]) == ["1 of 2 reps graded (missing 2)"]
    row = gate.cells.iloc[0]
    assert row["n_new"] == 1
    assert row["mismapped_ppm_new"] == pytest.approx(84.0)


def test_bless_fails_when_the_prior_release_was_not_graded(tmp_path: Path) -> None:
    """Without the prior's grades every candidate cell would read new_only."""
    db = tmp_path / "b.db"
    _seed_reps(db, sha="old", trial_reps=[], grades={})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _placement(84, 913_740)})
    assert list(_gate(db).cells["verdict"]) == ["new_only"]
    assert not _gate(db).failing  # a sweep against an ungraded SHA: new cells, shown
    gate = _bless_gate(db)
    assert gate.failing
    assert list(gate.missing["side"]) == ["prior"]


def test_summary_survives_a_cell_with_no_numeric_delta(tmp_path: Path) -> None:
    """Unreadable tables on both sides give NaN deltas; the report must still
    render and FAIL rather than raise."""
    db = tmp_path / "b.db"
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: _ZERO_TOTAL})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _ZERO_TOTAL})
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "1 cells (1 both_unreadable), worst Δmismapped n/a, worst Δcorrect n/a." in report


def test_a_table_whose_bins_do_not_sum_to_all_fails(tmp_path: Path) -> None:
    """A table missing a confident bin reads as fewer errors; it must fail instead."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    # 400 more confident errors than the prior, all in a bin the table then lost:
    # without the check the remaining bins read exactly like the prior's.
    worse = json.loads(good)
    worse["bins"]["0"]["total"] -= 400
    worse["bins"]["0"]["correct"] -= 400
    worse["bins"]["30-39"] = {"total": 400, "correct": 0, "mismapped": 400, "unmapped": 0}
    worse["all"]["correct"] -= 400
    worse["all"]["mismapped"] += 400
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _drop_bin(json.dumps(worse), "30-39")})
    gate = _gate(db)
    assert gate.failing
    assert gate.cells.iloc[0]["verdict"] == "candidate_unreadable"


def test_metrics_reject_an_inconsistent_table() -> None:
    """Bins that do not sum to the ALL row measure nothing."""
    mismapped, correct = placement_metrics(_drop_bin(_placement(84, 913_740), "60+"))
    assert math.isnan(mismapped)
    assert math.isnan(correct)


def test_a_prior_rep_missing_its_grade_fails_closed(tmp_path: Path) -> None:
    """The prior's median must not rest on a subset of its reps either."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 2: good})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["side"]) == ["prior"]
    assert list(gate.missing["detail"]) == ["1 of 2 reps graded (missing 2)"]


def test_an_unreadable_prior_table_fails_as_a_prior_data_error(tmp_path: Path) -> None:
    """A bad PRIOR table still fails the cell, but blames the prior, not the candidate."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2], grades={1: good, 2: _ZERO_TOTAL})
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 2: good})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["prior_unreadable"]
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "# Regression gate: FAIL" in report
    assert "1 cells (1 prior_unreadable)" in report
    assert "| prior_unreadable |" in report


def test_an_unreadable_new_cell_fails_as_a_candidate_data_error(tmp_path: Path) -> None:
    """A new_only cell has no prior to compare, but its own bad table still fails."""
    db = tmp_path / "b.db"
    _seed_reps(db, sha="old", trial_reps=[], grades={})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _ZERO_TOTAL})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]


def test_a_relabelled_bin_is_reported_unreadable(tmp_path: Path) -> None:
    """A MAPQ bin label that is not a number must not abort the report."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    bad = json.loads(good)
    bad["bins"]["high"] = bad["bins"].pop("60+")
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: json.dumps(bad)})
    assert list(_gate(db).cells["verdict"]) == ["candidate_unreadable"]


def test_the_correct_tolerance_is_inclusive(tmp_path: Path) -> None:
    """A drop of exactly PP_TOL passes; float rounding must not tip it into a fail."""
    db = tmp_path / "b.db"
    exact = int(PP_TOL / 100 * TOTAL)  # 200 of 10^6 reads = 0.02 pp
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: _placement(10, 290_200)})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _placement(10, 290_200 - exact)})
    assert list(_gate(db).cells["verdict"]) == ["ok"]


def _short(blob: str, dropped: int) -> str:
    """``blob`` with ``dropped`` correct MAPQ-0 reads missing, as if never aligned."""
    table = json.loads(blob)
    for row in (table["bins"]["0"], table["all"]):
        row["total"] -= dropped
        row["correct"] -= dropped
    return json.dumps(table)


def test_a_candidate_that_drops_reads_fails(tmp_path: Path) -> None:
    """A self-consistent table that graded fewer reads than the prior must fail:
    holodeck counts only the primaries the aligner emitted."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _short(good, 50_000)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["side"]) == ["candidate"]
    assert list(gate.missing["detail"]) == ["rep 1 graded 950000 of 1000000 reads"]


def test_a_rep_that_drops_reads_fails_within_a_new_cell(tmp_path: Path) -> None:
    """With no prior, the reps of the cell still have to agree on the read count."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[], grades={})
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 2: _short(good, 10)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.missing["detail"]) == ["rep 2 graded 999990 of 1000000 reads"]


def _overflowing(blob: str, *, field: str) -> str:
    """``blob`` with one count replaced by a JSON number too large for an int."""
    if field == "all":
        return blob.replace('"all": {"total": 1000000', '"all": {"total": 1e400', 1)
    return blob.replace('"mismapped": 84', '"mismapped": 1e400', 1)


@pytest.mark.parametrize("field", ["all", "bin"])
def test_metrics_treat_an_overflowing_count_as_unreadable(field: str) -> None:
    """JSON reads ``1e400`` as a float (infinity): not a count, and not an error that escapes."""
    blob = _overflowing(_placement(84, 913_740), field=field)
    assert "1e400" in blob
    mismapped, correct = placement_metrics(blob)
    assert math.isnan(mismapped)
    assert math.isnan(correct)


def test_an_overflowing_rep_is_reported_unreadable(tmp_path: Path) -> None:
    """The read-total check reads the ALL row too; it must not abort the report."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1, 2], grades={1: good, 2: good})
    bad = _overflowing(good, field="all")
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 2: bad})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]


def test_an_error_only_bin_relabelled_with_a_digit_prefix_is_unreadable(tmp_path: Path) -> None:
    """``0garbage`` would read as MAPQ 0 and drop the bin's confident errors from the count."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    table = json.loads(good)
    table["bins"]["20-29"] = {"total": 84, "correct": 0, "mismapped": 84, "unmapped": 0}
    table["bins"]["30-39"] = {"total": 916, "correct": 916, "mismapped": 0, "unmapped": 0}
    assert placement_metrics(json.dumps(table)) == placement_metrics(good)
    table["bins"]["0garbage"] = table["bins"].pop("20-29")
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: json.dumps(table)})
    assert list(_gate(db).cells["verdict"]) == ["candidate_unreadable"]


def _recounted(table: dict) -> str:
    """``table`` with its ALL row recomputed from its bins, so the sums stay whole."""
    for count in ("total", "correct", "mismapped"):
        table["all"][count] = sum(b[count] for b in table["bins"].values())
    return json.dumps(table)


def _invalid_counts(kind: str) -> str:
    """A table whose bins sum to ALL but hold a count no grader could produce."""
    table = json.loads(_placement(84, 913_740))
    if kind == "negative":  # cancels the confident errors, moved to an unconfident bin
        table["bins"]["60+"]["mismapped"] = -84
        table["bins"]["0"]["mismapped"] = 84
        table["bins"]["0"]["correct"] -= 84
    else:  # more reads correct than the bin holds
        table["bins"]["60+"]["correct"] += 5000
    return _recounted(table)


@pytest.mark.parametrize("kind", ["negative", "over_total"])
def test_metrics_treat_impossible_counts_as_unreadable(kind: str) -> None:
    """Bins can sum to ALL and still be impossible; such a table measures nothing."""
    mismapped, correct = placement_metrics(_invalid_counts(kind))
    assert math.isnan(mismapped)
    assert math.isnan(correct)


@pytest.mark.parametrize("kind", ["negative", "over_total"])
def test_an_impossible_count_fails_its_cell(kind: str, tmp_path: Path) -> None:
    """Without the check, both tables would pass: fewer errors, or more correct reads."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _invalid_counts(kind)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]


def _error_bin_relabelled(label: str) -> str:
    """The `_placement` table with its confident errors in a bin relabelled ``label``."""
    table = json.loads(_placement(84, 913_740))
    table["bins"]["30-39"] = {"total": 916, "correct": 916, "mismapped": 0, "unmapped": 0}
    table["bins"]["20-29"] = {"total": 84, "correct": 0, "mismapped": 84, "unmapped": 0}
    table["bins"][label] = table["bins"].pop("20-29")
    return json.dumps(table)


@pytest.mark.parametrize("label", ["19-0", "0-29", "10-20", "10+", "19+"])
def test_metrics_treat_a_reversed_or_straddling_bin_as_unreadable(label: str) -> None:
    """A bin that is not wholly below or wholly at/above MAPQ 20 cannot be split, so
    reading it as unconfident would hide its confident errors."""
    mismapped, correct = placement_metrics(_error_bin_relabelled(label))
    assert math.isnan(mismapped)
    assert math.isnan(correct)


@pytest.mark.parametrize(("label", "mismapped_ppm"), [("19", 0.0), ("10-19", 0.0), ("20+", 84.0)])
def test_metrics_split_bins_at_mapq_20(label: str, mismapped_ppm: float) -> None:
    """Bins wholly below or wholly at/above the threshold are still read."""
    assert placement_metrics(_error_bin_relabelled(label))[0] == mismapped_ppm


def test_a_straddling_bin_is_reported_unreadable(tmp_path: Path) -> None:
    """``0-29`` hides confident errors; the gate fails the cell as a data error."""
    db = tmp_path / "b.db"
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: _placement(84, 913_740)})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _error_bin_relabelled("0-29")})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]


def _non_integral(value: object) -> str:
    """The `_placement` table with the confident `60+` bin's zero mismapped count
    replaced by ``value``, which ``int()`` would turn back into 0."""
    table = json.loads(_placement(84, 913_740))
    assert table["bins"]["60+"]["mismapped"] == 0
    table["bins"]["60+"]["mismapped"] = value
    return json.dumps(table)


@pytest.mark.parametrize("value", [0.5, -0.5, False])
def test_metrics_treat_a_non_integral_count_as_unreadable(value: object) -> None:
    """A count no grader writes must not pass by truncating to a valid one."""
    mismapped, correct = placement_metrics(_non_integral(value))
    assert math.isnan(mismapped)
    assert math.isnan(correct)


def test_a_non_integral_count_fails_its_cell(tmp_path: Path) -> None:
    """Without the check the table reads exactly as the prior's and passes."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1], grades={1: _non_integral(-0.5)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]


def test_an_unreadable_table_does_not_set_the_expected_read_count(tmp_path: Path) -> None:
    """An inflated ALL total on a corrupt table must not make every whole rep read
    as short: the cell fails as unreadable, and only as unreadable."""
    db = tmp_path / "b.db"
    good = _placement(84, 913_740)
    corrupt = json.loads(good)
    corrupt["all"]["total"] = 2 * TOTAL  # its bins no longer sum to ALL
    _seed_reps(db, sha="old", trial_reps=[1], grades={1: good})
    _seed_reps(db, sha="new", trial_reps=[1, 2], grades={1: good, 2: json.dumps(corrupt)})
    gate = _gate(db)
    assert gate.failing
    assert list(gate.cells["verdict"]) == ["candidate_unreadable"]
    assert gate.missing.empty, gate.missing.to_dict("records")
