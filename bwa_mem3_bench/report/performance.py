"""`bench report` — full performance report: tables + PNG plots."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.report.plots import bar_by_arch
from bwa_mem3_bench.report.speedup import render_speedup_markdown
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage.ingest import baseline_sha_for
from bwa_mem3_bench.storage.queries import query_df
from bwa_mem3_bench.workflow_config import load_config

METRICS: list[tuple[str, str]] = [
    ("wall_seconds", "Wall clock (s)"),
    ("max_rss_mb", "Peak RSS (MB)"),
    ("io_read_mb", "Disk read (MB)"),
    ("io_write_mb", "Disk write (MB)"),
    ("cpu_time", "CPU time (s)"),
]

# Default upstream tag used when the workflow config can't be loaded.
_DEFAULT_UPSTREAM_TAG = "v2.2.1"


def _load_trials(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    return query_df(
        db_path,
        "SELECT sample, arch, rep, wall_seconds, max_rss_mb, io_read_mb, "
        "io_write_mb, cpu_time FROM trials WHERE fg_labs_sha = ? "
        "ORDER BY sample, arch, rep",
        params=(fg_labs_sha,),
    )


def _load_baseline_walls(db_path: Path, upstream_tag: str) -> pd.DataFrame:
    """Min wall_seconds per (sample, arch) for the synthetic baseline SHA."""
    return query_df(
        db_path,
        """
        SELECT sample, arch, MIN(wall_seconds) AS baseline_s
        FROM trials
        WHERE fg_labs_sha = ? AND wall_seconds IS NOT NULL AND wall_seconds > 0
        GROUP BY sample, arch
        """,
        params=(baseline_sha_for(upstream_tag),),
    )


def _resolve_upstream_tag() -> str:
    """Read upstream_tag from the workflow config; fall back to v2.2.1."""
    try:
        return load_config(Path(REPO_ROOT) / "config").upstream_tag
    except (FileNotFoundError, KeyError, OSError):
        return _DEFAULT_UPSTREAM_TAG


def _format_seconds(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{float(value):.2f}"  # type: ignore[arg-type]


def _format_speedup(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{float(value):.2f}x"  # type: ignore[arg-type]


def _wall_table_with_speedup(df: pd.DataFrame, baseline_walls: pd.DataFrame) -> str:
    """Render the wall-clock table with baseline_s and speedup columns."""
    merged = df.merge(baseline_walls, on=["sample", "arch"], how="left")
    merged["speedup"] = merged["baseline_s"] / merged["wall_seconds"]

    headers = ["sample", "arch", "rep", "baseline_s", "fg_labs_s", "speedup"]
    aligners = ["---", "---", "---", "---:", "---:", "---:"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(aligners) + " |"]
    for row in merged.itertuples(index=False):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.sample),
                    str(row.arch),
                    str(row.rep),
                    _format_seconds(row.baseline_s),
                    _format_seconds(row.wall_seconds),
                    _format_speedup(row.speedup),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def generate_performance(*, db_path: Path, fg_labs_sha: str, out_dir: Path) -> None:
    df = _load_trials(db_path, fg_labs_sha)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"

    lines = [f"# Performance report: `{fg_labs_sha}`", ""]

    if df.empty:
        md_path.write_text("\n".join(lines + ["_No trials to report._"]))
        return

    upstream_tag = _resolve_upstream_tag()
    baseline_walls = _load_baseline_walls(db_path, upstream_tag)

    # Headline speedup section first, then the per-metric tables.
    lines.append(
        render_speedup_markdown(
            db_path=db_path,
            fg_labs_sha=fg_labs_sha,
            upstream_tag=upstream_tag,
        )
    )
    lines.append("")

    for col, label in METRICS:
        lines.append(f"## {label}")
        lines.append("")
        if col == "wall_seconds":
            lines.append(_wall_table_with_speedup(df, baseline_walls))
        else:
            lines.append(
                md_table(
                    ["sample", "arch", "rep", col],
                    df[["sample", "arch", "rep", col]].to_records(index=False).tolist(),
                )
            )
        lines.append("")
        png = out_dir / f"{col}.png"
        try:
            bar_by_arch(df, y=col, title=label, y_title=label, out_png=png)
            lines.append(f"![{label}]({png.name})")
            lines.append("")
        except Exception as e:  # noqa: BLE001
            lines.append(f"_plot generation failed: {e}_")
            lines.append("")

    md_path.write_text("\n".join(lines))
