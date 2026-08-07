"""Gate #3 — thread-scaling efficiency must not regress vs the last release."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench.report.regression import _scaling_efficiency, _scaling_gate
from bwa_mem3_bench.storage.sqlite import connect, upsert_run, upsert_scaling

MAX_DROP_PP = 3.0
FLOAT_TOL = 1e-6

# The rung, in both superlinear-baseline fixtures below, whose efficiency lands
# UNDER 100% despite sharing the same inflated T(1) — real scaling loss at 8
# threads eats the inflation, the way v0.8.0's t=64 read 98.4%. It is the rung
# that makes each fixture load-bearing: one shows that a sub-100 rung is still
# rejected (so ANY superlinear rung condemns the ladder, not all of them), the
# other that it is rejected even when it is the ONLY rung shared with the new
# ladder (so the evidence comes from the whole prev ladder, not the join).
SUB_100_RUNG_THREADS = 8


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


def test_superlinear_baseline_is_rejected_rather_than_used_as_a_bar(tmp_path: Path) -> None:
    """A baseline claiming >100% efficiency cannot be the bar for the next release.

    E(n) = T(1)/(n*T(n)) above 100% says the run got MORE than n-fold faster on
    n threads. For this workload that is not a real cache effect — the FMI index
    is shared and huge, so the per-thread working set does not shrink — it means
    T(1) is inconsistent with the T(n) beside it. Since T(1) is the divisor at
    EVERY rung, one bad T(1) inflates the whole baseline ladder, which is why
    the entire ladder is rejected rather than just the offending rungs.

    Measured, not hypothetical: the v0.8.0 golden recorded 101-104% at five
    rungs, and its T(1) re-measured 8.4% faster (1341.22 -> 1228.33 s, 3 reps,
    1.3% spread). Against that inflated bar, a v0.9.0 that was FASTER at every
    rung in absolute terms reported a 9.81 pp REGRESSION.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    # prev T(1) inflated 10%. E(2)=E(4)=110%, but E(8)=98.2% — UNDER 100 despite
    # sharing the same bad divisor, because real scaling loss at 8 threads eats
    # the inflation. That is the shape v0.8.0 actually had (t=64 read 98.4%), and
    # it is why one superlinear rung must condemn the whole ladder: an
    # all-rungs-superlinear rule would gate t=8 against a bar that is just as
    # inflated as the rungs it rejected.
    _ladder(conn, "prev", {1: 1100.0, 2: 500.0, 4: 250.0, 8: 140.0})
    _ladder(conn, "new", {1: 1000.0, 2: 500.0, 4: 250.0, 8: 127.0})  # honest ~100%/98%
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert not out.empty
    assert (out["verdict"] == "unusable_baseline").all(), out.to_dict("records")
    assert not (out["verdict"] == "REGRESSION").any()
    # The sub-100 rung is rejected too, or the guard would only be skin deep.
    assert SUB_100_RUNG_THREADS in set(out["threads"])


def test_baseline_marginally_over_100_is_still_gated(tmp_path: Path) -> None:
    """Ratio-of-two-timings noise must not disable the gate.

    Efficiency divides two measured times, so a hair over 100% is ordinary
    noise rather than an inconsistent ladder. Only an excess beyond the
    tolerance rejects the baseline — otherwise a single lucky rung would switch
    the gate off silently, which is worse than the failure this guards against.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1005.0, 2: 500.0})  # E(2) = 100.5%, inside tolerance
    _ladder(conn, "new", {1: 1000.0, 2: 560.0})  # E(2) = 89.3% — a real drop
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert (out["verdict"] == "REGRESSION").any(), out.to_dict("records")


def test_a_baseline_exactly_at_the_tolerance_is_still_gated(tmp_path: Path) -> None:
    """The boundary is `>`, not `>=`, and this is the case that proves which.

    The 100.5% fixture above cannot distinguish them — it passes either way. This
    one lands on the tolerance EXACTLY: 1020/(2*500)*100 is 102.0 with no float
    slop (verified, not assumed), so `> 102.0` is False and the baseline stays
    usable while `>= 102.0` would reject it.

    Which way the boundary falls matters because the tolerance exists to absorb
    ratio-of-two-timings noise. Rejecting AT the tolerance would make the
    documented allowance one increment narrower than it reads, and a baseline
    sitting on the line would silently stop gating.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1020.0, 2: 500.0})  # E(2) = 102.0%, exactly the tolerance
    _ladder(conn, "new", {1: 1000.0, 2: 560.0})  # E(2) = 89.3% — a real drop
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert (out["verdict"] == "REGRESSION").any(), out.to_dict("records")
    assert not (out["verdict"] == "unusable_baseline").any(), out.to_dict("records")


def test_a_superlinear_rung_absent_from_the_new_ladder_still_rejects(tmp_path: Path) -> None:
    """The evidence must come from the WHOLE previous ladder, not the shared rungs.

    `_scaling_gate` inner-joins the two ladders, so a rung the previous release
    measured and the new one did not is absent from the join. Deriving the
    superlinear check from that join makes the rejection depend on which rungs
    happen to overlap — but T(1) is the divisor at EVERY rung, so a bad T(1)
    inflates every prev rung whether or not the new run has a counterpart.

    The gap is reachable: the ladder is configurable (`--ladder 1:3`), so a
    trimmed or retuned new run legitimately produces fewer rungs than the
    baseline it is compared against.

    This fixture is the v0.8.0 shape with the revealing rung removed from the
    new side. prev T(1) is inflated 10%, so E(2) reads 110% — but t=2 is
    prior-only. The one shared rung, t=8, reads UNDER 100% because real scaling
    loss at 8 threads eats the inflation, exactly as v0.8.0's t=64 read 98.4%.
    Judged on the shared rung alone the baseline looks usable, and the gate would
    grade against a T(1) already known to be 10% wrong.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1100.0, 2: 500.0, 8: 140.0})  # E(2)=110%, E(8)=98.2%
    _ladder(conn, "new", {1: 1000.0, 8: 127.0})  # only t=8 overlaps; E(8)=98.4%
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert not out.empty
    assert set(out["threads"]) == {SUB_100_RUNG_THREADS}, "only t=8 should be comparable"
    assert (out["verdict"] == "unusable_baseline").all(), out.to_dict("records")
