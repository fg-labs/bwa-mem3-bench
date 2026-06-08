"""Generate mdbook-ready markdown from the divergence registry + benchmark DB.

Emits two blocks for the bwa-mem3 mdbook, designed to be spliced in between
marker comments (`<!-- NAME:start --> ... <!-- NAME:end -->`) the same way the
existing FG-MAIN-TABLE block is generated — so the published docs never go stale
(they did once; see fg-labs/bwa-mem3 PR #125):

  * a **divergence catalog** from `expected-divergences.yaml`, and
  * a **per-release concordance + supplementary table** from `benchmark.db`.
"""

from __future__ import annotations

import json
from pathlib import Path

from bwa_mem3_bench.registry import DivergenceEntry, load_registry
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE
from bwa_mem3_bench.storage.queries import query_df


def parse_releases(spec: str) -> list[tuple[str, str]]:
    """Parse a ``label=sha,label=sha`` spec into ordered ``(label, sha)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for raw in spec.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"release spec entry {chunk!r} is not 'label=sha'")
        label, sha = chunk.split("=", 1)
        pairs.append((label.strip(), sha.strip()))
    if not pairs:
        raise ValueError("no releases parsed from spec")
    return pairs


def render_divergence_catalog(entries: list[DivergenceEntry]) -> str:
    """Markdown table cataloguing the declared vs-upstream divergences."""
    if not entries:
        return "_No divergences declared._"
    rows = [
        (
            e.id,
            e.pr,
            e.affected,
            ", ".join(e.samples) if e.samples else "all",
            e.expected_drift_pct,
            e.summary,
        )
        for e in entries
    ]
    return md_table(
        ["id", "pr", "affected", "samples", "budget_%", "summary"],
        rows,
        float_fmt="{:.4f}",
    )


def render_release_table(db_path: Path, releases: list[tuple[str, str]]) -> str:
    """Per-(release, sample) vs-upstream concordance + supplementary divergence.

    Concordance is the min vs-baseline `concordance_pct` over that release's
    reps/x86-archs (deterministic per sample); supp counts come from `supp_json`.
    Releases with no data are listed as a note rather than silently dropped.
    """
    header = [
        "release",
        "sample",
        "concordance_%",
        "supp_query",
        "supp_baseline",
        "count_mismatch",
    ]
    rows: list[tuple[object, ...]] = []
    missing: list[str] = []
    for label, sha in releases:
        df = query_df(
            db_path,
            """
            SELECT t.sample, c.concordance_pct, c.supp_json
            FROM trials t
            JOIN comparisons c ON c.trial_id = t.id AND c.kind = ?
            WHERE t.fg_labs_sha = ?
            """,
            params=(VS_BASELINE, sha),
        )
        if df.empty:
            missing.append(label)
            continue
        # Per sample, keep the row that defines the min concordance so its
        # concordance_pct and supp_json come from the *same* comparison row
        # (a GROUP BY with a bare supp_json could mix rows).
        df = (
            df.sort_values("concordance_pct")
            .groupby("sample", as_index=False)
            .first()
            .sort_values("sample")
        )
        for sample, conc, supp_json in zip(
            df["sample"], df["concordance_pct"], df["supp_json"], strict=False
        ):
            supp = json.loads(supp_json) if isinstance(supp_json, str) and supp_json else {}
            rows.append(
                (
                    label,
                    sample,
                    conc,
                    int(supp.get("supp_query_total", 0)),
                    int(supp.get("supp_baseline_total", 0)),
                    int(supp.get("supp_count_mismatch_templates", 0)),
                )
            )
    out = md_table(header, rows, float_fmt="{:.4f}") if rows else "_No release data._"
    if missing:
        out += f"\n\n_No data for: {', '.join(missing)}._"
    return out


def inject_between_markers(text: str, name: str, content: str) -> str:
    """Replace the region between ``<!-- name:start -->`` and ``<!-- name:end -->``.

    The markers are preserved; only the content between them is replaced. Raises
    if either marker is missing so a typo can't silently no-op.
    """
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        raise ValueError(f"markers for {name!r} not found (need {start} ... {end})")
    return text[: si + len(start)] + "\n" + content + "\n" + text[ei:]


def generate_docs(
    *,
    db_path: Path,
    releases: list[tuple[str, str]],
    registry_path: Path,
    out_dir: Path,
) -> list[Path]:
    """Write the divergence catalog + per-release table markdown to ``out_dir``.

    Returns the written paths. These are intended to be spliced into the
    bwa-mem3 mdbook via [`inject_between_markers`].
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = render_divergence_catalog(load_registry(registry_path))
    table = render_release_table(db_path, releases)
    p_catalog = out_dir / "divergence-catalog.md"
    p_table = out_dir / "release-table.md"
    p_catalog.write_text(catalog + "\n")
    p_table.write_text(table + "\n")
    return [p_catalog, p_table]
