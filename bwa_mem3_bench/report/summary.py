"""`bench summary` — one-page dashboard for a single fg-labs commit."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE
from bwa_mem3_bench.storage.queries import query_df


def _load_trials(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    return query_df(
        db_path,
        """
        SELECT t.sample, t.arch, t.rep,
               t.wall_seconds, t.max_rss_mb, t.cpu_time,
               t.io_read_mb, t.io_write_mb,
               c.concordance_pct, c.total AS comp_total
        FROM trials t
        LEFT JOIN comparisons c
          ON c.trial_id = t.id AND c.kind = ?
        WHERE t.fg_labs_sha = ?
        ORDER BY t.sample, t.arch, t.rep
        """,
        params=(VS_BASELINE, fg_labs_sha),
    )


def generate_summary(*, db_path: Path, fg_labs_sha: str, out_md: Path) -> None:
    """Write a markdown one-pager to `out_md`."""
    df = _load_trials(db_path, fg_labs_sha)
    lines = [f"# bwa-mem3-bench summary: `{fg_labs_sha}`", ""]

    if df.empty:
        lines.append("_No trials ingested for this SHA._")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text("\n".join(lines))
        return

    lines.append("## Performance")
    lines.append("")
    lines.append(
        md_table(
            ["sample", "arch", "rep", "wall_s", "max_rss_mb", "io_read_mb", "io_write_mb"],
            df[["sample", "arch", "rep", "wall_seconds", "max_rss_mb", "io_read_mb", "io_write_mb"]]
            .to_records(index=False)
            .tolist(),
        )
    )
    lines.append("")

    lines.append("## Concordance (vs upstream baseline)")
    lines.append("")
    conc = df[df["concordance_pct"].notna()]
    if conc.empty:
        lines.append("_No baseline comparisons available._")
    else:
        lines.append(
            md_table(
                ["sample", "arch", "rep", "concordance_pct", "total"],
                conc[["sample", "arch", "rep", "concordance_pct", "comp_total"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
    lines.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines))
