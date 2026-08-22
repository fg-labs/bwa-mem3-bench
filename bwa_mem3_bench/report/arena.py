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
- ``vs_prior_release`` — for every bwa-mem3 arm (every historical release AND
  today's candidate, ``fg-labs-default``) except the oldest tracked release:
  this arm's median wall divided into its OWN immediately-prior release's
  (not a single fixed label) median wall. ``>1`` means this release is FASTER
  than the one immediately before it. A release with no measured predecessor
  (SKIPPED, or the oldest tracked release) carries no value.
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


def _arena_rows(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    return query_df(
        db_path,
        """
        SELECT id, arch, label, mode, wall_seconds, process_seconds
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
        # The row's DB insertion order (SQLite `id`, autoincrement) --
        # NOT a data value in its own right, used only to recover each
        # release's true chronological position. See the comment on `chain`
        # below for why this is the only order the report layer can use.
        min_id=("id", "min"),
    )

    def _fg_labs_default_wall(arch_group: pd.DataFrame) -> float | None:
        row = arch_group[
            (arch_group["label"] == "fg-labs-default") & (arch_group["mode"] == "default")
        ]
        return float(row["median_wall_s"].iloc[0]) if not row.empty else None

    # Keyed by `grouped`'s row index, not appended positionally: a plain list
    # is only correctly aligned with `grouped` while `groupby(..., as_index=
    # False)` happens to keep sorting its keys by `arch` (true today, but an
    # implicit invariant a future `sort=False` or an inserted `sort_values`
    # before this point would silently violate -- the table would still
    # render, just with every ratio on the wrong row).
    vs_prior_release: dict[int, float | None] = {}
    vs_fg_labs: dict[int, float | None] = {}
    for _arch, arch_group in grouped.groupby("arch"):
        # Chronological release chain for this arch: every historical
        # release's default-mode row, plus fg-labs-default's, ordered by
        # `min_id` -- the DB row insertion order. That order faithfully
        # reflects arena.smk's ARENA_RELEASES (oldest first) because
        # align_arena's shell loop writes each rep's arms in `_arena_arms()`
        # order and `ingest_arena` parses the TSV top to bottom, so it is
        # usable WITHOUT the report layer ever importing the Snakefile (see
        # the module docstring). fg-labs-default's own arm always runs last
        # in each rep, so it naturally sorts last here too -- one ordering
        # gives every historical release AND fg-labs-default its own true
        # predecessor, with no separate case needed for either.
        chain = arch_group[
            (arch_group["mode"] == "default")
            & ~arch_group["label"].isin(_NON_BWA_MEM3_LABELS | {"fg-labs-fast"})
        ].sort_values("min_id")
        predecessor_wall: dict[str, float | None] = {}
        last_measured_wall: float | None = None
        for chain_row in chain.itertuples(index=False):
            predecessor_wall[chain_row.label] = last_measured_wall
            # Only advance past a release that actually measured -- a SKIPPED
            # release (old binary, unsupported flag) has no wall time to
            # compare against, so the NEXT release's predecessor is the last
            # one that did measure, not a gap.
            if pd.notna(chain_row.median_wall_s):
                last_measured_wall = float(chain_row.median_wall_s)

        fg_labs_wall = _fg_labs_default_wall(arch_group)

        for row in arch_group.itertuples():
            wall_ok = pd.notna(row.median_wall_s)
            pred_wall = predecessor_wall.get(row.label)
            vs_prior_release[row.Index] = (
                pred_wall / row.median_wall_s if wall_ok and pred_wall is not None else None
            )
            vs_fg_labs[row.Index] = (
                fg_labs_wall / row.median_wall_s
                if row.label in _NON_BWA_MEM3_LABELS and fg_labs_wall is not None and wall_ok
                else None
            )

    grouped["vs_prior_release"] = pd.Series(vs_prior_release, dtype=float)
    grouped["vs_fg_labs"] = pd.Series(vs_fg_labs, dtype=float)
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
            "- **`vs_prior_release`** — every bwa-mem3 release's (including "
            "`fg-labs-default`'s) median wall divided into ITS OWN immediately "
            "prior release's — not one fixed release for every row. `>1` means "
            "this release is FASTER than the one immediately before it. Blank "
            "for the oldest tracked release (no predecessor), a SKIPPED release, "
            "`--fast` rows, and the three non-bwa-mem3 comparators.",
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
