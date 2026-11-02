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
  today's candidate, in both modes) except the oldest tracked release: this
  arm's wall divided into its OWN immediately-prior release's (not a single
  fixed label). ``>1`` means this release is FASTER than the one immediately
  before it. A release with no measured predecessor (SKIPPED, or the oldest
  tracked release) carries no value.

  On a paired-round arena (rows carry a ``block``, see arena.smk's
  "Schedule"), both arms ran once per round in the same block, so the value is
  the MEDIAN OF PER-ROUND RATIOS: each round's ``prior_wall / wall``, with any
  round where either run was COLD (`_cold_mask`) dropped. ``prior_min`` /
  ``prior_max`` give the spread, ``prior_rounds`` the rounds used,
  ``vs_prior_process`` the same paired median on the aligner's own PROCESS()
  time, and ``prior_note`` says ``inconclusive`` when fewer than
  `MIN_CLEAN_ROUNDS` rounds survive or the spread exceeds `MAX_RATIO_SPREAD`.
  Only an adjacent pair (`ARENA_PAIRS` in arena.smk) is ``paired``; other
  same-block neighbours are ``same-round``, a block scheduled with fewer than
  `MIN_CLEAN_ROUNDS` rounds is ``context``, and a pair crossing index blocks,
  or a legacy arena (no ``block``), falls back to the ratio of the two medians
  as ``unpaired``. See `paired_ratio`.

Round 0 of every block is the schedule's discarded warmup and is excluded from
every statistic here.
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

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.arena_arms import SAM_CONTROL_SUFFIX
from bwa_mem3_bench.arena_ladder import arena_releases
from bwa_mem3_bench.storage.queries import query_df

EM_DASH = "—"

# Arm labels never compared against the prior release (they aren't a bwa-mem3
# build at all) or against fg-labs (they ARE the fg-labs build). Named directly
# rather than derived -- they are a stable public contract of the arena.tsv
# format, unlike the release labels (which DO come from arena.smk via
# `_release_chronology`, because that is where their chronological ORDER lives).
_NON_BWA_MEM3_LABELS = frozenset({"bwa", "bwa-mem2-upstream", "minibwa"})

# A run is COLD -- its page cache did not hold the index -- when its own
# index-load time exceeds this multiple of its block's median, or it read more
# than `COLD_IO_IN_MB` from disk. A warm human-index load reads ~0 MB and takes
# ~0.5 s; a cold one in the 0a65bec run took 2-11 s and read 3-13 GB.
COLD_INDEX_READ_FACTOR = 3.0
COLD_IO_IN_MB = 1024.0

# Below this many clean paired rounds, or above this relative spread
# ((max - min) / median of the per-round ratios), `vs_prior_release` is marked
# inconclusive rather than presented as a measurement.
MIN_CLEAN_ROUNDS = 3
MAX_RATIO_SPREAD = 0.05

# How `vs_prior_release` was estimated, shown beside it (`prior_note`):
NOTE_PAIRED = "paired"  # back-to-back in every round; lead-balanced median of per-round ratios
NOTE_SAME_ROUND = "same-round"  # same block and rounds, but not adjacent within a round
NOTE_CONTEXT = "context"  # a block scheduled with fewer than MIN_CLEAN_ROUNDS rounds
NOTE_INCONCLUSIVE = "inconclusive"  # too few clean rounds survived, or too wide a spread
NOTE_UNPAIRED = "unpaired"  # different blocks, or a legacy arena: ratio of medians

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


def arena_rows(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Every MEASURED arena run for the SHA, with a ``cold`` flag (`_cold_mask`).

    Warmup round 0 is excluded, and so are writer-control arms
    (`SAM_CONTROL_SUFFIX`), which are not releases. The one place that decides
    which rows count, shared by every arena report and the results snapshot.
    """
    rows = _all_arena_rows(db_path, fg_labs_sha)
    return rows[~rows["label"].str.endswith(SAM_CONTROL_SUFFIX)].reset_index(drop=True)


def _all_arena_rows(db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Every measured (round >= 1) arena run for the SHA, writer controls included."""
    rows = query_df(
        db_path,
        """
        SELECT arch, label, mode, rep, wall_seconds, process_seconds, cpu_time, max_rss_mb,
               block, position, index_read_seconds, io_in_mb, thp_fault_fallback
        FROM arena
        WHERE fg_labs_sha = ? AND rep >= 1
        ORDER BY arch, label, mode, rep
        """,
        params=(fg_labs_sha,),
    )
    return rows.assign(cold=_cold_mask(rows)) if not rows.empty else rows.assign(cold=False)


def _cold_mask(rows: pd.DataFrame) -> pd.Series:
    """True for every run whose page cache did not hold its index.

    Compared within (arch, block), since an index family's warm load time is a
    property of that family. Runs without the columns (legacy arenas, SKIPPED
    runs) are never cold.
    """
    block_median = rows.groupby(["arch", "block"], dropna=False)["index_read_seconds"].transform(
        "median"
    )
    slow_load = rows["index_read_seconds"] > COLD_INDEX_READ_FACTOR * block_median
    disk_read = rows["io_in_mb"] > COLD_IO_IN_MB
    return (slow_load | disk_read).fillna(False).astype(bool)


@dataclass(frozen=True)
class PairedRatio:
    """`prior / this` for one release pair, over the rounds both arms ran.

    :param ratio: the estimate (see `paired_ratio`), or the ratio of medians
        when `note` is `NOTE_UNPAIRED`.
    :param process_ratio: the same on PROCESS() seconds, when both report it.
    :param low: smallest per-round ratio used.
    :param high: largest per-round ratio used.
    :param rounds: rounds that contributed.
    :param note: one of the ``NOTE_*`` constants.
    """

    ratio: float | None
    process_ratio: float | None
    low: float | None
    high: float | None
    rounds: int
    note: str


def _lead_balanced_median(ratios: pd.Series, prior_leads: pd.Series) -> float:
    """Median of per-round ratios that weights the two lead orders equally.

    The schedule alternates which arm of an adjacent pair runs first, so with an
    odd round count one order has one more round than the other. A plain median
    then lands in the majority order's cluster and absorbs the full
    second-runner penalty the alternation exists to cancel. The geometric mean
    of the two orders' medians cancels it at any count; when only one order
    survived (cold rounds dropped), the plain median is all there is.
    """
    first = ratios[prior_leads]
    second = ratios[~prior_leads]
    if first.empty or second.empty:
        return float(ratios.median())
    return math.sqrt(float(first.median()) * float(second.median()))


def paired_ratio(prior: pd.DataFrame, this: pd.DataFrame) -> PairedRatio:
    """`prior / this` wall ratio for two arms' measured runs on one arch.

    Rows carry a ``cold`` flag (`arena_rows`). When both arms share one non-null
    ``block``, rows with the same ``rep`` ran in the same round; a round where
    either run is cold or SKIPPED is dropped as a whole. If the two were
    adjacent in every round the estimate is `_lead_balanced_median` and the note
    `NOTE_PAIRED`; otherwise a plain median noted `NOTE_SAME_ROUND`. A block
    scheduled with fewer than `MIN_CLEAN_ROUNDS` rounds is `NOTE_CONTEXT`, and
    too few clean rounds, too wide a spread, or an adjacent pair whose clean
    rounds all share one lead order `NOTE_INCONCLUSIVE`. Arms in
    different blocks, or a legacy arena, fall back to the ratio of medians
    (`NOTE_UNPAIRED`).
    """
    blocks = set(prior["block"].dropna()) | set(this["block"].dropna())
    same_block = len(blocks) == 1 and prior["block"].notna().all() and this["block"].notna().all()
    if not same_block:
        prior_wall = prior["wall_seconds"].median()
        this_wall = this["wall_seconds"].median()
        prior_proc = prior["process_seconds"].median()
        this_proc = this["process_seconds"].median()
        ratio = prior_wall / this_wall if pd.notna(prior_wall) and pd.notna(this_wall) else None
        process_ratio = (
            prior_proc / this_proc if pd.notna(prior_proc) and pd.notna(this_proc) else None
        )
        return PairedRatio(ratio, process_ratio, None, None, 0, NOTE_UNPAIRED)

    merged = prior.merge(this, on="rep", suffixes=("_prior", "_this"))
    clean = merged[
        ~merged["cold_prior"]
        & ~merged["cold_this"]
        & merged["wall_seconds_prior"].notna()
        & merged["wall_seconds_this"].notna()
    ]
    if clean.empty:
        return PairedRatio(None, None, None, None, 0, NOTE_INCONCLUSIVE)
    ratios = clean["wall_seconds_prior"] / clean["wall_seconds_this"]
    process = (clean["process_seconds_prior"] / clean["process_seconds_this"]).dropna()
    adjacent = bool(((clean["position_prior"] - clean["position_this"]).abs() == 1).all())
    prior_leads = clean["position_prior"] < clean["position_this"]
    if adjacent:
        ratio = _lead_balanced_median(ratios, prior_leads)
        process_ratio = (
            _lead_balanced_median(process, prior_leads[process.index])
            if not process.empty
            else None
        )
    else:
        ratio = float(ratios.median())
        process_ratio = float(process.median()) if not process.empty else None
    low, high = float(ratios.min()), float(ratios.max())
    # An adjacent pair is only lead-balanced if both orders survived; with one
    # order left the second-runner penalty is not cancelled.
    one_lead_order = adjacent and prior_leads.nunique() < 2  # noqa: PLR2004
    if len(merged) < MIN_CLEAN_ROUNDS:
        note = NOTE_CONTEXT
    elif (
        len(ratios) < MIN_CLEAN_ROUNDS or (high - low) / ratio > MAX_RATIO_SPREAD or one_lead_order
    ):
        note = NOTE_INCONCLUSIVE
    else:
        note = NOTE_PAIRED if adjacent else NOTE_SAME_ROUND
    return PairedRatio(
        ratio=ratio,
        process_ratio=process_ratio,
        low=low,
        high=high,
        rounds=len(ratios),
        note=note,
    )


def _release_chronology() -> dict[str, int]:
    """Chronological rank for every bwa-mem3 default-mode arm label.

    ``0`` is the oldest tracked release, increasing to today's candidate
    (``fg-labs-default``), which is always the newest. The order is
    ``ARENA_RELEASES`` (``workflow/rules/arena.smk``), read via
    ``arena_ladder.arena_releases()`` -- the SAME oldest-first ledger the base
    image builds every release binary from.

    Chronology MUST come from that canonical list, NOT from arena.tsv / DB row
    order: ``arena_arms.build_schedule`` deliberately SHUFFLES the run order
    every round (seeded by the SHA under measurement) to defeat host drift, so
    ``arena.id`` insertion order carries no chronology whatsoever. Ordering the
    release chain by ``min_id`` -- as this module once did -- divided each
    release by a RANDOM predecessor (e.g. c8a v090 reported as v030/v090, its
    shuffled neighbour, not v080/v090).
    """
    ranks = {label: rank for rank, (label, _sha) in enumerate(arena_releases())}
    ranks["fg-labs-default"] = len(ranks)  # today's candidate is newest of all
    return ranks


def _bwa_mem3_release_chain(arch_group: pd.DataFrame) -> pd.DataFrame:
    """Every default-mode bwa-mem3 release row, plus fg-labs-default's, oldest first.

    The rows of `arch_group` (one arch) whose labels `_mode_chain` returns, in
    its order -- the same chain `build_arena_table` walks, so the two can never
    define "release order" differently.
    """
    order = _mode_chain(arch_group, "default")
    chain = arch_group[(arch_group["mode"] == "default") & arch_group["label"].isin(order)]
    return chain.set_index("label").loc[order].reset_index()


def _fast_sibling_label(label: str) -> str:
    """The `-fast` arm's label for a given default-mode release label.

    Today's candidate is `fg-labs-default` / `fg-labs-fast` -- two
    independent labels from arena.smk's own arm list, not `<label>-fast` --
    every other release's fast arm IS `<label>-fast` on the same binary. Not
    derivable from one rule, so this names the exception explicitly.
    """
    return "fg-labs-fast" if label == "fg-labs-default" else f"{label}-fast"


# The `PairedRatio` fields `build_arena_table` publishes, in table order.
_PRIOR_COLUMNS = [
    "vs_prior_release",
    "vs_prior_process",
    "prior_min",
    "prior_max",
    "prior_rounds",
    "prior_note",
]

_ARENA_TABLE_COLUMNS = [
    "arch",
    "label",
    "mode",
    "median_wall_s",
    "median_process_s",
    "n_reps",
    "n_skipped",
    "cold_runs",
    "thp_fallback",
    "vs_prior_release",
    "vs_prior_process",
    "prior_min",
    "prior_max",
    "prior_rounds",
    "prior_note",
    "vs_fg_labs",
]


def _release_of(label: str) -> str:
    """The default-mode release label a (possibly ``-fast``) arm belongs to."""
    if label == "fg-labs-fast":
        return "fg-labs-default"
    return label.removesuffix("-fast")


def _mode_chain(arch_rows: pd.DataFrame, mode: str) -> list[str]:
    """Every bwa-mem3 arm label of `mode` on one arch, oldest release first.

    Ordered by `_release_chronology()` (the canonical ARENA_RELEASES order),
    never by DB insertion order, which the schedule randomizes.
    """
    chronology = _release_chronology()
    labels = {
        label
        for label in arch_rows.loc[arch_rows["mode"] == mode, "label"]
        if label not in _NON_BWA_MEM3_LABELS and _release_of(label) in chronology
    }
    return sorted(labels, key=lambda label: chronology[_release_of(label)])


def build_arena_table(*, db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """Return one row per (arch, label, mode) with medians and both speedup columns.

    Columns: `_ARENA_TABLE_COLUMNS`. ``cold_runs`` counts measured runs flagged
    by `_cold_mask`; ``thp_fallback`` is the median per-run
    ``thp_fault_fallback`` delta (NULL on legacy arenas). See the module
    docstring for the ``vs_prior_*`` / ``prior_*`` family.
    """
    return arena_table_from_rows(arena_rows(db_path, fg_labs_sha))


def arena_table_from_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """`build_arena_table` over rows already loaded by `arena_rows`."""
    if raw.empty:
        return pd.DataFrame(columns=_ARENA_TABLE_COLUMNS)

    grouped = raw.groupby(["arch", "label", "mode"], as_index=False).agg(
        median_wall_s=("wall_seconds", "median"),
        median_process_s=("process_seconds", "median"),
        n_reps=("wall_seconds", lambda s: int(s.notna().sum())),
        n_skipped=("wall_seconds", lambda s: int(s.isna().sum())),
        cold_runs=("cold", lambda s: int(s.sum())),
        thp_fallback=("thp_fault_fallback", "median"),
    )

    # Keyed by (arch, label, mode), not appended positionally, so a reordering
    # of `grouped` can never put a ratio on the wrong row.
    paired: dict[tuple[str, str, str], PairedRatio] = {}
    fg_labs_wall: dict[str, float] = {}
    for arch, arch_rows in raw.groupby("arch"):
        measured = grouped[grouped["arch"] == arch].set_index(["label", "mode"])
        for mode in ("default", "fast"):
            last_measured: str | None = None
            for label in _mode_chain(arch_rows, mode):
                if last_measured is not None:
                    paired[(str(arch), label, mode)] = paired_ratio(
                        arch_rows[
                            (arch_rows["label"] == last_measured) & (arch_rows["mode"] == mode)
                        ],
                        arch_rows[(arch_rows["label"] == label) & (arch_rows["mode"] == mode)],
                    )
                # Advance only past a release that measured: a SKIPPED release
                # has no wall time, so the next release's predecessor is the
                # last one that did measure, not a gap.
                if pd.notna(measured.loc[(label, mode), "median_wall_s"]):
                    last_measured = label
        if ("fg-labs-default", "default") in measured.index:
            wall = measured.loc[("fg-labs-default", "default"), "median_wall_s"]
            if pd.notna(wall):
                fg_labs_wall[str(arch)] = float(wall)

    ratios = pd.DataFrame(
        [
            {
                "arch": arch,
                "label": label,
                "mode": mode,
                "vs_prior_release": r.ratio,
                "vs_prior_process": r.process_ratio,
                "prior_min": r.low,
                "prior_max": r.high,
                "prior_rounds": r.rounds,
                "prior_note": r.note,
            }
            for (arch, label, mode), r in paired.items()
        ],
        columns=["arch", "label", "mode", *_PRIOR_COLUMNS],
    )
    grouped = grouped.merge(ratios, on=["arch", "label", "mode"], how="left")
    comparator = grouped["label"].isin(_NON_BWA_MEM3_LABELS)
    grouped["vs_fg_labs"] = grouped["arch"].map(fg_labs_wall).where(comparator) / grouped[
        "median_wall_s"
    ].where(comparator)
    for column in ("vs_prior_release", "vs_prior_process", "prior_min", "prior_max", "vs_fg_labs"):
        grouped[column] = pd.to_numeric(grouped[column], errors="coerce")
    return (
        grouped[_ARENA_TABLE_COLUMNS].sort_values(["arch", "label", "mode"]).reset_index(drop=True)
    )


def build_writer_control_table(*, db_path: Path, fg_labs_sha: str) -> pd.DataFrame:
    """What native BAM output saves over the SAM pipe, per writer-control arm.

    `arena.sam_writer_control` adds ``<label>-sam`` arms that run a bwa-mem3
    build through ``samtools view -u`` instead of its own ``--bam=0`` writer.
    Each is compared with its native sibling (``<label>``), run in the same
    block and rounds, as ``sam / native`` -- ``>1`` means native BAM is faster.
    Empty when the arena ran without the control.

    Columns: ``arch``, ``label``, ``native_saves``, ``process_saves``,
    ``rounds``, ``note``.
    """
    rows = _all_arena_rows(db_path, fg_labs_sha)
    records: list[dict[str, object]] = []
    for (arch, label), sam in rows.groupby(["arch", "label"]):
        if not str(label).endswith(SAM_CONTROL_SUFFIX):
            continue
        native_label = str(label).removesuffix(SAM_CONTROL_SUFFIX)
        native = rows[(rows["arch"] == arch) & (rows["label"] == native_label)]
        if native.empty:
            continue
        ratio = paired_ratio(sam, native)
        records.append(
            {
                "arch": arch,
                "label": native_label,
                "native_saves": ratio.ratio,
                "process_saves": ratio.process_ratio,
                "rounds": ratio.rounds,
                "note": ratio.note,
            }
        )
    return pd.DataFrame(
        records, columns=["arch", "label", "native_saves", "process_saves", "rounds", "note"]
    )


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
            "Every arm below ran on ONE fixed on-demand host per arch "
            "(see `workflow/rules/arena.smk`) — a release-over-release or "
            "vs-comparator wall-time claim here is measured under identical "
            "conditions, not from two separate spot runs' medians.",
            "",
            "- **`vs_prior_release`** — every bwa-mem3 release's (including "
            "today's candidate's, in both modes) wall divided into ITS OWN "
            "immediately prior release's. `>1` means this release is FASTER than "
            "the one before it. `prior_range` is the spread of the per-round "
            "ratios, `rounds` the clean rounds used (a round with a cold run is "
            "dropped) and `process` the same ratio on PROCESS() time. `note` says "
            "how it was estimated: `paired` (the two ran back-to-back every round; "
            "median of per-round ratios, balanced across which ran first), "
            "`same-round` (same rounds, not adjacent), `context` (a block "
            f"scheduled with fewer than {MIN_CLEAN_ROUNDS} rounds), `inconclusive` "
            f"(fewer than {MIN_CLEAN_ROUNDS} clean rounds, or over "
            f"{MAX_RATIO_SPREAD:.0%} spread), or `unpaired` (different index "
            "blocks, or an arena from before paired rounds: ratio of medians).",
            "- **`cold`** — measured runs whose index was not in page cache "
            "(slow index load or >1 GB read from disk); **`thp_fb`** — median "
            "huge-page fault fallbacks per run.",
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
        "cold",
        "thp_fb",
        "vs_prior_release",
        "prior_range",
        "rounds",
        "process",
        "note",
        "vs_fg_labs",
    ]
    text_columns = {"arch", "label", "mode", "note"}
    aligners = ["---" if header in text_columns else "---:" for header in headers]
    body_rows: list[str] = []
    for row in df.itertuples(index=False):
        prior_range = (
            f"{row.prior_min:.3f}–{row.prior_max:.3f}"
            if pd.notna(row.prior_min) and pd.notna(row.prior_max)
            else EM_DASH
        )
        cells = [
            str(row.arch),
            str(row.label),
            str(row.mode),
            _format_seconds(row.median_wall_s),
            _format_seconds(row.median_process_s),
            str(int(row.n_reps)),
            str(int(row.n_skipped)),
            str(int(row.cold_runs)),
            EM_DASH if pd.isna(row.thp_fallback) else f"{float(row.thp_fallback):.0f}",
            _format_vs_prev(row.vs_prior_release),
            prior_range,
            EM_DASH
            if pd.isna(row.prior_rounds) or row.prior_note == NOTE_UNPAIRED
            else str(int(row.prior_rounds)),
            _format_vs_prev(row.vs_prior_process),
            row.prior_note if isinstance(row.prior_note, str) else EM_DASH,
            _format_speedup(row.vs_fg_labs),
        ]
        body_rows.append("| " + " | ".join(cells) + " |")

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(aligners) + " |")
    lines.extend(body_rows)
    lines.append("")

    writers = build_writer_control_table(db_path=db_path, fg_labs_sha=fg_labs_sha)
    if not writers.empty:
        lines.extend(
            [
                "## Output writer control",
                "",
                "Each bwa-mem3 build run twice per round: its native `--bam=0` "
                "writer, and SAM text through `samtools view -u` (the `-sam` arm). "
                "`native_saves` is `sam_wall / native_wall`: `>1` means native BAM "
                "is faster.",
                "",
                "| arch | label | native_saves | process | rounds | note |",
                "| --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for row in writers.itertuples(index=False):
            lines.append(
                f"| {row.arch} | {row.label} | {_format_vs_prev(row.native_saves)} | "
                f"{_format_vs_prev(row.process_saves)} | {row.rounds} | {row.note} |"
            )
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

    It is a RATIO OF MEDIANS, not `build_arena_table`'s paired estimate: this
    table spans the whole ladder, including the bwa-mem2 seed, which no paired
    estimate covers, and stays comparable across arenas recorded before paired
    rounds. Quote `bench arena`'s ``vs_prior_release`` for a candidate-vs-prior
    claim; the two can differ in the last digit.
    """
    raw = arena_rows(db_path, fg_labs_sha)
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
            "release, on the same host: `prev_wall / this_wall` as a ratio of "
            "the two medians, `>1` faster, `<1` slower. For the paired "
            "per-round estimate of the candidate vs the prior release, see "
            "`bench arena`. The first release's stock predecessor is upstream "
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
    "COLD_INDEX_READ_FACTOR",
    "COLD_IO_IN_MB",
    "EM_DASH",
    "MAX_RATIO_SPREAD",
    "MIN_CLEAN_ROUNDS",
    "NOTE_CONTEXT",
    "NOTE_INCONCLUSIVE",
    "NOTE_PAIRED",
    "NOTE_SAME_ROUND",
    "NOTE_UNPAIRED",
    "PairedRatio",
    "arena_rows",
    "arena_table_from_rows",
    "build_arena_table",
    "build_release_speedup_table",
    "build_writer_control_table",
    "generate_arena",
    "generate_release_speedup",
    "paired_ratio",
    "render_arena_markdown",
    "render_release_speedup_markdown",
]
