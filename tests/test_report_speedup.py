"""Tests for `bench speedup` and the speedup column in `bench report`."""

from __future__ import annotations

import math
from pathlib import Path

from bwa_mem3_bench.report.performance import generate_performance
from bwa_mem3_bench.report.speedup import (
    build_speedup_table,
    generate_speedup,
    render_speedup_markdown,
)
from bwa_mem3_bench.storage.ingest import baseline_sha_for, minibwa_sha_for
from bwa_mem3_bench.storage.sqlite import connect, upsert_run, upsert_trial

FG_LABS_SHA = "abc1234"
UPSTREAM_TAG = "v2.2.1"

# Seeded values; kept as module-level constants so test assertions don't trip
# ruff's PLR2004 "magic value in comparison" rule.
FG_LABS_WALL = 12.0
BASELINE_WALL_REP1 = 13.8
BASELINE_WALL_REP2 = 14.5
EXPECTED_SPEEDUP = BASELINE_WALL_REP1 / FG_LABS_WALL  # 1.15
SPEEDUP_TOL = 1e-9
ARM_DASH_COUNT = (
    5  # baseline_s, wall_speedup, baseline_compute_s, fg_labs_compute_s, compute_speedup
)


def _seed_db(db_path: Path) -> None:
    """Seed a tiny benchmark.db with both fg-labs and synthetic-baseline trials.

    Layout:

      fg-labs (`abc1234`):
        - smoke-1M / c6a / rep-1: 12.0s
        - smoke-1M / c7g / rep-1: 12.0s   (no x86 baseline ⇒ speedup blank)

      baseline (synthetic SHA `baseline-bwa-mem2-v2.2.1`):
        - smoke-1M / c6a / rep-1: 13.8s   (baseline beats fg-labs by 1.15x)
    """
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")

    # fg-labs trials (one x86, one ARM).
    for arch in ("c6a", "c7g"):
        upsert_trial(
            conn,
            fg_labs_sha=FG_LABS_SHA,
            sample="smoke-1M",
            arch=arch,
            rep=1,
            wall_seconds=FG_LABS_WALL,
            max_rss_mb=1024.0,
            cpu_time=48.0,
            io_read_mb=200.0,
            io_write_mb=50.0,
            mean_load=380.0,
            reads_processed=2003,
            instance_type=f"{arch}.4xlarge",
            availability_zone="us-east-1a",
            spot_price=None,
            status="ok",
        )

    # Synthetic baseline: only x86 has a baseline. Multiple reps to verify
    # we take the MIN.
    synthetic = baseline_sha_for(UPSTREAM_TAG)
    upsert_run(conn, fg_labs_sha=synthetic, upstream_tag=UPSTREAM_TAG, status="baseline")
    for rep, wall in ((1, BASELINE_WALL_REP1), (2, BASELINE_WALL_REP2)):
        upsert_trial(
            conn,
            fg_labs_sha=synthetic,
            sample="smoke-1M",
            arch="c6a",
            rep=rep,
            wall_seconds=wall,
            max_rss_mb=1024.0,
            cpu_time=48.0,
            io_read_mb=200.0,
            io_write_mb=50.0,
            mean_load=380.0,
            reads_processed=0,
            instance_type="c6a.4xlarge",
            availability_zone="us-east-1a",
            spot_price=None,
            status="ok",
        )
    conn.close()


def test_build_speedup_table_uses_min_walls(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    df = build_speedup_table(db_path=db, fg_labs_sha=FG_LABS_SHA, upstream_tag=UPSTREAM_TAG)

    rows = df.set_index(["sample", "arch"])
    assert rows.loc[("smoke-1M", "c6a"), "fg_labs_s"] == FG_LABS_WALL
    # MIN of the two baseline reps wins.
    assert rows.loc[("smoke-1M", "c6a"), "baseline_s"] == BASELINE_WALL_REP1
    assert abs(rows.loc[("smoke-1M", "c6a"), "wall_speedup"] - EXPECTED_SPEEDUP) < SPEEDUP_TOL

    # ARM has no baseline ⇒ NaN wall_speedup
    assert math.isnan(rows.loc[("smoke-1M", "c7g"), "baseline_s"])
    assert math.isnan(rows.loc[("smoke-1M", "c7g"), "wall_speedup"])


def test_render_speedup_markdown_contains_x_suffix_and_emdash(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    md = render_speedup_markdown(db_path=db, fg_labs_sha=FG_LABS_SHA, upstream_tag=UPSTREAM_TAG)
    # Headline + table headers.
    assert "Speedup vs upstream bwa-mem2 v2.2.1" in md
    assert FG_LABS_SHA in md
    assert (
        "| sample | arch | compute_speedup | wall_speedup | "
        "fg_labs_compute_s | baseline_compute_s | fg_labs_s | baseline_s |"
    ) in md
    # Right-aligned numeric columns.
    assert "---:" in md
    # 13.8 / 12.0 ≈ 1.15
    assert "1.15x" in md
    # ARM row ⇒ em-dashes for baseline_s, wall_speedup, and all 3 compute columns
    # (compute fields NaN regardless of arch since the test fixture has no stderr).
    arm_row = next(line for line in md.splitlines() if "| c7g |" in line)
    assert arm_row.count("—") == ARM_DASH_COUNT
    assert "12.00" in arm_row  # fg-labs wall is still rendered


def test_generate_speedup_writes_to_out(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    out = tmp_path / "speedup.md"
    text = generate_speedup(
        db_path=db, fg_labs_sha=FG_LABS_SHA, upstream_tag=UPSTREAM_TAG, out_md=out
    )
    assert out.exists()
    assert out.read_text() == text
    assert "1.15x" in out.read_text()


def test_generate_speedup_no_trials(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    # Connect just to create the schema; do not seed any trials.
    connect(db).close()
    text = render_speedup_markdown(db_path=db, fg_labs_sha="missing", upstream_tag=UPSTREAM_TAG)
    assert "No fg-labs trials" in text


MINIBWA_SHA = "a8cf4d336613672213dd2df89e9fe9cbc041c31e"
MINIBWA_WALL = 6.0  # half of FG_LABS_WALL ⇒ minibwa_speedup 2.0x on c6a
EXPECTED_MINIBWA_SPEEDUP = FG_LABS_WALL / MINIBWA_WALL  # 2.0


def _seed_minibwa(db_path: Path) -> None:
    """Add a single minibwa trial (smoke-1M / c6a) to an already-seeded db."""
    conn = connect(db_path)
    synthetic = minibwa_sha_for(MINIBWA_SHA)
    upsert_run(conn, fg_labs_sha=synthetic, status="minibwa")
    upsert_trial(
        conn,
        fg_labs_sha=synthetic,
        sample="smoke-1M",
        arch="c6a",
        rep=1,
        wall_seconds=MINIBWA_WALL,
        max_rss_mb=1024.0,
        cpu_time=48.0,
        io_read_mb=200.0,
        io_write_mb=50.0,
        mean_load=380.0,
        reads_processed=0,
        instance_type="c6a.4xlarge",
        availability_zone="us-east-1a",
        spot_price=None,
        status="ok",
    )
    conn.close()


def test_speedup_table_omits_minibwa_columns_by_default(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    df = build_speedup_table(db_path=db, fg_labs_sha=FG_LABS_SHA, upstream_tag=UPSTREAM_TAG)
    assert "minibwa_speedup" not in df.columns
    assert "minibwa_s" not in df.columns


def test_speedup_table_adds_minibwa_columns_when_sha_given(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    _seed_minibwa(db)
    df = build_speedup_table(
        db_path=db,
        fg_labs_sha=FG_LABS_SHA,
        upstream_tag=UPSTREAM_TAG,
        minibwa_sha=MINIBWA_SHA,
    )
    rows = df.set_index(["sample", "arch"])
    assert rows.loc[("smoke-1M", "c6a"), "minibwa_s"] == MINIBWA_WALL
    assert (
        abs(rows.loc[("smoke-1M", "c6a"), "minibwa_speedup"] - EXPECTED_MINIBWA_SPEEDUP)
        < SPEEDUP_TOL
    )
    # No minibwa trial for the ARM arch ⇒ NaN.
    assert math.isnan(rows.loc[("smoke-1M", "c7g"), "minibwa_s"])


def test_render_speedup_markdown_includes_minibwa_columns(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    _seed_minibwa(db)
    md = render_speedup_markdown(
        db_path=db,
        fg_labs_sha=FG_LABS_SHA,
        upstream_tag=UPSTREAM_TAG,
        minibwa_sha=MINIBWA_SHA,
    )
    assert "minibwa_speedup | minibwa_s |" in md
    assert f"minibwa-{MINIBWA_SHA}" in md
    assert "2.00x" in md  # fg_labs 12.0 / minibwa 6.0


def test_performance_report_includes_speedup_column(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed_db(db)
    out_dir = tmp_path / "report"
    generate_performance(db_path=db, fg_labs_sha=FG_LABS_SHA, out_dir=out_dir)
    md = (out_dir / "report.md").read_text()
    # Headline speedup section ahead of per-metric tables.
    assert "Speedup vs upstream bwa-mem2" in md
    # Wall-clock table now has baseline_s, fg_labs_s, speedup columns.
    assert "| sample | arch | rep | baseline_s | fg_labs_s | speedup |" in md
    assert "1.15x" in md
