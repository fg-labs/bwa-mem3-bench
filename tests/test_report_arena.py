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

from bwa_mem3_bench.report.arena import (
    build_arena_table,
    build_release_speedup_table,
    render_arena_markdown,
    render_release_speedup_markdown,
)
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


# The full arm shape as (label, mode, rep, wall) rows: 3 comparators, 4
# historical releases (one SKIPPED), fg-labs-default, fg-labs-fast. The four
# release labels are REAL `ARENA_RELEASES` entries in real chronological order
# (v021 < v022 < v030 < v040), because `build_arena_table` now ranks releases
# by that canonical ladder (`_release_chronology`), not by DB insertion order.
# Wall times are chosen so every ratio is distinct and easy to eyeball:
#   v021: 100.0   (oldest tracked -- no predecessor)
#   v022:  90.0   predecessor v021 -> 100/90
#   v030: SKIPPED (no wall time at all)
#   v040:  80.0   predecessor v022 (v030 skipped) -> 90/80
#   fg-labs-default: 70.0   predecessor v040 (latest measured) -> 80/70
#   fg-labs-fast:    65.0   never gets a vs_prior_release
# v040 has a --fast sibling; v021/v022 (predate --fast) and v030 (SKIPPED, no
# stock wall) deliberately do not -- covers the three release_speedup cases:
# both columns populated, --fast blank, both blank.
_FIXTURE_ROWS: list[tuple[str, str, int, float | None]] = [
    ("bwa", "default", 1, 50.0),
    ("minibwa", "default", 1, 60.0),
    ("bwa-mem2-upstream", "default", 1, 55.0),
    ("v021", "default", 1, 100.0),
    ("v022", "default", 1, 90.0),
    ("v030", "default", 1, None),
    ("v040", "default", 1, 80.0),
    ("fg-labs-default", "default", 1, 70.0),
    ("fg-labs-fast", "fast", 1, 65.0),
    ("v040-fast", "fast", 1, 40.0),
]


def _seed_rows(db_path: Path, rows: list[tuple[str, str, int, float | None]]) -> None:
    """Insert `rows` in the given order, so a test can control DB insertion
    order (`arena.id`) independently of chronology."""
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")
    for label, mode, rep, wall in rows:
        _row(conn, label=label, mode=mode, rep=rep, wall=wall)


def _seed_db(db_path: Path) -> None:
    """Seed the standard fixture in chronological / arm-spec order."""
    _seed_rows(db_path, _FIXTURE_ROWS)


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


def test_vs_prior_release_survives_shuffled_insertion_order(tmp_path: Path) -> None:
    """The bug this guards: `align_arena` writes arms to arena.tsv in an order
    that `arena_arms.front_load_fast_arms` SHUFFLES per run (seeded by the SHA),
    so `arena.id` insertion order is NOT chronological. The report used to sort
    the release chain by `min_id` and so divided each release by whatever label
    happened to precede it in the shuffle -- a real, silent corruption seen in
    the 0.11.0 arena (c8a v090 reported v030/v090, its shuffled neighbour).

    Insert the SAME fixture rows in a deliberately anti-chronological order; the
    ratios must be identical to the in-order `_seed_db` case. Under the old
    `min_id` sort this fails on at least v040 and fg-labs-default.
    """
    db_path = tmp_path / "benchmark.db"
    scrambled = [
        _FIXTURE_ROWS[7],  # fg-labs-default (newest) inserted FIRST
        _FIXTURE_ROWS[6],  # v040
        _FIXTURE_ROWS[3],  # v021
        _FIXTURE_ROWS[9],  # v040-fast
        _FIXTURE_ROWS[5],  # v030 (SKIPPED)
        _FIXTURE_ROWS[0],  # bwa
        _FIXTURE_ROWS[4],  # v022
        _FIXTURE_ROWS[8],  # fg-labs-fast
        _FIXTURE_ROWS[1],  # minibwa
        _FIXTURE_ROWS[2],  # bwa-mem2-upstream
    ]
    assert sorted(scrambled) == sorted(_FIXTURE_ROWS), "scramble must be a permutation"
    _seed_rows(db_path, scrambled)
    df = build_arena_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    assert _vs_prior(df, "v021") is None, "the oldest tracked release has no predecessor"
    _assert_vs_prior_close(df, "v022", 100.0 / 90.0, "chronology must ignore insertion order")
    assert _vs_prior(df, "v030") is None, "a SKIPPED release has nothing to compare"
    _assert_vs_prior_close(
        df, "v040", 90.0 / 80.0, "v040's predecessor is v022 by ladder order, not by min_id"
    )
    _assert_vs_prior_close(
        df, "fg-labs-default", 80.0 / 70.0, "the candidate's predecessor is the newest release"
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


def _speedup_row(df: pd.DataFrame, label: str) -> pd.Series:
    return df[df["label"] == label].iloc[0]


def test_release_speedup_reports_stock_and_fast_vs_baseline(tmp_path: Path) -> None:
    """`bwa` (wall=50.0) is the baseline. Every stock/fast wall in `_seed_db`
    is chosen so its ratio is distinct and easy to eyeball."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH)

    v040 = _speedup_row(df, "v040")
    assert math.isclose(v040["stock_median_wall_s"], 80.0, rel_tol=TOL)
    assert math.isclose(v040["stock_speedup"], 50.0 / 80.0, rel_tol=TOL)
    assert math.isclose(v040["fast_median_wall_s"], 40.0, rel_tol=TOL)
    assert math.isclose(v040["fast_speedup"], 50.0 / 40.0, rel_tol=TOL)

    candidate = _speedup_row(df, "fg-labs-default")
    assert math.isclose(candidate["stock_speedup"], 50.0 / 70.0, rel_tol=TOL)
    # fg-labs-fast, NOT fg-labs-default-fast -- the one label pair arena.smk
    # does not name `<label>-fast`.
    assert math.isclose(candidate["fast_median_wall_s"], 65.0, rel_tol=TOL)
    assert math.isclose(candidate["fast_speedup"], 50.0 / 65.0, rel_tol=TOL)


def test_release_speedup_blanks_fast_when_the_release_predates_it(tmp_path: Path) -> None:
    """v021/v022 have no `-fast` sibling seeded at all (they predate --fast in
    real release history) -- must read as blank, not zero or an error."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH)

    for label in ("v021", "v022"):
        row = _speedup_row(df, label)
        assert row["fast_median_wall_s"] is None or math.isnan(row["fast_median_wall_s"])
        assert row["fast_speedup"] is None or math.isnan(row["fast_speedup"])


def test_release_speedup_blanks_both_columns_for_a_skipped_release(tmp_path: Path) -> None:
    """v030 is SKIPPED (no stock wall at all, per `_seed_db`) -- both stock
    and fast must read as blank, not crash on a missing baseline lookup."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH)

    row = _speedup_row(df, "v030")
    assert row["stock_median_wall_s"] is None or math.isnan(row["stock_median_wall_s"])
    assert row["stock_speedup"] is None or math.isnan(row["stock_speedup"])
    assert row["fast_median_wall_s"] is None or math.isnan(row["fast_median_wall_s"])
    assert row["fast_speedup"] is None or math.isnan(row["fast_speedup"])


def test_release_speedup_excludes_comparators_from_the_release_rows(tmp_path: Path) -> None:
    """`bwa` is the baseline itself -- it (and minibwa, bwa-mem2-upstream)
    must not also appear as a "release" row; only bwa-mem3 releases + today's
    candidate do."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH)

    for label in ("bwa", "minibwa", "bwa-mem2-upstream"):
        assert label not in set(df["label"]), f"{label} is a comparator, not a release row"


def test_release_speedup_respects_a_non_default_baseline(tmp_path: Path) -> None:
    """`baseline_label` selects which arena comparator normalizes the
    speedup column -- not hardcoded to `bwa`."""
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(
        db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH, baseline_label="minibwa"
    )

    v040 = _speedup_row(df, "v040")
    assert math.isclose(v040["stock_speedup"], 60.0 / 80.0, rel_tol=TOL)


def test_release_speedup_empty_for_an_arch_with_no_arena_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch="c8g")
    assert df.empty


_EXPECTED_RELEASE_SPEEDUP_COLUMNS = [
    "label",
    "stock_median_wall_s",
    "stock_speedup",
    "fast_median_wall_s",
    "fast_speedup",
]


def test_release_speedup_preserves_columns_when_the_arch_has_only_comparators(
    tmp_path: Path,
) -> None:
    """A distinct empty case from the one above: `raw` is NOT empty (the arch
    has arena rows), but every one of them is a comparator (bwa/minibwa/
    bwa-mem2-upstream) -- no bwa-mem3 release has run on this arch yet. The
    `raw.empty` early return doesn't fire, so `_bwa_mem3_release_chain`
    filters every row out and `rows` ends up `[]`. `pd.DataFrame([])`
    collapses to zero columns instead of the five documented ones, which
    would break a caller indexing `df["label"]` even on an empty frame."""
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    other_sha = "cafef00d"
    upsert_run(conn, fg_labs_sha=other_sha, status="complete")
    upsert_arena(
        conn,
        fg_labs_sha=other_sha,
        arch=ARCH,
        label="bwa",
        mode="default",
        rep=1,
        wall_seconds=50.0,
        cpu_time=500.0,
        max_rss_mb=1024.0,
        process_seconds=45.0,
    )

    df = build_release_speedup_table(db_path=db_path, fg_labs_sha=other_sha, arch=ARCH)

    assert df.empty
    assert list(df.columns) == _EXPECTED_RELEASE_SPEEDUP_COLUMNS


def test_render_release_speedup_markdown_mentions_every_release(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    text = render_release_speedup_markdown(db_path=db_path, fg_labs_sha=FG_LABS_SHA, arch=ARCH)
    for label in ("v021", "v022", "v030", "v040", "fg-labs-default"):
        assert label in text
    assert f"{50.0 / 40.0:.2f}x" in text
