"""`bench speedup` — fg-labs vs upstream baseline wall-clock speedup table.

Joins fg-labs trials at ``fg_labs_sha`` with the synthetic baseline trials
ingested by :func:`bwa_mem3_bench.storage.ingest.ingest_baseline` and emits
a markdown table with one row per ``(sample, arch)``.

For each (sample, arch):

- ``fg_labs_s`` is the minimum ``wall_seconds`` across reps for that fg-labs
  SHA (best-of-N is the standard "headline" perf number).
- ``baseline_s`` is the minimum ``wall_seconds`` across reps from the
  baseline trials (synthetic SHA ``baseline-bwa-mem2-<upstream_tag>``).
- ``speedup`` is ``baseline_s / fg_labs_s`` rendered as ``"<x.xx>x"``.

ARM archs (and any other (sample, arch) where the baseline tree is empty —
e.g. upstream bwa-mem2 v2.2.1 has no native ARM build) get an em-dash for
``baseline_s`` and ``speedup``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench.storage.ingest import baseline_sha_for
from bwa_mem3_bench.storage.queries import query_df

EM_DASH = "—"


def _best_walls(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Min wall_seconds + min process_seconds per (sample, arch) for `fg_labs_sha`.

    process_seconds is parsed from bwa-mem2's stderr `PROCESS()` line and
    excludes index loading — apples-to-apples kernel time, immune to host
    page-cache state. NULL when the stderr log is missing or unparseable
    (older runs that pre-date the stderr capture, or runs where bwa
    crashed before printing the profiling block).
    """
    return query_df(
        db_path,
        """
        SELECT
            sample,
            arch,
            MIN(wall_seconds) AS wall_seconds,
            MIN(process_seconds) AS process_seconds
        FROM trials
        WHERE fg_labs_sha = ? AND wall_seconds IS NOT NULL AND wall_seconds > 0
        GROUP BY sample, arch
        ORDER BY sample, arch
        """,
        params=(fg_labs_sha,),
    )


def build_speedup_table(
    *,
    db_path: Path,
    fg_labs_sha: str,
    upstream_tag: str,
) -> pd.DataFrame:
    """Return a DataFrame with both wall and compute speedups per (sample, arch).

    Columns: ``sample``, ``arch``, ``baseline_s`` (wall), ``fg_labs_s`` (wall),
    ``wall_speedup``, ``baseline_compute_s``, ``fg_labs_compute_s``,
    ``compute_speedup``. Compute columns are NaN when bwa stderr was not
    captured (pre-PROCESS()-parsing runs).
    """
    fg = _best_walls(db_path, fg_labs_sha).rename(
        columns={"wall_seconds": "fg_labs_s", "process_seconds": "fg_labs_compute_s"}
    )
    base = _best_walls(db_path, baseline_sha_for(upstream_tag)).rename(
        columns={"wall_seconds": "baseline_s", "process_seconds": "baseline_compute_s"}
    )
    merged = fg.merge(base, on=["sample", "arch"], how="left")
    merged["wall_speedup"] = merged["baseline_s"] / merged["fg_labs_s"]
    merged["compute_speedup"] = merged["baseline_compute_s"] / merged["fg_labs_compute_s"]
    return merged.sort_values(["sample", "arch"]).reset_index(drop=True)


def _format_seconds(value: float | None) -> str:
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.2f}"


def _format_speedup(value: float | None) -> str:
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.2f}x"


def render_speedup_markdown(
    *,
    db_path: Path,
    fg_labs_sha: str,
    upstream_tag: str,
) -> str:
    """Return the markdown speedup table as a string."""
    df = build_speedup_table(
        db_path=db_path,
        fg_labs_sha=fg_labs_sha,
        upstream_tag=upstream_tag,
    )

    lines = [
        f"# Speedup vs upstream bwa-mem2 {upstream_tag}: `{fg_labs_sha}`",
        "",
    ]

    if df.empty:
        lines.append(f"_No fg-labs trials ingested for `{fg_labs_sha}`._")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "Two speedup columns:",
            "",
            "- **`wall_speedup`** — `baseline_s / fg_labs_s`. End-user-experienced "
            "speedup; includes `bwa-mem2 shm` (fg-labs) and dummy-run page-cache "
            "prewarm (baseline) so both sides start with a warm index.",
            "- **`compute_speedup`** — `baseline_compute_s / fg_labs_compute_s` from "
            "bwa-mem2's `PROCESS()` profiling line. Excludes index loading; this is "
            "the apples-to-apples kernel speedup, host-state independent.",
            "",
        ]
    )

    headers = [
        "sample",
        "arch",
        "baseline_s",
        "fg_labs_s",
        "wall_speedup",
        "baseline_compute_s",
        "fg_labs_compute_s",
        "compute_speedup",
    ]
    aligners = ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:"]
    body_rows: list[str] = []
    for row in df.itertuples(index=False):
        body_rows.append(
            "| "
            + " | ".join(
                [
                    str(row.sample),
                    str(row.arch),
                    _format_seconds(row.baseline_s),
                    _format_seconds(row.fg_labs_s),
                    _format_speedup(row.wall_speedup),
                    _format_seconds(row.baseline_compute_s),
                    _format_seconds(row.fg_labs_compute_s),
                    _format_speedup(row.compute_speedup),
                ]
            )
            + " |"
        )

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(aligners) + " |")
    lines.extend(body_rows)
    lines.append("")
    return "\n".join(lines)


def generate_speedup(
    *,
    db_path: Path,
    fg_labs_sha: str,
    upstream_tag: str,
    out_md: Path | None,
) -> str:
    """Render the speedup table; write to `out_md` if given. Returns the markdown."""
    text = render_speedup_markdown(
        db_path=db_path,
        fg_labs_sha=fg_labs_sha,
        upstream_tag=upstream_tag,
    )
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(text)
    return text


__all__ = [
    "EM_DASH",
    "build_speedup_table",
    "generate_speedup",
    "render_speedup_markdown",
]
