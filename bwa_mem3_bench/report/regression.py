"""`bench regression` — gate vs fg-labs golden (concordance + perf)."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_GOLDEN
from bwa_mem3_bench.storage.queries import query_df

CONCORDANCE_THRESHOLD = 99.999
PERF_REGRESSION_THRESHOLD_PCT = 5.0


def check_regression(
    *,
    db_path: Path,
    new_sha: str,
    prev_sha: str,
) -> tuple[bool, str]:
    """Return (passes, markdown_report)."""
    new_df = query_df(
        db_path,
        """
        SELECT t.sample, t.arch, t.rep, t.wall_seconds,
               c.concordance_pct AS golden_concordance
        FROM trials t
        LEFT JOIN comparisons c
          ON c.trial_id = t.id AND c.kind = ?
        WHERE t.fg_labs_sha = ?
        """,
        params=(VS_GOLDEN, new_sha),
    )
    prev_df = query_df(
        db_path,
        "SELECT sample, arch, rep, wall_seconds FROM trials WHERE fg_labs_sha = ?",
        params=(prev_sha,),
    )

    if new_df.empty:
        return False, f"# Regression gate: FAIL\n\n_No trials for {new_sha}._\n"

    joined = new_df.merge(
        prev_df,
        on=["sample", "arch", "rep"],
        suffixes=("_new", "_prev"),
        how="left",
    )
    joined["perf_delta_pct"] = (
        (joined["wall_seconds_new"] - joined["wall_seconds_prev"]) / joined["wall_seconds_prev"]
    ) * 100.0

    concordance_fails = joined[
        joined["golden_concordance"].notna()
        & (joined["golden_concordance"] < CONCORDANCE_THRESHOLD)
    ]
    perf_fails = joined[
        joined["perf_delta_pct"].notna()
        & (joined["perf_delta_pct"] > PERF_REGRESSION_THRESHOLD_PCT)
    ]

    failing = not concordance_fails.empty or not perf_fails.empty
    verdict = "FAIL" if failing else "PASS"
    lines = [f"# Regression gate: {verdict}", ""]
    lines.append(f"- Concordance threshold: ≥ {CONCORDANCE_THRESHOLD}%")
    lines.append(f"- Performance regression threshold: ≤ {PERF_REGRESSION_THRESHOLD_PCT}%")
    lines.append("")

    lines.append("## Per-trial summary")
    lines.append("")
    lines.append(
        md_table(
            ["sample", "arch", "rep", "wall_new", "wall_prev", "delta_%", "golden_conc_%"],
            joined[
                [
                    "sample",
                    "arch",
                    "rep",
                    "wall_seconds_new",
                    "wall_seconds_prev",
                    "perf_delta_pct",
                    "golden_concordance",
                ]
            ]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.4f}",
        )
    )
    lines.append("")

    if not concordance_fails.empty:
        lines.append("## Concordance failures")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "rep", "golden_concordance"],
                concordance_fails[["sample", "arch", "rep", "golden_concordance"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    if not perf_fails.empty:
        lines.append("## Performance regressions")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "rep", "perf_delta_pct"],
                perf_fails[["sample", "arch", "rep", "perf_delta_pct"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    return not failing, "\n".join(lines)
