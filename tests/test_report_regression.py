"""Test bench regression: pass/fail gating vs fg-labs golden + vs-upstream budget."""

import json
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.registry import DivergenceEntry
from bwa_mem3_bench.report.regression import _baseline_budget, check_regression
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


# ── Gate #1: vs-upstream drift vs registry budget ──────────────────────────


def _entry(drift: float, samples: tuple[str, ...]) -> DivergenceEntry:
    return DivergenceEntry(
        id="X",
        pr="p",
        date="2026-06-07",
        summary="s",
        affected="primary_alignment",
        expected_drift_pct=drift,
        samples=samples,
    )


def test_baseline_budget_flags_only_over_budget_cells() -> None:
    conc = pd.DataFrame(
        {
            "sample": ["wgs-5M", "meth-twist-emseq-5M"],
            "arch": ["c6a", "m7i"],
            "baseline_concordance": [99.80, 99.00],  # drift 0.20%, 1.00%
        }
    )
    registry = [_entry(0.10, ("wgs-5M",)), _entry(1.50, ("meth-twist-emseq-5M",))]
    out = _baseline_budget(conc, registry)
    verdicts = dict(zip(out["sample"], out["verdict"], strict=True))
    assert verdicts["wgs-5M"] == "over_budget"  # 0.20 > 0.10
    assert verdicts["meth-twist-emseq-5M"] == "ok"  # 1.00 <= 1.50 (meth tier)


def test_baseline_budget_empty_in_empty_out() -> None:
    out = _baseline_budget(pd.DataFrame(columns=["sample", "arch", "baseline_concordance"]), [])
    assert out.empty


def _seed_baseline(db: Path, *, sample: str, arch: str, baseline_pct: float) -> None:
    """One flat-perf, golden-clean cell with a vs-baseline comparison at baseline_pct."""
    conn = connect(db)
    upsert_run(conn, fg_labs_sha="new", status="complete")
    upsert_run(conn, fg_labs_sha="old", status="complete")
    common = dict(
        sample=sample,
        arch=arch,
        rep=1,
        wall_seconds=100.0,
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
    upsert_trial(conn, fg_labs_sha="old", **common)
    new_trial = upsert_trial(conn, fg_labs_sha="new", **common)
    # Gate #2 clean so only the budget gate decides the outcome.
    upsert_comparison(
        conn,
        trial_id=new_trial,
        kind="vs-golden",
        concordant=10000,
        total=10000,
        concordance_pct=100.0,
        by_class_json=json.dumps({}),
    )
    upsert_comparison(
        conn,
        trial_id=new_trial,
        kind="vs-baseline",
        concordant=int(10000 * baseline_pct / 100),
        total=10000,
        concordance_pct=baseline_pct,
        by_class_json=json.dumps({}),
    )
    conn.close()


def test_gate1_passes_within_budget(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # wgs-5M non-meth budget is 0.10%; 0.05% drift is within it.
    _seed_baseline(db, sample="wgs-5M", arch="c6a", baseline_pct=99.95)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report


def test_gate1_fails_over_budget(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # 0.20% drift exceeds wgs-5M's 0.10% budget → unexplained upstream divergence.
    _seed_baseline(db, sample="wgs-5M", arch="c6a", baseline_pct=99.80)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "FAIL" in report
    assert "Gate #1 failures" in report


def test_gate1_meth_tier_tolerates_higher_drift(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # 1.00% drift would bust the non-meth budget but is within meth's 1.50%.
    _seed_baseline(db, sample="meth-twist-emseq-5M", arch="m7i", baseline_pct=99.00)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report


def _seed_missing(db: Path, *, new_arch: str, baseline_arch: str, add_vs_baseline: bool) -> None:
    conn = connect(db)
    for sha in ("new", "old"):
        upsert_run(conn, fg_labs_sha=sha, status="complete")
    upsert_run(conn, fg_labs_sha="baseline-bwa-mem2-v2.2.1", status="baseline")
    common = dict(
        sample="wgs-5M",
        rep=1,
        wall_seconds=100.0,
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
    upsert_trial(conn, fg_labs_sha="baseline-bwa-mem2-v2.2.1", arch=baseline_arch, **common)
    upsert_trial(conn, fg_labs_sha="old", arch=new_arch, **common)
    tid = upsert_trial(conn, fg_labs_sha="new", arch=new_arch, **common)
    upsert_comparison(
        conn,
        trial_id=tid,
        kind="vs-golden",
        concordant=1,
        total=1,
        concordance_pct=100.0,
        by_class_json="{}",
    )
    if add_vs_baseline:
        upsert_comparison(
            conn,
            trial_id=tid,
            kind="vs-baseline",
            concordant=1,
            total=1,
            concordance_pct=100.0,
            by_class_json="{}",
        )
    conn.close()


def test_gate1_fails_closed_on_missing_baseline_comparison(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # x86 cell with a baseline counterpart but no vs-baseline comparison → must fail.
    _seed_missing(db, new_arch="c6a", baseline_arch="c6a", add_vs_baseline=False)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is False
    assert "missing vs-baseline" in report


def test_gate1_does_not_false_fail_arm_without_baseline(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # ARM cell (c8g) has no baseline counterpart (baseline is x86 c6a) → not expected,
    # so a missing vs-baseline for ARM must NOT fail the gate.
    _seed_missing(db, new_arch="c8g", baseline_arch="c6a", add_vs_baseline=False)
    ok, report = check_regression(db_path=db, new_sha="new", prev_sha="old")
    assert ok is True
    assert "PASS" in report
