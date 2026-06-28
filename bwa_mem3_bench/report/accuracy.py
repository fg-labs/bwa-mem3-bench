"""`bench accuracy` — truth-based alignment-accuracy report.

Reads the `accuracy` table (populated by
:func:`bwa_mem3_bench.storage.ingest.ingest_accuracy` from holodeck eval
outputs) and renders a markdown report with three sections, one row per
aligner arm (`tool`) within each sim dataset (`sample`):

1. **Placement + MAPQ calibration** — correct / mismapped / unmapped rate
   (the `ALL`-bin headline). The cross-tool comparison: bwa-mem3 (`fg-labs`)
   vs `minibwa` vs upstream `bwa-mem2` (non-meth) / `bwameth` (meth).
2. **Variant representation + methylation** — per-read variant-bearing count,
   MD/NM concordance vs golden, and (meth) the per-CpG methylation-level
   Pearson r / RMSE.
3. **Per-class AS/MAPQ honesty** — the genomic-vs-collapsed headline: for each
   substitution class, is the aligner's `AS`/`MAPQ` deflated on reads carrying
   a recoverable variant? The conversion-direction class (C→T / G→A) is flagged
   *confounded* — no mode resolves it from a single read.

Metrics are averaged across reps for the headline tables; the per-class table
takes the lowest rep per cell (JSON blobs aren't meaningfully averaged).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.storage.queries import query_df

EM_DASH = "—"

# Display order for the aligner arms (fg-labs is the protagonist; baseline and
# minibwa are the references it's graded against).
_TOOL_ORDER = {"fg-labs": 0, "baseline": 1, "minibwa": 2}


def _tool_rank(tool: str) -> int:
    return _TOOL_ORDER.get(tool, 99)


def build_accuracy_table(*, db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Per-(sample, arch, tool) headline accuracy, averaged across reps.

    Columns: ``sample``, ``arch``, ``tool``, placement rates
    (``placement_correct_pct`` / ``_mismapped_pct`` / ``_unmapped_pct`` /
    ``placement_total``), variant representation (``variant_bearing_reads``,
    ``md_concordant_pct``, ``nm_concordant_pct``), and methylation correlation
    (``meth_n_cpg``, ``meth_pearson_r``, ``meth_rmse``). The meth columns are
    NaN for non-meth datasets (SQL ``AVG`` over all-NULL is NULL).
    """
    df = query_df(
        db_path,
        """
        SELECT
            sample,
            arch,
            tool,
            AVG(placement_correct_pct)   AS placement_correct_pct,
            AVG(placement_mismapped_pct) AS placement_mismapped_pct,
            AVG(placement_unmapped_pct)  AS placement_unmapped_pct,
            AVG(placement_total)         AS placement_total,
            AVG(variant_bearing_reads)   AS variant_bearing_reads,
            AVG(md_concordant_pct)       AS md_concordant_pct,
            AVG(nm_concordant_pct)       AS nm_concordant_pct,
            AVG(meth_n_cpg)              AS meth_n_cpg,
            AVG(meth_pearson_r)          AS meth_pearson_r,
            AVG(meth_rmse)               AS meth_rmse
        FROM accuracy
        WHERE fg_labs_sha = ?
        GROUP BY sample, arch, tool
        """,
        params=(fg_labs_sha,),
    )
    return _sort_by_sample_tool(df)


def build_accuracy_class_table(*, db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Per-(sample, tool, class) substitution-class table from `by_class_json`.

    One row per substitution class per arm, taken from the lowest rep of each
    (sample, arch, tool) cell. Columns: ``sample``, ``arch``, ``tool``,
    ``class``, ``confounded``, ``n_expected``, ``n_represented``,
    ``represented_pct``, ``mean_mapq``, ``mean_as``. Empty when no accuracy
    rows exist for the SHA.
    """
    raw = query_df(
        db_path,
        """
        SELECT sample, arch, tool, rep, by_class_json
        FROM accuracy
        WHERE fg_labs_sha = ?
        ORDER BY sample, arch, tool, rep
        """,
        params=(fg_labs_sha,),
    )
    rows: list[dict[str, object]] = []
    seen_cells: set[tuple[str, str, str]] = set()
    for row in raw.itertuples(index=False):
        cell = (row.sample, row.arch, row.tool)
        if cell in seen_cells:  # keep only the lowest rep (rows are rep-ordered)
            continue
        # `by_class_json` is nullable, so the lowest rep for a cell may carry no
        # class payload yet. Only claim the cell once a row has usable data, so a
        # later populated rep still gets surfaced instead of being skipped.
        if not row.by_class_json:
            continue
        seen_cells.add(cell)
        by_class = json.loads(row.by_class_json)
        for label, acc in by_class.items():
            rows.append(
                {
                    "sample": row.sample,
                    "arch": row.arch,
                    "tool": row.tool,
                    # `subclass` (not `class`) so DataFrame.itertuples() can
                    # surface it — `class` is a Python keyword and pandas mangles
                    # it to a positional name.
                    "subclass": label,
                    "confounded": bool(acc.get("confounded", False)),
                    "n_expected": acc.get("n_expected"),
                    "n_represented": acc.get("n_represented"),
                    "represented_pct": acc.get("represented_pct"),
                    "mean_mapq": acc.get("mean_mapq"),
                    "mean_as": acc.get("mean_as"),
                }
            )
    df = pd.DataFrame(rows)
    return _sort_by_sample_tool(df, extra=["subclass"])


def _sort_by_sample_tool(df: pd.DataFrame, extra: list[str] | None = None) -> pd.DataFrame:
    """Sort by sample, arm display order, arch, then any `extra` columns.

    `arch` is in the key so rows stay deterministic if a sample is ever graded
    on more than one arch (accuracy pins one arch per chemistry today, but the
    schema allows several).
    """
    if df.empty:
        return df.reset_index(drop=True)
    df = df.copy()
    df["_tool_rank"] = df["tool"].map(_tool_rank)
    by = ["sample", "_tool_rank", "arch", *(extra or [])]
    return df.sort_values(by).drop(columns="_tool_rank").reset_index(drop=True)


def _fmt(value: float | None, digits: int) -> str:
    """Format a numeric cell to `digits` decimals; em-dash for NaN/None."""
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.{digits}f}"


def _fmt_int(value: float | None) -> str:
    if value is None or pd.isna(value):
        return EM_DASH
    return str(round(float(value)))


def _md_table(headers: list[str], aligners: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(aligners) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows)
    return lines


def _placement_section(df: pd.DataFrame) -> list[str]:
    headers = ["sample", "tool", "arch", "correct_%", "mismapped_%", "unmapped_%", "reads"]
    aligners = ["---", "---", "---", "---:", "---:", "---:", "---:"]
    rows = [
        [
            str(r.sample),
            str(r.tool),
            str(r.arch),
            _fmt(r.placement_correct_pct, 2),
            _fmt(r.placement_mismapped_pct, 2),
            _fmt(r.placement_unmapped_pct, 2),
            _fmt_int(r.placement_total),
        ]
        for r in df.itertuples(index=False)
    ]
    return [
        "## Placement + MAPQ calibration",
        "",
        "Correct / mismapped / unmapped rate vs golden truth (the `ALL`-bin "
        "headline). Compare across tools within a dataset: bwa-mem3 (`fg-labs`) "
        "vs `minibwa` vs the baseline (`bwa-mem2` non-meth / `bwameth` meth).",
        "",
        *_md_table(headers, aligners, rows),
        "",
    ]


def _variant_section(df: pd.DataFrame) -> list[str]:
    headers = [
        "sample",
        "tool",
        "arch",
        "variant_reads",
        "md_concord_%",
        "nm_concord_%",
        "meth_r",
        "meth_rmse",
        "n_cpg",
    ]
    aligners = ["---", "---", "---", "---:", "---:", "---:", "---:", "---:", "---:"]
    rows = [
        [
            str(r.sample),
            str(r.tool),
            str(r.arch),
            _fmt_int(r.variant_bearing_reads),
            _fmt(r.md_concordant_pct, 2),
            _fmt(r.nm_concordant_pct, 2),
            _fmt(r.meth_pearson_r, 4),
            _fmt(r.meth_rmse, 4),
            _fmt_int(r.meth_n_cpg),
        ]
        for r in df.itertuples(index=False)
    ]
    return [
        "## Variant representation + methylation correlation",
        "",
        "Per-read variant-bearing count, MD/NM concordance vs golden (`—` when "
        "the golden carries no comparable tag — non-meth golden has no MD/NM), "
        "and per-CpG methylation-level Pearson r / RMSE (meth datasets only).",
        "",
        *_md_table(headers, aligners, rows),
        "",
    ]


def _class_section(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    headers = [
        "sample",
        "tool",
        "arch",
        "class",
        "confounded",
        "n_expected",
        "represented_%",
        "mean_mapq",
        "mean_as",
    ]
    aligners = ["---", "---", "---", "---", ":---:", "---:", "---:", "---:", "---:"]
    rows = [
        [
            str(r.sample),
            str(r.tool),
            str(r.arch),
            str(r.subclass),
            "yes" if bool(r.confounded) else "no",
            _fmt_int(r.n_expected),
            _fmt(r.represented_pct, 2),
            _fmt(r.mean_mapq, 2),
            _fmt(r.mean_as, 2),
        ]
        for r in df.itertuples(index=False)
    ]
    return [
        "## Per-class AS/MAPQ honesty",
        "",
        "The genomic-vs-collapsed headline. For each substitution class, does "
        "the aligner deflate `AS`/`MAPQ` on reads carrying a recoverable "
        "variant? **D3-genomic should deflate the `mirror` and `transversion` "
        "classes where D3-collapsed leaves them over-confident.** The "
        "conversion-direction class (C→T / G→A) is `confounded`: intrinsically "
        "inseparable from bisulfite conversion on a single read, in every mode "
        "— an honest finding, not a tool failure.",
        "",
        *_md_table(headers, aligners, rows),
        "",
    ]


def render_accuracy_markdown(*, db_path: Path, fg_labs_sha: str) -> str:
    """Return the full accuracy report markdown as a string."""
    headline = build_accuracy_table(db_path=db_path, fg_labs_sha=fg_labs_sha)
    classes = build_accuracy_class_table(db_path=db_path, fg_labs_sha=fg_labs_sha)

    lines = [f"# Truth-based alignment accuracy: `{fg_labs_sha}`", ""]
    if headline.empty:
        lines.append(f"_No accuracy rows ingested for `{fg_labs_sha}`._")
        return "\n".join(lines) + "\n"

    lines.extend(_placement_section(headline))
    lines.extend(_variant_section(headline))
    lines.extend(_class_section(classes))
    return "\n".join(lines).rstrip() + "\n"


def generate_accuracy(*, db_path: Path, fg_labs_sha: str, out_md: Path | None) -> str:
    """Render the accuracy report; write to `out_md` if given. Returns the markdown."""
    text = render_accuracy_markdown(db_path=db_path, fg_labs_sha=fg_labs_sha)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(text)
    return text


__all__ = [
    "EM_DASH",
    "build_accuracy_class_table",
    "build_accuracy_table",
    "generate_accuracy",
    "render_accuracy_markdown",
]
