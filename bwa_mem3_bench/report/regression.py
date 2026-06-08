"""`bench regression` — gate a new SHA on concordance + perf.

Three gates:
  * Gate #1 (vs upstream bwa-mem2): observed concordance drift must stay within
    the per-sample budget declared in the divergence registry.
  * Gate #2 (vs the blessed golden / last release): concordance stays tight
    (>= CONCORDANCE_THRESHOLD) — two fg-labs builds should be ~identical.
  * Performance: median wall_seconds must not regress past the threshold.

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

from bwa_mem3_bench.registry import (
    DEFAULT_REGISTRY_PATH,
    DivergenceEntry,
    allowed_drift_pct,
    load_registry,
)
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE, VS_GOLDEN
from bwa_mem3_bench.storage.queries import query_df

# Gate #2 (vs the blessed golden / last release): two fg-labs builds should be
# ~identical, so this stays tight. Loosening it must be deliberate (re-bless +
# declared allowance).
CONCORDANCE_THRESHOLD = 99.999
PERF_REGRESSION_THRESHOLD_PCT = 5.0

# Gate #1 (vs upstream bwa-mem2): observed drift is gated against the per-sample
# budget declared in the divergence registry, not a flat threshold — workloads
# diverge from upstream by intentionally different amounts (exome ~0.0004% vs
# methylation ~1.1%). MARGIN absorbs float noise; concordance is deterministic
# per (sample), so it can be tiny.
DRIFT_MARGIN_PCT = 0.001

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


def _baseline_budget(conc_df: pd.DataFrame, registry: list[DivergenceEntry]) -> pd.DataFrame:
    """Gate #1: compare observed vs-upstream drift against each sample's budget.

    ``conc_df`` has columns ``sample, arch, baseline_concordance`` (min over
    reps). Returns the same rows plus ``observed_drift_pct``, ``allowed_drift_pct``,
    and a ``verdict`` of ``ok`` / ``over_budget``. Empty in, empty out.
    """
    cols = [
        "sample",
        "arch",
        "baseline_concordance",
        "observed_drift_pct",
        "allowed_drift_pct",
        "verdict",
    ]
    if conc_df.empty:
        return pd.DataFrame(columns=cols)
    out = conc_df.copy()
    out["observed_drift_pct"] = 100.0 - out["baseline_concordance"]
    out["allowed_drift_pct"] = out["sample"].map(lambda s: allowed_drift_pct(registry, s))
    out["verdict"] = [
        "over_budget" if obs > allowed + DRIFT_MARGIN_PCT else "ok"
        for obs, allowed in zip(out["observed_drift_pct"], out["allowed_drift_pct"], strict=False)
    ]
    return out.sort_values(["sample", "arch"]).reset_index(drop=True)[cols]


def _missing_baseline_cells(
    db_path: Path, new_df: pd.DataFrame, baseline_conc: pd.DataFrame
) -> pd.DataFrame:
    """(sample, arch) cells that should have a vs-baseline comparison but don't.

    "Should" = the cell exists in this run AND has an upstream baseline
    counterpart (a ``baseline-*`` trial). Intersecting with the baseline run's
    cells excludes ARM archs, which legitimately have no upstream comparison.
    """
    empty = pd.DataFrame(columns=["sample", "arch"])
    baseline_cells = query_df(
        db_path,
        "SELECT DISTINCT sample, arch FROM trials WHERE fg_labs_sha LIKE 'baseline-%'",
    )
    if baseline_cells.empty:
        return empty  # no baseline data to define expectations against
    new_cells = new_df[["sample", "arch"]].drop_duplicates()
    expected = new_cells.merge(baseline_cells, on=["sample", "arch"])
    present = (
        baseline_conc[["sample", "arch"]].drop_duplicates() if not baseline_conc.empty else empty
    )
    merged = expected.merge(present, on=["sample", "arch"], how="left", indicator=True)
    return merged[merged["_merge"] == "left_only"][["sample", "arch"]].reset_index(drop=True)


def check_regression(
    *,
    db_path: Path,
    new_sha: str,
    prev_sha: str,
) -> tuple[bool, str]:
    """Return ``(passes, markdown_report)``.

    Gate #1 (vs upstream): per-(sample) observed drift (100 - vs-baseline
    concordance) fails when it exceeds the sample's registry budget. Gate #2
    (vs golden): per-rep vs-golden concordance below the threshold fails its
    cell. Perf gating uses per-(sample, arch) median walls — a cell fails only
    when the median is over the regression threshold AND the new wall_s range
    lies entirely above the prev range (no overlap).
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

    # Gate #1: vs-upstream drift vs the per-sample registry budget.
    registry = load_registry(DEFAULT_REGISTRY_PATH)
    baseline_conc = query_df(
        db_path,
        """
        SELECT t.sample, t.arch, MIN(c.concordance_pct) AS baseline_concordance
        FROM trials t
        JOIN comparisons c ON c.trial_id = t.id AND c.kind = ?
        WHERE t.fg_labs_sha = ?
        GROUP BY t.sample, t.arch
        """,
        params=(VS_BASELINE, new_sha),
    )
    budget = _baseline_budget(baseline_conc, registry)
    budget_fails = budget[budget["verdict"] == "over_budget"]

    # Gate #1 fails *closed*: every cell that has an upstream baseline counterpart
    # must have produced a vs-baseline comparison. Intersecting the run's cells
    # with the baseline run's cells excludes ARM archs (no upstream baseline), so
    # a genuinely-missing x86/meth comparison fails rather than silently passing.
    missing_baseline = _missing_baseline_cells(db_path, new_df, baseline_conc)

    failing = (
        not concordance_fails.empty
        or not perf_fails.empty
        or not budget_fails.empty
        or not missing_baseline.empty
    )
    verdict = "FAIL" if failing else "PASS"

    lines: list[str] = [
        f"# Regression gate: {verdict}",
        "",
        f"- Gate #1 (vs upstream): observed drift must stay within the "
        f"per-sample registry budget (margin {DRIFT_MARGIN_PCT}%).",
        f"- Gate #2 (vs golden / last release): concordance ≥ {CONCORDANCE_THRESHOLD}%.",
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

    if not budget.empty:
        lines.append("## Gate #1 — vs-upstream drift vs budget")
        lines.append("")
        lines.append(
            md_table(
                [
                    "sample",
                    "arch",
                    "baseline_concordance",
                    "observed_drift_%",
                    "budget_%",
                    "verdict",
                ],
                budget[
                    [
                        "sample",
                        "arch",
                        "baseline_concordance",
                        "observed_drift_pct",
                        "allowed_drift_pct",
                        "verdict",
                    ]
                ]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    if not missing_baseline.empty:
        lines.append("## Gate #1 failures — missing vs-baseline comparisons")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch"],
                missing_baseline[["sample", "arch"]].to_records(index=False).tolist(),
            )
        )
        lines.append("")

    if not budget_fails.empty:
        lines.append("## Gate #1 failures — unexplained upstream divergence")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "observed_drift_%", "budget_%"],
                budget_fails[["sample", "arch", "observed_drift_pct", "allowed_drift_pct"]]
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
