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


def _ladder(conn, sha: str, times: dict[int, float], wall: dict[int, float] | None = None) -> None:
    """Seed one ladder: {threads: seconds}. process_seconds drives efficiency.

    `wall` optionally overrides `wall_seconds` per thread (defaults to the same
    value as process_seconds), so a fixture can make PROCESS() and wall diverge
    and pin which one the gate reads.
    """
    upsert_run(conn, fg_labs_sha=sha, status="complete")
    for threads, t in times.items():
        upsert_scaling(
            conn,
            fg_labs_sha=sha,
            sample="wgs-5M",
            arch="c8g64",
            threads=threads,
            rep=1,
            wall_seconds=(wall or {}).get(threads, t),
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


def test_superlinear_baseline_falls_back_to_absolute_and_passes_a_faster_release(
    tmp_path: Path,
) -> None:
    """A baseline claiming >100% efficiency cannot be the EFFICIENCY bar — so the
    gate compares absolute per-rung times instead, and a faster release passes.

    E(n) = T(1)/(n*T(n)) above 100% says the run got MORE than n-fold faster on
    n threads. For this workload that is not a real cache effect — the FMI index
    is shared and huge, so the per-thread working set does not shrink — it means
    T(1) is inconsistent with the T(n) beside it. Since T(1) is the divisor at
    EVERY rung, one bad T(1) inflates the whole baseline ladder, so the whole
    ladder's efficiency is refused as a bar (`basis=absolute`) rather than just
    the offending rungs.

    Refusing the efficiency bar is not declining to gate: each rung still has two
    measured absolute times that do not depend on T(1), so the gate compares
    those. Here the new release is faster-or-equal at every shared rung, so it
    passes — as it should.

    Measured, not hypothetical: the v0.8.0 golden recorded 101-104% at five
    rungs, and its T(1) re-measured 8.4% faster (1341.22 -> 1228.33 s, 3 reps,
    1.3% spread). Against that inflated EFFICIENCY bar, a v0.9.0 that was FASTER
    at every rung in absolute terms reported a 9.81 pp REGRESSION — the exact
    false positive the absolute fallback converts back into a pass.
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
    _ladder(conn, "new", {1: 1000.0, 2: 500.0, 4: 250.0, 8: 127.0})  # faster/equal each rung
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert not out.empty
    # The efficiency bar is refused for the whole ladder, so every rung is graded
    # on absolute time; the sub-100 rung too, or the guard would be skin deep.
    assert (out["basis"] == "absolute").all(), out.to_dict("records")
    assert SUB_100_RUNG_THREADS in set(out["threads"])
    # Faster-or-equal at every rung -> no regression.
    assert not (out["verdict"] == "REGRESSION").any(), out.to_dict("records")


def test_absolute_fallback_grades_process_time_not_wall(tmp_path: Path) -> None:
    """The absolute fallback grades PROCESS() time, NOT end-to-end wall.

    Gate #3's efficiency basis is deliberately process-only (see
    `_scaling_efficiency`'s docstring: wall/startup overhead is Amdahl cost
    excluded from the SCALING metric on purpose). The absolute fallback stands in
    for that efficiency bar on a super-linear baseline, so it must measure the
    SAME quantity — otherwise a ladder's verdict would depend on which basis it
    happened to fall into. A rung whose PROCESS() time is flat but whose WALL time
    regressed past the threshold must therefore still pass: a wall-only regression
    is out of scope for the scaling gate, exactly as it is for the efficiency
    metric. (`process_seconds` and `wall_seconds` are seeded to diverge here.)
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1100.0, 2: 500.0, 4: 250.0, 8: 140.0})  # E(2)=110% -> unusable
    # PROCESS() flat vs prev at every rung; only t=8 WALL is inflated 20% (140->168).
    _ladder(
        conn,
        "new",
        {1: 1000.0, 2: 500.0, 4: 250.0, 8: 140.0},
        wall={1: 1000.0, 2: 500.0, 4: 250.0, 8: 168.0},
    )
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert (out["basis"] == "absolute").all(), out.to_dict("records")
    # Graded on PROCESS() (flat) -> ok, despite the 20% wall slowdown at t=8.
    assert not (out["verdict"] == "REGRESSION").any(), out.to_dict("records")
    row8 = out[out["threads"] == SUB_100_RUNG_THREADS].iloc[0]
    assert abs(row8["abs_delta_pct"]) < FLOAT_TOL, out.to_dict("records")


def test_superlinear_baseline_absolute_fallback_still_catches_a_real_slowdown(
    tmp_path: Path,
) -> None:
    """The absolute fallback must GATE, not merely narrate.

    An inflated efficiency baseline used to punt the ladder with no verdict,
    which let a genuine per-rung slowdown ride through. The fallback compares
    absolute T(n): the same super-linear baseline, but a new release slower at
    t=8 by more than the performance-regression threshold, must fail.
    """
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    _ladder(conn, "prev", {1: 1100.0, 2: 500.0, 4: 250.0, 8: 140.0})  # E(2)=110% -> unusable
    # t=8: 154 vs 140 is +10% wall, past the 5% threshold; other rungs unchanged.
    _ladder(conn, "new", {1: 1000.0, 2: 500.0, 4: 250.0, 8: 154.0})
    out = _scaling_gate(db, "new", "prev", MAX_DROP_PP)
    assert (out["basis"] == "absolute").all(), out.to_dict("records")
    regressed = out[out["verdict"] == "REGRESSION"]
    assert set(regressed["threads"]) == {SUB_100_RUNG_THREADS}, out.to_dict("records")


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
    # At the tolerance the baseline stays usable, so it is graded on efficiency
    # (the drop_pp bar), not shunted to the absolute fallback.
    assert (out["basis"] == "efficiency").all(), out.to_dict("records")


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
    # The whole prev ladder is superlinear (evidence from the prior-only t=2), so
    # the efficiency bar is refused and the shared rung is graded on absolute time
    # (127 vs 140 -> faster -> ok), never against the T(1) known to be 10% wrong.
    assert (out["basis"] == "absolute").all(), out.to_dict("records")
    assert not (out["verdict"] == "REGRESSION").any(), out.to_dict("records")
