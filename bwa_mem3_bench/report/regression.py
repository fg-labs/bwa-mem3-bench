"""`bench regression` — gate vs fg-labs golden (concordance + perf).

The report aggregates wall_seconds across reps to per-(sample, arch) medians
and ranges. A perf delta only fails the gate when the new wall_s range lies
entirely above the previous range — overlapping ranges are marked `noisy`
and explicitly do not fail the gate. This keeps Sapphire Rapids spot-pool
noise (m7i / c7i, ~10-50% CV per CLAUDE.md) from masquerading as a real
SHA-vs-SHA regression while still catching clean signals on the low-CV
archs (c6a / c7a / c7g / c8g, ~1% CV).
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_GOLDEN
from bwa_mem3_bench.storage.queries import query_df

CONCORDANCE_THRESHOLD = 99.999
PERF_REGRESSION_THRESHOLD_PCT = 5.0

# Sample-stdev (ddof=1) is undefined for a single sample; report 0% CV
# instead of NaN so the markdown table stays tidy.
_MIN_REPS_FOR_CV = 2


def _cv_pct(series: pd.Series) -> float:
    if len(series) < _MIN_REPS_FOR_CV:
        return 0.0
    mean = float(series.mean())
    if mean == 0:
        return 0.0
    return float(series.std(ddof=1) / mean * 100.0)


def _aggregate_perf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["sample", "arch", "n", "median", "min", "max", "cv_pct"])
    grouped = df.groupby(["sample", "arch"])["wall_seconds"]
    return pd.DataFrame(
        {
            "n": grouped.count(),
            "median": grouped.median(),
            "min": grouped.min(),
            "max": grouped.max(),
            "cv_pct": grouped.apply(_cv_pct),
        }
    ).reset_index()


def _classify(
    delta_pct: float,
    new_min: float,
    new_max: float,
    prev_min: float,
    prev_max: float,
) -> str:
    if math.isnan(delta_pct):
        return "new_only"
    if abs(delta_pct) <= PERF_REGRESSION_THRESHOLD_PCT:
        return "flat"
    if delta_pct > 0:
        return "REGRESSION" if new_min > prev_max else "noisy"
    return "improvement" if new_max < prev_min else "noisy"


def check_regression(
    *,
    db_path: Path,
    new_sha: str,
    prev_sha: str,
) -> tuple[bool, str]:
    """Return ``(passes, markdown_report)``.

    Perf gating uses per-(sample, arch) median walls. A cell fails only when
    the median is over the regression threshold AND the new wall_s range
    lies entirely above the prev wall_s range (no overlap). Concordance is
    per-rep: any rep below the threshold fails its cell.
    """
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

    new_perf = _aggregate_perf(new_df)
    prev_perf = _aggregate_perf(prev_df)

    cells = new_perf.merge(
        prev_perf,
        on=["sample", "arch"],
        suffixes=("_new", "_prev"),
        how="left",
    )
    cells["delta_pct"] = (
        (cells["median_new"] - cells["median_prev"]) / cells["median_prev"]
    ) * 100.0
    cells["verdict"] = [
        _classify(d, nmn, nmx, pmn, pmx)
        for d, nmn, nmx, pmn, pmx in zip(
            cells["delta_pct"],
            cells["min_new"],
            cells["max_new"],
            cells["min_prev"],
            cells["max_prev"],
            strict=False,
        )
    ]
    cells = cells.sort_values(["arch", "sample"]).reset_index(drop=True)

    conc = (
        new_df[new_df["golden_concordance"].notna()]
        .groupby(["sample", "arch"])["golden_concordance"]
        .min()
        .reset_index()
    )
    concordance_fails = conc[conc["golden_concordance"] < CONCORDANCE_THRESHOLD]
    perf_fails = cells[cells["verdict"] == "REGRESSION"]

    failing = not concordance_fails.empty or not perf_fails.empty
    verdict = "FAIL" if failing else "PASS"

    lines: list[str] = [
        f"# Regression gate: {verdict}",
        "",
        f"- Concordance threshold: ≥ {CONCORDANCE_THRESHOLD}%",
        f"- Performance regression threshold: ≤ {PERF_REGRESSION_THRESHOLD_PCT}%",
        "- Reps aggregated by median; perf verdict is `REGRESSION` /",
        "  `improvement` only when the new and prev wall_s ranges do **not**",
        "  overlap (otherwise `noisy`, which does not fail the gate).",
        "",
        "## Per-cell summary",
        "",
        md_table(
            [
                "sample",
                "arch",
                "n_new",
                "n_prev",
                "new_med_s",
                "new_cv%",
                "prev_med_s",
                "prev_cv%",
                "delta_%",
                "verdict",
            ],
            cells[
                [
                    "sample",
                    "arch",
                    "n_new",
                    "n_prev",
                    "median_new",
                    "cv_pct_new",
                    "median_prev",
                    "cv_pct_prev",
                    "delta_pct",
                    "verdict",
                ]
            ]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.2f}",
        ),
        "",
    ]

    if not concordance_fails.empty:
        lines.append("## Concordance failures")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "min_golden_concordance"],
                concordance_fails[["sample", "arch", "golden_concordance"]]
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
                ["sample", "arch", "new_med_s", "prev_med_s", "delta_%"],
                perf_fails[["sample", "arch", "median_new", "median_prev", "delta_pct"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.2f}",
            )
        )
        lines.append("")

    return not failing, "\n".join(lines)
