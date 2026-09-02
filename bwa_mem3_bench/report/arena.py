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

from bwa_mem3_bench.arena_ladder import arena_releases
from bwa_mem3_bench.storage.queries import query_df

EM_DASH = "—"

# Arm labels never compared against the prior release (they aren't a bwa-mem3
# build at all) or against fg-labs (they ARE the fg-labs build). Named directly
# rather than derived -- they are a stable public contract of the arena.tsv
# format, unlike the release labels (which DO come from arena.smk via
# `_release_chronology`, because that is where their chronological ORDER lives).
_NON_BWA_MEM3_LABELS = frozenset({"bwa", "bwa-mem2-upstream", "minibwa"})

# `build_release_speedup_table`'s documented column set -- shared by its two
# empty-result branches (no arena rows at all for this arch, and arena rows
# that are all comparators with no bwa-mem3 release among them) so a future
# column change can't update one branch's schema and silently miss the other.
_RELEASE_SPEEDUP_COLUMNS = [
    "label",
    "stock_median_wall_s",
    "stock_speedup",
    "fast_median_wall_s",
    "fast_speedup",
    "stock_vs_prev_speedup",
    "fast_vs_prev_speedup",
]


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


def _release_chronology() -> dict[str, int]:
    """Chronological rank for every bwa-mem3 default-mode arm label.

    ``0`` is the oldest tracked release, increasing to today's candidate
    (``fg-labs-default``), which is always the newest. The order is
    ``ARENA_RELEASES`` (``workflow/rules/arena.smk``), read via
    ``arena_ladder.arena_releases()`` -- the SAME oldest-first ledger the base
    image builds every release binary from.

    Chronology MUST come from that canonical list, NOT from arena.tsv / DB row
    order: ``arena_arms.front_load_fast_arms`` deliberately SHUFFLES the arm
    order every run (seeded by the SHA under measurement) to defeat a
    ``--fast``-vs-host-drift confound, so ``arena.id`` insertion order carries
    no chronology whatsoever. Ordering the release chain by ``min_id`` -- as
    this module did until that shuffle landed -- divided each release by a
    RANDOM predecessor (e.g. c8a v090 reported as v030/v090, its shuffled
    neighbour, not v080/v090).
    """
    ranks = {label: rank for rank, (label, _sha) in enumerate(arena_releases())}
    ranks["fg-labs-default"] = len(ranks)  # today's candidate is newest of all
    return ranks


def _bwa_mem3_release_chain(arch_group: pd.DataFrame) -> pd.DataFrame:
    """Every historical release's default-mode row, plus fg-labs-default's, in
    chronological order (oldest first).

    Ordered by `_release_chronology()` -- the canonical ARENA_RELEASES order --
    NOT by DB insertion order, which `front_load_fast_arms` shuffles per run
    (see that helper and `_release_chronology` for why the DB carries no
    chronology). Shared by `build_arena_table`'s `vs_prior_release` and
    `build_release_speedup_table`, so the two can never define "release order"
    differently.
    """
    chronology = _release_chronology()
    chain = arch_group[
        (arch_group["mode"] == "default")
        & ~arch_group["label"].isin(_NON_BWA_MEM3_LABELS | {"fg-labs-fast"})
    ].copy()
    # An unmapped label (not in ARENA_RELEASES and not fg-labs-default) sorts
    # last via NaN, rather than raising -- but that shouldn't happen: the
    # filter above leaves only default-mode bwa-mem3 arms, all of which are
    # blessed releases or the candidate. `test_arena_ladder` guards the ladder.
    chain["_chronological_rank"] = chain["label"].map(chronology)
    return chain.sort_values("_chronological_rank")


def _fast_sibling_label(label: str) -> str:
    """The `-fast` arm's label for a given default-mode release label.

    Today's candidate is `fg-labs-default` / `fg-labs-fast` -- two
    independent labels from arena.smk's own arm list, not `<label>-fast` --
    every other release's fast arm IS `<label>-fast` on the same binary. Not
    derivable from one rule, so this names the exception explicitly.
    """
    return "fg-labs-fast" if label == "fg-labs-default" else f"{label}-fast"


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
        chain = _bwa_mem3_release_chain(arch_group)
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


def _format_vs_prev(value: float | None) -> str:
    """Format the release-over-release speedup ratio (blank for None).

    Three decimals, since consecutive releases are often within ~1% and a
    2-decimal ``x`` would collapse many rows to the same value. ``>1`` means
    faster than the previous release shown, ``<1`` slower.
    """
    if value is None or pd.isna(value):
        return EM_DASH
    return f"{float(value):.3f}x"


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


def build_release_speedup_table(
    *, db_path: Path, fg_labs_sha: str, arch: str, baseline_label: str = "bwa"
) -> pd.DataFrame:
    """One row per bwa-mem3 release (chronological) for `arch`, feeding the
    README's release-history speedup table.

    Columns: ``label``, ``stock_median_wall_s``, ``stock_speedup``,
    ``fast_median_wall_s``, ``fast_speedup``, ``stock_vs_prev_speedup``,
    ``fast_vs_prev_speedup``. Speedup is `baseline_label`'s (default ``bwa``, the
    same comparator `_arena_arms` always runs -- see arena.smk) own median wall
    on the SAME arena host divided by this row's median wall -- ``>1`` means the
    release is FASTER than the baseline. A NaN ``fast_*`` pair means the release
    predates ``--fast`` or its `-fast` arm was recorded SKIPPED (see arena.smk's
    "Never hard-fail on an old binary"), not a missing measurement.

    ``*_vs_prev_speedup`` is the release-over-release speedup vs the PREVIOUS
    release in this chronological table -- ``prev_wall / this_wall``, so ``>1``
    is faster (shorter wall) and ``<1`` slower. "Previous" is the last release
    ABOVE this one that actually measured in that mode, so a SKIPPED release
    does not create a gap. The FIRST bwa-mem3 release's stock predecessor is
    upstream ``bwa-mem2`` -- bwa-mem3 is the bwa-mem2 successor, so its lineage
    "previous version" is bwa-mem2 (absent on ARM, where upstream has no build,
    so the first release reads blank there). ``--fast`` has no bwa-mem2
    counterpart, so the first release with a ``--fast`` arm still has no fast
    predecessor. Because the chain is chronological (`_bwa_mem3_release_chain`)
    and every arm ran on one fixed host, this is a real same-host delta.
    """
    raw = _arena_rows(db_path, fg_labs_sha)
    raw = raw[raw["arch"] == arch]
    if raw.empty:
        return pd.DataFrame(columns=_RELEASE_SPEEDUP_COLUMNS)

    grouped = raw.groupby(["label", "mode"], as_index=False).agg(
        median_wall_s=("wall_seconds", "median"),
    )

    baseline_row = grouped[(grouped["label"] == baseline_label) & (grouped["mode"] == "default")]
    baseline_wall = (
        float(baseline_row["median_wall_s"].iloc[0])
        if not baseline_row.empty and pd.notna(baseline_row["median_wall_s"].iloc[0])
        else None
    )

    def _wall(label: str, mode: str) -> float | None:
        row = grouped[(grouped["label"] == label) & (grouped["mode"] == mode)]
        if row.empty or pd.isna(row["median_wall_s"].iloc[0]):
            return None
        return float(row["median_wall_s"].iloc[0])

    def _speedup(wall: float | None) -> float | None:
        return baseline_wall / wall if wall is not None and baseline_wall is not None else None

    def _vs_prev(prev: float | None, cur: float | None) -> float | None:
        # prev / cur -- the release-over-release speedup ratio (>1 = faster).
        return prev / cur if prev is not None and cur is not None else None

    rows: list[dict[str, object]] = []
    # Seed the stock chain with upstream bwa-mem2: bwa-mem3 is the bwa-mem2
    # successor, so the FIRST bwa-mem3 release's "previous version" is bwa-mem2,
    # not a blank. Absent on ARM (upstream has no NEON build), where the first
    # release then has no predecessor and reads blank. `--fast` has no bwa-mem2
    # counterpart, so the fast chain is not seeded.
    prev_stock: float | None = _wall("bwa-mem2-upstream", "default")
    prev_fast: float | None = None
    for chain_row in _bwa_mem3_release_chain(grouped).itertuples(index=False):
        stock_wall = _wall(chain_row.label, "default")
        fast_wall = _wall(_fast_sibling_label(chain_row.label), "fast")
        rows.append(
            {
                "label": chain_row.label,
                "stock_median_wall_s": stock_wall,
                "stock_speedup": _speedup(stock_wall),
                "fast_median_wall_s": fast_wall,
                "fast_speedup": _speedup(fast_wall),
                "stock_vs_prev_speedup": _vs_prev(prev_stock, stock_wall),
                "fast_vs_prev_speedup": _vs_prev(prev_fast, fast_wall),
            }
        )
        # Advance "previous" only past a release that measured, so a SKIPPED
        # release does not become a phantom predecessor for the next row.
        if stock_wall is not None:
            prev_stock = stock_wall
        if fast_wall is not None:
            prev_fast = fast_wall
    if not rows:
        return pd.DataFrame(columns=_RELEASE_SPEEDUP_COLUMNS)
    return pd.DataFrame(rows)


def render_release_speedup_markdown(
    *, db_path: Path, fg_labs_sha: str, arch: str, baseline_label: str = "bwa"
) -> str:
    """Return the release-history speedup markdown report as a string."""
    df = build_release_speedup_table(
        db_path=db_path, fg_labs_sha=fg_labs_sha, arch=arch, baseline_label=baseline_label
    )

    lines = [
        f"# Release-history speedup vs `{baseline_label}`, {arch}, `{fg_labs_sha}`",
        "",
    ]

    if df.empty:
        lines.append(
            f"_No arena rows for arch `{arch}` and SHA `{fg_labs_sha}`. "
            "Run `--target arena` first._"
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "Every release below ran INTERLEAVED on ONE fixed on-demand host "
            "(see `workflow/rules/arena.smk`) — a speedup claim here is "
            "measured under identical conditions, not from two separate spot "
            "runs' medians.",
            "",
            f"- **speedup** — `{baseline_label}`'s own median wall on this "
            "SAME host, divided by the release's median wall. `>1` means the "
            f"release is FASTER than `{baseline_label}`.",
            "- **vs_prev** — release-over-release speedup vs the PREVIOUS "
            "release, on the same host: `prev_wall / this_wall`, `>1` faster, "
            "`<1` slower. The first release's stock predecessor is upstream "
            "`bwa-mem2` (bwa-mem3's ancestor); blank where absent (ARM) or for "
            "the first `--fast` arm.",
            "- A blank `--fast` pair means the release predates the flag or "
            "its arm was recorded SKIPPED, not a missing measurement.",
            "",
        ]
    )

    headers = [
        "release",
        "stock_wall_s",
        "stock_speedup",
        "fast_wall_s",
        "fast_speedup",
        "stock_vs_prev",
        "fast_vs_prev",
    ]
    aligners = ["---", "---:", "---:", "---:", "---:", "---:", "---:"]
    body_rows: list[str] = []
    for row in df.itertuples(index=False):
        cells = [
            str(row.label),
            _format_seconds(row.stock_median_wall_s),
            _format_speedup(row.stock_speedup),
            _format_seconds(row.fast_median_wall_s),
            _format_speedup(row.fast_speedup),
            _format_vs_prev(row.stock_vs_prev_speedup),
            _format_vs_prev(row.fast_vs_prev_speedup),
        ]
        body_rows.append("| " + " | ".join(cells) + " |")

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(aligners) + " |")
    lines.extend(body_rows)
    lines.append("")
    return "\n".join(lines)


def generate_release_speedup(
    *, db_path: Path, fg_labs_sha: str, arch: str, baseline_label: str = "bwa", out_md: Path | None
) -> str:
    """Render the release-history speedup table; write to `out_md` if given."""
    text = render_release_speedup_markdown(
        db_path=db_path, fg_labs_sha=fg_labs_sha, arch=arch, baseline_label=baseline_label
    )
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(text)
    return text


__all__ = [
    "EM_DASH",
    "build_arena_table",
    "build_release_speedup_table",
    "generate_arena",
    "generate_release_speedup",
    "render_arena_markdown",
    "render_release_speedup_markdown",
]
