"""Tests for `bench arena` (`bwa_mem3_bench.report.arena`).

The scenario below is deliberately shaped to fail loudly under the ORIGINAL
`vs_prior_release` bug (a real CodeRabbit finding on the PR that introduced
this report): every historical release's median wall was divided by the SAME
fixed "last historical label" (whichever sorted last alphabetically), not by
its own true immediate predecessor, and `fg-labs-default` never received a
`vs_prior_release` value at all. `test_vs_prior_release_walks_each_releases_own_predecessor`
plants four historical releases with visibly DIFFERENT wall times specifically
so any "always divide by the same one" implementation produces the wrong
ratio for at least one row, not just a subtly-off one.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.report.arena import build_arena_table, render_arena_markdown
from bwa_mem3_bench.storage.sqlite import connect, upsert_arena, upsert_run

FG_LABS_SHA = "deadbeef"
ARCH = "c7i"
TOL = 1e-9


def _row(conn: sqlite3.Connection, *, label: str, mode: str, rep: int, wall: float | None) -> None:
    upsert_arena(
        conn,
        fg_labs_sha=FG_LABS_SHA,
        arch=ARCH,
        label=label,
        mode=mode,
        rep=rep,
        wall_seconds=wall,
        cpu_time=None if wall is None else wall * 10,
        max_rss_mb=None if wall is None else 1024.0,
        process_seconds=None if wall is None else wall * 0.9,
    )


def _seed_db(db_path: Path) -> None:
    """One arch, the full arm shape: 3 comparators, 4 historical releases (one
    SKIPPED), fg-labs-default, fg-labs-fast -- inserted in the SAME order
    `align_arena`'s shell loop and `ingest_arena` produce it in (arm_spec
    order: comparators, then historical releases oldest-first, then today's
    candidate), since `build_arena_table` derives release order from DB
    insertion order (`id`), not from the label text.

    Wall times are chosen so every ratio is distinct and easy to eyeball:
      v021: 100.0   (oldest tracked -- no predecessor)
      v022:  90.0   predecessor v021 -> 100/90
      v030: SKIPPED (no wall time at all)
      v040:  80.0   predecessor v022 (v030 skipped) -> 90/80
      fg-labs-default: 70.0   predecessor v040 (latest measured) -> 80/70
      fg-labs-fast:    65.0   never gets a vs_prior_release
    """
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")

    _row(conn, label="bwa", mode="default", rep=1, wall=50.0)
    _row(conn, label="minibwa", mode="default", rep=1, wall=60.0)
    _row(conn, label="bwa-mem2-upstream", mode="default", rep=1, wall=55.0)
    _row(conn, label="v021", mode="default", rep=1, wall=100.0)
    _row(conn, label="v022", mode="default", rep=1, wall=90.0)
    _row(conn, label="v030", mode="default", rep=1, wall=None)
    _row(conn, label="v040", mode="default", rep=1, wall=80.0)
    _row(conn, label="fg-labs-default", mode="default", rep=1, wall=70.0)
    _row(conn, label="fg-labs-fast", mode="fast", rep=1, wall=65.0)


def _vs_prior(df: pd.DataFrame, label: str) -> float | None:
    row = df[(df["label"] == label) & (df["mode"] == "default")].iloc[0]
    value = row["vs_prior_release"]
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


def _assert_vs_prior_close(df: pd.DataFrame, label: str, expected: float, msg: str = "") -> None:
    actual = _vs_prior(df, label)
    assert actual is not None, f"{label}: expected a vs_prior_release value, got None. {msg}"
    assert math.isclose(actual, expected, rel_tol=TOL), msg


def test_vs_prior_release_walks_each_releases_own_predecessor(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    assert _vs_prior(df, "v021") is None, "the oldest tracked release has no predecessor"
    _assert_vs_prior_close(df, "v022", 100.0 / 90.0)
    assert _vs_prior(df, "v030") is None, "a SKIPPED release (no wall time) has nothing to compare"
    _assert_vs_prior_close(
        df,
        "v040",
        90.0 / 80.0,
        "v040's predecessor must be v022 (the last release that actually measured), "
        "not v030 (SKIPPED, no wall time) and not a fixed 'last historical label'",
    )


def test_vs_prior_release_includes_fg_labs_default(tmp_path: Path) -> None:
    """The bug this guards: `fg-labs-default` — the run's own candidate, and the
    entire reason the arena exists — got NO `vs_prior_release` at all."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    _assert_vs_prior_close(
        df,
        "fg-labs-default",
        80.0 / 70.0,
        "fg-labs-default's predecessor must be v040, the most recently blessed historical release",
    )


def test_vs_prior_release_excludes_fast_and_comparators(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    fast_row = df[(df["label"] == "fg-labs-fast") & (df["mode"] == "fast")].iloc[0]
    assert fast_row["vs_prior_release"] is None or math.isnan(fast_row["vs_prior_release"])
    for label in ("bwa", "minibwa", "bwa-mem2-upstream"):
        row = df[(df["label"] == label) & (df["mode"] == "default")].iloc[0]
        assert row["vs_prior_release"] is None or math.isnan(row["vs_prior_release"])


def test_vs_fg_labs_unaffected_by_the_prior_release_fix(tmp_path: Path) -> None:
    """The unrelated `vs_fg_labs` column (comparators vs today's candidate) was
    already correct -- confirm the rewrite didn't disturb it."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    def _vs_fg_labs(label: str) -> float:
        row = df[(df["label"] == label) & (df["mode"] == "default")].iloc[0]
        return float(row["vs_fg_labs"])

    assert math.isclose(_vs_fg_labs("bwa"), 70.0 / 50.0, rel_tol=TOL)
    assert math.isclose(_vs_fg_labs("minibwa"), 70.0 / 60.0, rel_tol=TOL)
    assert math.isclose(_vs_fg_labs("bwa-mem2-upstream"), 70.0 / 55.0, rel_tol=TOL)


def test_skipped_release_has_n_skipped_and_no_median(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    row = df[(df["label"] == "v030") & (df["mode"] == "default")].iloc[0]
    assert row["n_reps"] == 0
    assert row["n_skipped"] == 1
    assert math.isnan(row["median_wall_s"])


def test_vs_prior_release_does_not_bleed_across_arches(tmp_path: Path) -> None:
    """Multi-arch coverage `build_arena_table`'s tests otherwise lack entirely
    (every other test here uses one arch). CodeRabbit flagged that
    `vs_prior_release`/`vs_fg_labs` used to be built as plain per-arch-loop-
    iteration lists, correctly aligned with `grouped`'s rows only because
    `groupby(..., as_index=False)` happens to sort by `arch` today -- a
    future `sort=False` or an inserted `sort_values` would silently
    misalign them. Fixed by assigning through index-keyed dicts instead
    (pandas then aligns by index, not position).

    NOTE: this test does not itself force that sort assumption to break, so
    it passes under BOTH the old and new code -- confirmed by temporarily
    reverting the fix locally. It's kept anyway for the arch coverage it
    adds on its own merits, not as proof the fix's failure mode is real.
    """
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")

    for arch, v021_wall, v022_wall in (("c7i", 100.0, 50.0), ("c8g", 100.0, 25.0)):
        upsert_arena(
            conn,
            fg_labs_sha=FG_LABS_SHA,
            arch=arch,
            label="v021",
            mode="default",
            rep=1,
            wall_seconds=v021_wall,
            cpu_time=v021_wall * 10,
            max_rss_mb=1024.0,
            process_seconds=v021_wall * 0.9,
        )
        upsert_arena(
            conn,
            fg_labs_sha=FG_LABS_SHA,
            arch=arch,
            label="v022",
            mode="default",
            rep=1,
            wall_seconds=v022_wall,
            cpu_time=v022_wall * 10,
            max_rss_mb=1024.0,
            process_seconds=v022_wall * 0.9,
        )

    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    def _v022_ratio(arch: str) -> float:
        row = df[(df["arch"] == arch) & (df["label"] == "v022") & (df["mode"] == "default")]
        return float(row["vs_prior_release"].iloc[0])

    assert math.isclose(_v022_ratio("c7i"), 100.0 / 50.0, rel_tol=TOL)
    assert math.isclose(_v022_ratio("c8g"), 100.0 / 25.0, rel_tol=TOL)


def test_render_arena_markdown_mentions_fg_labs_default_predecessor(tmp_path: Path) -> None:
    """A light smoke test on the rendered markdown, not just the DataFrame."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    text = render_arena_markdown(db_path=db_path, fg_labs_sha=FG_LABS_SHA)
    assert "fg-labs-default" in text
    assert f"{80.0 / 70.0:.2f}x" in text
