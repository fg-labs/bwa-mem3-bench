"""`bench trend` — time-series of wall-clock + concordance over recent commits."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench.report.plots import trend_over_commits
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE
from bwa_mem3_bench.storage.queries import query_df


def _load_trend(db_path: Path, last: int) -> pd.DataFrame:
    return query_df(
        db_path,
        """
        WITH recent AS (
            SELECT fg_labs_sha
            FROM runs
            ORDER BY submitted_at DESC
            LIMIT ?
        )
        SELECT r.fg_labs_sha, r.submitted_at, t.sample, t.arch, t.rep,
               t.wall_seconds, c.concordance_pct
        FROM trials t
        JOIN runs r ON r.fg_labs_sha = t.fg_labs_sha
        JOIN recent ON recent.fg_labs_sha = r.fg_labs_sha
        LEFT JOIN comparisons c
          ON c.trial_id = t.id AND c.kind = ?
        ORDER BY r.submitted_at DESC
        """,
        params=(last, VS_BASELINE),
    )


def generate_trend(*, db_path: Path, out_dir: Path, last: int = 20) -> None:
    df = _load_trend(db_path, last)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "trend.md"

    lines = ["# Trend report", ""]
    if df.empty:
        md.write_text("\n".join(lines + ["_No trials in database._"]))
        return

    lines.append(f"Last {last} commits.")
    lines.append("")

    agg = (
        df.groupby(["fg_labs_sha", "sample", "arch"])
        .agg(
            wall_mean=("wall_seconds", "mean"),
            concordance=("concordance_pct", "mean"),
        )
        .reset_index()
    )

    lines.append("## Summary (mean across reps)")
    lines.append("")
    lines.append(
        md_table(
            ["fg_labs_sha", "sample", "arch", "wall_mean_s", "concordance_pct"],
            agg[["fg_labs_sha", "sample", "arch", "wall_mean", "concordance"]]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.4f}",
        )
    )
    lines.append("")

    try:
        trend_over_commits(
            agg,
            y="wall_mean",
            title="Wall clock trend",
            y_title="Wall clock (s)",
            out_png=out_dir / "trend_wall.png",
            color="arch",
        )
        lines.append("![Wall clock trend](trend_wall.png)")
        lines.append("")
    except Exception as e:  # noqa: BLE001
        lines.append(f"_trend plot failed: {e}_")

    md.write_text("\n".join(lines))
