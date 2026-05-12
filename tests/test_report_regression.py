"""Test bench regression: pass/fail gating vs fg-labs golden."""

import json
from pathlib import Path

from bwa_mem3_bench.report.regression import check_regression
from bwa_mem3_bench.storage.sqlite import connect, upsert_comparison, upsert_run, upsert_trial


def _seed(db: Path, golden_pct: float, perf_delta_pct: float) -> None:
    conn = connect(db)
    upsert_run(conn, fg_labs_sha="new", status="complete")
    upsert_run(conn, fg_labs_sha="old", status="complete")
    upsert_trial(
        conn,
        fg_labs_sha="old",
        sample="smoke-1M",
        arch="c7g",
        rep=1,
        wall_seconds=100.0,
        max_rss_mb=1000.0,
        cpu_time=400.0,
        io_read_mb=10.0,
        io_write_mb=5.0,
        mean_load=300.0,
        reads_processed=2000,
        instance_type=None,
        availability_zone=None,
        spot_price=None,
        status="ok",
    )
    new_trial = upsert_trial(
        conn,
        fg_labs_sha="new",
        sample="smoke-1M",
        arch="c7g",
        rep=1,
        wall_seconds=100.0 * (1 + perf_delta_pct / 100.0),
        max_rss_mb=1000.0,
        cpu_time=400.0,
        io_read_mb=10.0,
        io_write_mb=5.0,
        mean_load=300.0,
        reads_processed=2000,
        instance_type=None,
        availability_zone=None,
        spot_price=None,
        status="ok",
    )
    upsert_comparison(
        conn,
        trial_id=new_trial,
        kind="vs-golden",
        concordant=int(2000 * golden_pct / 100),
        total=2000,
        concordance_pct=golden_pct,
        by_class_json=json.dumps({}),
    )
    conn.close()


def test_regression_passes_when_concordant_and_fast(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    _seed(db, golden_pct=99.9995, perf_delta_pct=-3.0)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report


def test_regression_fails_on_concordance_below_threshold(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    _seed(db, golden_pct=99.99, perf_delta_pct=-1.0)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "FAIL" in report


def test_regression_fails_on_perf_regression_over_5_percent(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    _seed(db, golden_pct=99.9999, perf_delta_pct=7.0)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "FAIL" in report


def _seed_multi(
    db: Path,
    *,
    new_walls: list[float],
    prev_walls: list[float],
    golden_pct: float = 99.9999,
) -> None:
    """Seed multi-rep wall_seconds for one (sample, arch) cell on each SHA."""
    conn = connect(db)
    upsert_run(conn, fg_labs_sha="new", status="complete")
    upsert_run(conn, fg_labs_sha="old", status="complete")
    for rep, wall in enumerate(prev_walls, start=1):
        upsert_trial(
            conn,
            fg_labs_sha="old",
            sample="wgs-5M",
            arch="c6a",
            rep=rep,
            wall_seconds=wall,
            max_rss_mb=1.0,
            cpu_time=1.0,
            io_read_mb=1.0,
            io_write_mb=1.0,
            mean_load=1.0,
            reads_processed=1,
            instance_type=None,
            availability_zone=None,
            spot_price=None,
            status="ok",
        )
    for rep, wall in enumerate(new_walls, start=1):
        trial_id = upsert_trial(
            conn,
            fg_labs_sha="new",
            sample="wgs-5M",
            arch="c6a",
            rep=rep,
            wall_seconds=wall,
            max_rss_mb=1.0,
            cpu_time=1.0,
            io_read_mb=1.0,
            io_write_mb=1.0,
            mean_load=1.0,
            reads_processed=1,
            instance_type=None,
            availability_zone=None,
            spot_price=None,
            status="ok",
        )
        upsert_comparison(
            conn,
            trial_id=trial_id,
            kind="vs-golden",
            concordant=int(golden_pct * 100),
            total=10000,
            concordance_pct=golden_pct,
            by_class_json=json.dumps({}),
        )
    conn.close()


def test_regression_fails_on_clean_multi_rep_regression(tmp_path: Path) -> None:
    """Non-overlapping ranges + median >threshold → REGRESSION → gate fails."""
    db = tmp_path / "b.db"
    _seed_multi(db, new_walls=[110, 112, 115, 118, 120], prev_walls=[100, 101, 102])
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "FAIL" in report
    assert "REGRESSION" in report


def test_regression_does_not_fail_on_noisy_overlapping_ranges(tmp_path: Path) -> None:
    """Median delta > threshold but ranges overlap → `noisy`, gate passes.

    Catches the m7i / c7i spot-noise case: the per-rep median can drift 20-40%
    SHA-to-SHA purely from where reps landed in the bimodal distribution,
    but the wall_s ranges fully overlap so there's no real evidence.
    """
    db = tmp_path / "b.db"
    _seed_multi(
        db,
        new_walls=[110, 220, 165, 180, 200],
        prev_walls=[100, 180, 140, 170, 150],
    )
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report
    assert "noisy" in report


def test_regression_reports_improvement_when_ranges_strictly_better(tmp_path: Path) -> None:
    """Symmetric to the regression case on the fast side. Gate still passes."""
    db = tmp_path / "b.db"
    _seed_multi(db, new_walls=[80, 82, 85, 88, 90], prev_walls=[100, 101, 102])
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report
    assert "improvement" in report
