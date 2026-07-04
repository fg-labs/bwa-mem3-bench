"""`bench compare` — concordance vs upstream (+ registry cross-check) and, when
present, concordance of the `--fast` preset vs the default bwa-mem3 arm."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.registry import DEFAULT_REGISTRY_PATH as REGISTRY_PATH
from bwa_mem3_bench.registry import load_registry
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE, VS_DEFAULT
from bwa_mem3_bench.storage.queries import query_df


def _load_comparisons(db_path: Path, fg_labs_sha: str, kind: str) -> pd.DataFrame:
    return query_df(
        db_path,
        """
        SELECT t.sample, t.arch, t.rep, c.concordant, c.total,
               c.concordance_pct, c.by_class_json, c.supp_json
        FROM trials t
        JOIN comparisons c ON c.trial_id = t.id
        WHERE t.fg_labs_sha = ? AND c.kind = ?
        ORDER BY t.sample, t.arch, t.rep
        """,
        params=(fg_labs_sha, kind),
    )


def _render_concordance_sections(df: pd.DataFrame, lines: list[str], *, heading: str) -> None:
    """Append per-trial concordance, by-class discordance, and supplementary
    divergence tables for one comparison DataFrame.

    Shared by the vs-upstream and vs-default sections so both render the same
    breakdown — in particular the by-class table is where a MAPQ-stratified
    divergence (e.g. `--fast`'s low-MAPQ-only differences) surfaces.
    """
    df = df.copy()
    df["drift_pct"] = 100.0 - df["concordance_pct"]
    lines.append(f"### {heading} per trial")
    lines.append("")
    lines.append(
        md_table(
            ["sample", "arch", "rep", "concordance_pct", "drift_pct", "total"],
            df[["sample", "arch", "rep", "concordance_pct", "drift_pct", "total"]]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.4f}",
        )
    )
    lines.append("")

    lines.append("### By-class discordance (sum across trials)")
    lines.append("")
    class_counts: dict[str, int] = {}
    for j in df["by_class_json"]:
        classes = json.loads(j) if j else {}
        for cls, entry in classes.items():
            if isinstance(entry, dict) and "count" in entry:
                class_counts[cls] = class_counts.get(cls, 0) + int(entry["count"])
    if not class_counts:
        lines.append("_no discordances recorded_")
    else:
        lines.append(
            md_table(
                ["class", "count"],
                sorted(class_counts.items(), key=lambda kv: kv[1], reverse=True),  # type: ignore[arg-type,return-value]
            )
        )
    lines.append("")

    lines.append("### Supplementary divergence")
    lines.append("")
    supp_rows: list[tuple[str, str, int, int, int, float]] = []
    for sample, arch, j in zip(df["sample"], df["arch"], df["supp_json"], strict=False):
        supp = json.loads(j) if isinstance(j, str) and j else {}
        if not supp:
            continue
        supp_rows.append(
            (
                sample,
                arch,
                int(supp.get("supp_query_total", 0)),
                int(supp.get("supp_baseline_total", 0)),
                int(supp.get("supp_count_mismatch_templates", 0)),
                float(supp.get("supp_unmatched_pct", 0.0)),
            )
        )
    if not supp_rows:
        lines.append("_no supplementary metrics recorded_")
    else:
        lines.append(
            md_table(
                ["sample", "arch", "supp_query", "supp_baseline", "count_mismatch", "unmatched_%"],
                supp_rows,
                float_fmt="{:.4f}",
            )
        )
    lines.append("")


def generate_compare(*, db_path: Path, fg_labs_sha: str, out_md: Path) -> None:
    df = _load_comparisons(db_path, fg_labs_sha, kind=VS_BASELINE)
    registry = load_registry(REGISTRY_PATH)
    expected_total = sum(e.expected_drift_pct for e in registry)

    lines = [f"# Drift report vs upstream: `{fg_labs_sha}`", ""]

    if df.empty:
        lines.append("_No baseline comparisons available._")
    else:
        df["drift_pct"] = 100.0 - df["concordance_pct"]
        lines.append("## Concordance vs upstream bwa-mem2")
        lines.append("")
        _render_concordance_sections(df, lines, heading="Concordance")

        lines.append("## Registry cross-check")
        lines.append("")
        lines.append(f"- Expected drift (sum of registry entries): {expected_total:.4f}%")
        observed_mean = df["drift_pct"].mean()
        lines.append(f"- Observed drift (mean across trials): {observed_mean:.4f}%")
        if registry:
            lines.append("")
            lines.append("### Registered divergences")
            lines.append("")
            lines.append(
                md_table(
                    ["id", "pr", "date", "affected", "expected_drift_pct", "summary"],
                    [
                        (e.id, e.pr, e.date, e.affected, e.expected_drift_pct, e.summary)
                        for e in registry
                    ],
                    float_fmt="{:.4f}",
                )
            )
        lines.append("")

    # `--fast` preset: concordance of each `<sample>-fast` arm vs its default
    # bwa-mem3 sibling (compare_vs_default). Only present when a `fast` run has
    # been collected; the by-class table is the MAPQ-stratified breakdown that
    # checks PR #189's "divergence confined to low-MAPQ reads" claim.
    fast_df = _load_comparisons(db_path, fg_labs_sha, kind=VS_DEFAULT)
    if not fast_df.empty:
        lines.append("## `--fast` preset vs default bwa-mem3")
        lines.append("")
        _render_concordance_sections(fast_df, lines, heading="Concordance")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines))
