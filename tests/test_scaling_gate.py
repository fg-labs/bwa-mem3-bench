"""Gate #3 — thread-scaling efficiency must not regress vs the last release."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench.report.regression import _scaling_efficiency, _scaling_gate
from bwa_mem3_bench.storage.sqlite import connect, upsert_run, upsert_scaling

MAX_DROP_PP = 3.0
FLOAT_TOL = 1e-6


def _ladder(conn, sha: str, times: dict[int, float]) -> None:
    """Seed one ladder: {threads: seconds}. process_seconds drives efficiency."""
    upsert_run(conn, fg_labs_sha=sha, status="complete")
    for threads, t in times.items():
        upsert_scaling(
            conn,
            fg_labs_sha=sha,
            sample="wgs-5M",
            arch="c8g64",
            threads=threads,
            rep=1,
            wall_seconds=t,
            cpu_time=t * threads,
            max_rss_mb=16000.0,
            process_seconds=t,
        )


def test_efficiency_matches_the_textbook_formula(tmp_path: Path) -> None:
    """E(n) = T(1) / (n * T(n)) — perfect scaling is 100%."""
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "abc", {1: 1000.0, 2: 500.0, 4: 250.0})  # perfectly linear
    eff = _scaling_efficiency(db, "abc")
    assert set(eff["threads"]) == {2, 4}  # the 1-thread rung is the baseline
    for _, row in eff.iterrows():
        assert abs(row["efficiency_pct"] - 100.0) < FLOAT_TOL


def test_regression_beyond_tolerance_fails(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1000.0, 2: 500.0})  # E(2) = 100%
    _ladder(conn, "new", {1: 1000.0, 2: 560.0})  # E(2) = 89.3%, -10.7 pp
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert (out["verdict"] == "REGRESSION").any()


def test_small_drop_within_tolerance_passes(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1000.0, 2: 500.0})  # E(2) = 100%
    _ladder(conn, "new", {1: 1000.0, 2: 505.0})  # E(2) = 99.0%, -1.0 pp
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert not (out["verdict"] == "REGRESSION").any()


def test_improvement_never_fails(tmp_path: Path) -> None:
    """A faster run has a NEGATIVE drop; it must not trip the gate."""
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1000.0, 2: 560.0})
    _ladder(conn, "new", {1: 1000.0, 2: 500.0})
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert not (out["verdict"] == "REGRESSION").any()


def test_no_previous_ladder_is_not_a_failure(tmp_path: Path) -> None:
    """The FIRST run after this gate lands has no predecessor.

    It must no-op rather than fail closed, or introducing the gate would block
    the very release that establishes its baseline.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "new", {1: 1000.0, 2: 500.0})
    upsert_run(conn, fg_labs_sha="prev", status="complete")  # exists, no ladder
    assert _scaling_gate(db, "new", "prev", MAX_DROP_PP).empty


def test_missing_one_thread_rung_yields_no_efficiency(tmp_path: Path) -> None:
    """Without T(1) the formula is undefined; skip rather than divide by nothing."""
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "abc", {2: 500.0, 4: 250.0})
    assert _scaling_efficiency(db, "abc").empty


def test_untimed_rung_is_dropped_rather_than_scored_as_ok(tmp_path: Path) -> None:
    """A rung with no parseable timing must leave the gate, not pass it.

    Both timing columns are NULL when neither PROCESS() nor the tricorder wall
    could be read. Every comparison against the resulting NaN is False, so such a
    rung would otherwise render as `ok` — a green verdict computed from no data.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "abc", {1: 1000.0, 2: 500.0})
    upsert_scaling(
        conn,
        fg_labs_sha="abc",
        sample="wgs-5M",
        arch="c8g64",
        threads=4,
        rep=1,
        wall_seconds=None,
        cpu_time=None,
        max_rss_mb=None,
        process_seconds=None,
    )
    eff = _scaling_efficiency(db, "abc")
    assert set(eff["threads"]) == {2}, "the untimed 4-thread rung must not be scored"


def test_untimed_one_thread_rung_disables_the_gate(tmp_path: Path) -> None:
    """No usable T(1) means no baseline, so no rung may be scored against it."""
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "abc", {2: 500.0, 4: 250.0})
    upsert_scaling(
        conn,
        fg_labs_sha="abc",
        sample="wgs-5M",
        arch="c8g64",
        threads=1,
        rep=1,
        wall_seconds=None,
        cpu_time=None,
        max_rss_mb=None,
        process_seconds=None,
    )
    assert _scaling_efficiency(db, "abc").empty
