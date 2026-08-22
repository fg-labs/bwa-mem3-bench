"""`bench arena` — release-history "arena" comparison table.

Reads the `arena` table (workflow/rules/arena.smk, one on-demand Batch job per
arch, every arm interleaved on the SAME host) and renders one markdown table
per arch with, for every arm:

- ``median_wall_s`` / ``median_process_s`` across measured reps (NULL reps —
  arena.smk's SKIPPED rows for an old binary that could not run at all — are
  excluded from the median, not treated as zero).
- ``n_reps`` / ``n_skipped`` — how many reps actually measured vs were
  recorded SKIPPED, so a median computed from e.g. 1-of-3 reps is visible
  rather than silently presented as equivalent to a full 3-of-3 measurement.
- ``vs_prior_release`` — for bwa-mem3 arms only: this release's median wall
  divided into the immediately-prior blessed release's (``ARENA_PRIOR_RELEASE_
  LABEL``'s) median wall. ``>1`` means this release is FASTER than the one
  before it.
- ``vs_fg_labs`` — for the three non-bwa-mem3 comparators (bwa, bwa-mem2-
  upstream, minibwa) only: today's candidate's (``fg-labs-default``) median
  wall divided by this arm's median wall, matching the sign convention
  `report/speedup.py`'s ``minibwa_speedup`` already uses (``>1`` means the
  comparator is FASTER than bwa-mem3).

The fgumi correctness spot-check (today's candidate vs the prior release,
default mode, boolean full-content identity) is NOT ingested into SQLite —
see arena.smk's "Correctness, narrowly scoped" for why — so it is not part of
this table. Its raw output lives at
``arena/<fg_labs_sha>/<arch>/fgumi-compare.txt`` in the synced mirror; this
report just points the reader there.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench.storage.queries import query_df

EM_DASH = "—"

# Arm labels never compared against the prior release (they aren't a bwa-mem3
# build at all) or against fg-labs (they ARE the fg-labs build). Kept here
# rather than imported from workflow/rules/arena.smk: the report layer reads
# only the DB, never the Snakefile, so the three non-bwa-mem3 labels are named
# directly -- they are a stable public contract of the arena.tsv format.
_NON_BWA_MEM3_LABELS = frozenset({"bwa", "bwa-mem2-upstream", "minibwa"})
_FG_LABS_LABELS = frozenset({"fg-labs-default", "fg-labs-fast"})


def _arena_rows(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    return query_df(
        db_path,
        """
        SELECT arch, label, mode, wall_seconds, process_seconds
        FROM arena
        WHERE fg_labs_sha = ?
        ORDER BY arch, label, mode
        """,
        params=(fg_labs_sha,),
    )


def build_arena_table(*, db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Return one row per (arch, label, mode) with medians and both speedup columns.

    Columns: ``arch``, ``label``, ``mode``, ``median_wall_s``,
    ``median_process_s``, ``n_reps``, ``n_skipped``, ``vs_prior_release``,
    ``vs_fg_labs``.
    """
    raw = _arena_rows(db_path, fg_labs_sha)
    if raw.empty:
        return raw.assign(
            median_wall_s=pd.Series(dtype=float),
            median_process_s=pd.Series(dtype=float),
            n_reps=pd.Series(dtype=int),
            n_skipped=pd.Series(dtype=int),
            vs_prior_release=pd.Series(dtype=float),
            vs_fg_labs=pd.Series(dtype=float),
        )

    grouped = raw.groupby(["arch", "label", "mode"], as_index=False).agg(
        median_wall_s=("wall_seconds", "median"),
        median_process_s=("process_seconds", "median"),
        n_reps=("wall_seconds", lambda s: int(s.notna().sum())),
        n_skipped=("wall_seconds", lambda s: int(s.isna().sum())),
    )

    # The prior release is whichever bwa-mem3 label is NOT fg-labs-* and sorts
    # last among the historical `vNNN` labels for that arch -- rather than
    # hardcoding "v090" here, which would silently go stale the next time
    # arena.smk's ARENA_RELEASES grows a new entry.
    def _prior_release_label(arch_group: pd.DataFrame) -> str | None:
        historical = arch_group[
            (arch_group["mode"] == "default")
            & ~arch_group["label"].isin(_NON_BWA_MEM3_LABELS | _FG_LABS_LABELS)
        ]
        if historical.empty:
            return None
        return str(historical["label"].sort_values().iloc[-1])

    def _fg_labs_default_wall(arch_group: pd.DataFrame) -> float | None:
        row = arch_group[
            (arch_group["label"] == "fg-labs-default") & (arch_group["mode"] == "default")
        ]
        return float(row["median_wall_s"].iloc[0]) if not row.empty else None

    vs_prior_release: list[float | None] = []
    vs_fg_labs: list[float | None] = []
    for _arch, arch_group in grouped.groupby("arch"):
        prior_label = _prior_release_label(arch_group)
        prior_wall = None
        if prior_label is not None:
            prior_row = arch_group[
                (arch_group["label"] == prior_label) & (arch_group["mode"] == "default")
            ]
            prior_wall = float(prior_row["median_wall_s"].iloc[0]) if not prior_row.empty else None
        fg_labs_wall = _fg_labs_default_wall(arch_group)

        for row in arch_group.itertuples(index=False):
            is_historical_default = (
                row.mode == "default"
                and row.label not in _NON_BWA_MEM3_LABELS
                and row.label not in _FG_LABS_LABELS
                and row.label != prior_label
            )
            wall_ok = pd.notna(row.median_wall_s)
            if is_historical_default and prior_wall and wall_ok:
                vs_prior_release.append(prior_wall / row.median_wall_s)
            else:
                vs_prior_release.append(None)

            if row.label in _NON_BWA_MEM3_LABELS and fg_labs_wall and wall_ok:
                vs_fg_labs.append(fg_labs_wall / row.median_wall_s)
            else:
                vs_fg_labs.append(None)

    grouped["vs_prior_release"] = vs_prior_release
    grouped["vs_fg_labs"] = vs_fg_labs
    return grouped.sort_values(["arch", "label", "mode"]).reset_index(drop=True)


def _format_seconds(value: float | None) -> str:
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.2f}"


def _format_speedup(value: float | None) -> str:
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.2f}x"


def render_arena_markdown(*, db_path: Path, fg_labs_sha: str) -> str:
    """Return the arena markdown report as a string."""
    df = build_arena_table(db_path=db_path, fg_labs_sha=fg_labs_sha)

    lines = [
        f"# Arena: release-history comparison for `{fg_labs_sha}`",
        "",
    ]

    if df.empty:
        lines.append(f"_No arena rows ingested for `{fg_labs_sha}`. Run `--target arena` first._")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "Every arm below ran INTERLEAVED on ONE fixed on-demand host per arch "
            "(see `workflow/rules/arena.smk`) — a release-over-release or "
            "vs-comparator wall-time claim here is measured under identical "
            "conditions, not from two separate spot runs' medians.",
            "",
            "- **`vs_prior_release`** — this bwa-mem3 release's median wall divided "
            "into the immediately prior blessed release's. `>1` means this release "
            "is FASTER than the one before it. Blank for the prior release's own "
            "row, `--fast` rows, and the three non-bwa-mem3 comparators.",
            "- **`vs_fg_labs`** — for `bwa` / `bwa-mem2-upstream` / `minibwa` only: "
            "today's candidate's median wall divided by this comparator's. `>1` "
            "means the comparator is FASTER than today's candidate.",
            "- A blank `median_wall_s` with `n_skipped > 0` means every rep of that "
            "arm failed — almost always an old binary that does not support a flag "
            'this rule assumes (see the rule\'s "Never hard-fail on an old binary").',
            "- The correctness spot-check (today's candidate vs the prior release, "
            "`fgumi compare bams`) is NOT in this table — see "
            "`arena/<sha>/<arch>/fgumi-compare.txt` in the synced mirror.",
            "",
        ]
    )

    headers = [
        "arch",
        "label",
        "mode",
        "median_wall_s",
        "median_process_s",
        "n_reps",
        "n_skipped",
        "vs_prior_release",
        "vs_fg_labs",
    ]
    aligners = ["---", "---", "---", "---:", "---:", "---:", "---:", "---:", "---:"]
    body_rows: list[str] = []
    for row in df.itertuples(index=False):
        cells = [
            str(row.arch),
            str(row.label),
            str(row.mode),
            _format_seconds(row.median_wall_s),
            _format_seconds(row.median_process_s),
            str(int(row.n_reps)),
            str(int(row.n_skipped)),
            _format_speedup(row.vs_prior_release),
            _format_speedup(row.vs_fg_labs),
        ]
        body_rows.append("| " + " | ".join(cells) + " |")

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(aligners) + " |")
    lines.extend(body_rows)
    lines.append("")
    return "\n".join(lines)


def generate_arena(*, db_path: Path, fg_labs_sha: str, out_md: Path | None) -> str:
    """Render the arena table; write to `out_md` if given. Returns the markdown."""
    text = render_arena_markdown(db_path=db_path, fg_labs_sha=fg_labs_sha)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(text)
    return text


__all__ = [
    "EM_DASH",
    "build_arena_table",
    "generate_arena",
    "render_arena_markdown",
]
