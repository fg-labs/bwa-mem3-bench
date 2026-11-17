"""Regenerate the performance tables in fg-labs/bwa-mem3's README from a bless's arena.

After every bless, bwa-mem3's README gets a `docs(readme): update performance tables
for vX.Y.Z` PR (see docs/RELEASE.md). The README carries three tables built from the
arena: a headline table (the current release against `bwa`, upstream `bwa-mem2` and
`minibwa`) and one release-history table per arena host (Graviton4 m8g, AMD m8a).
Every cell is a ratio of median walls from ONE arena run, so a new bless replaces every
cell, not just the new release's row: the arena re-measures every release.

The cells:

- ``wall_s`` -- the arm's median wall, ``stock / --fast`` for releases with ``--fast``.
- ``vs bwa`` / ``vs bwa-mem2`` / ``vs minibwa`` -- the comparator's median wall over the
  row's, so ``>1`` means the row is faster than the comparator.
- ``vs prev`` -- the previous release's median wall over the row's (`prev / this`). The
  chain skips non-release ladder arms (the interim ``394f8f8`` build); a release without a
  ``--fast`` arm leaves the fast chain on the last release that had one. v0.2.1's stock
  predecessor is upstream bwa-mem2 (bwa-mem3's ancestor).

This is deliberately NOT `bench release-speedup`'s table (`build_release_speedup_table`):
that one keeps the interim build in its chain and computes ratios from unrounded medians.
Here every ratio is computed from the walls as printed (rounded to 2 decimals), so a
reader can recompute any cell from the table itself, and the published READMEs are
reproduced exactly. Both are ratios of medians, not `bench arena`'s paired estimate.

Release stars use the arena's `DenseSaPolicy`, checked against each arm's recorded
index family (``block``). Blocks distinguish dense from stock, not stride 2 from
stride 4: callers must independently verify the run's numeric stride and explicitly
declare it. The current checkout's stride is never assumed to describe a past run.

Every check runs before anything is written: a host whose arena is missing, narrowed
(a release the README already lists, a ``--fast`` wall it shows, or a comparator has
no measurement), has an arm missing rounds or a block that ran other than the
configured round count, has a declared stride other than 2, or a README whose tables
(including one split by a blank line), table headers, headline rows, or prose markers
(the round counts included) have drifted raises ``ValueError``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.arena_arms import DenseSaPolicy, index_family
from bwa_mem3_bench.arena_ladder import arena_releases
from bwa_mem3_bench.report.arena import (
    EM_DASH,
    arena_rows,
    fast_sibling_label,
    format_seconds,
    format_speedup,
    format_vs_prev,
    mode_chain,
)
from bwa_mem3_bench.report.results import ARENA_COMPARATORS, ARENA_DENSE_AFTER
from bwa_mem3_bench.report.results_data import RELEASE_LABEL, release_label_to_version
from bwa_mem3_bench.workflow_config import STOCK_SA_SHIFT

# README section heading (a regex) for each arena host's release-history table.
README_HOSTS: tuple[tuple[str, str], ...] = (
    ("m8g", r"\*\*Graviton4 \(m8g"),
    ("m8a", r"\*\*AMD \(m8a"),
)
# Headline-table row label for each arena host.
HEADLINE_ROWS: dict[str, str] = {"m8g": "ARM", "m8a": "x86"}
# Comparators each host must have measured. Upstream bwa-mem2 has no NEON build, so the
# README shows em-dashes for it on m8g; anywhere else a missing comparator is a failure.
REQUIRED_COMPARATORS: dict[str, tuple[str, ...]] = {
    "m8g": ("bwa", "minibwa"),
    "m8a": ("bwa", "bwa-mem2-upstream", "minibwa"),
}
# The only index the README's footnote and stars describe.
README_SA_STRIDE = 2
_CANDIDATE_LABEL = "fg-labs-default"
_CANDIDATE_BINARY = "bwa-mem2.fg-labs"
_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")
# The README's headline sentence names the current release; the previous version is
# read from it, so the prose markers can be moved forward without being told.
_CURRENT_RELEASE = re.compile(r"Wall-clock speedup of the current release \((v[\d.]+)\\\*\)")
_TABLE_HEADER = "> | release |"
_TABLE_ROW_PREFIX = "> |"
_ROW_NAME = re.compile(r"(?:\*\*)?(v\d+\.\d+\.\d+)(?:\*\*)?(?:\\\*)?")
# Both tables name the comparators in `ARENA_COMPARATORS` order, the order cells are written.
_VS_COLUMNS = " | ".join(f"vs {name}" for _, name in ARENA_COMPARATORS)
_HEADLINE_HEADER = f"| arch | wall_s | {_VS_COLUMNS} |"
_HOST_HEADER = f"> | release | wall_s | {_VS_COLUMNS} | vs prev |"
# A Markdown table separator: one `---` cell (optionally `:`-aligned) per column.
_SEPARATOR = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+$")
_COLLECT_HINT = "run `cli collect --fg-labs-sha {sha}` for the full bless arena first"


@dataclass(frozen=True)
class ReleaseRow:
    """One bwa-mem3 release's arena walls on one host."""

    version: str
    stock_wall: float
    fast_wall: float | None
    dense: bool


@dataclass(frozen=True)
class HostWalls:
    """Every median wall the README needs for one arena host."""

    comparators: dict[str, float | None]
    releases: list[ReleaseRow]
    # Measured rounds the candidate's block and every other block ran (checked against
    # the config); the README's "N rounds for v0.13.0 through ..., M for the rest".
    headline_rounds: int
    context_rounds: int


def _dense_policy(arena_sa_stride: int) -> DenseSaPolicy:
    if arena_sa_stride != README_SA_STRIDE:
        # The README's footnote and stars describe the stride-2 index; for any other
        # stride that prose is false, and rewriting it is a judgment call, not a template.
        raise ValueError(
            f"arena SA stride {arena_sa_stride} is not the stride-{README_SA_STRIDE} index "
            "the README's footnote describes, so update the README by hand"
        )
    return DenseSaPolicy(
        release_labels=tuple(label for label, _ in arena_releases()),
        min_label=ARENA_DENSE_AFTER,
        dense_shift=README_SA_STRIDE.bit_length() - 1,
        stock_shift=STOCK_SA_SHIFT,
    )


def _check_recorded_index(group: pd.DataFrame, arch: str, label: str, dense: bool) -> None:
    """Check the exact recorded index family; numeric stride is declared by the caller."""
    blocks = set(group.loc[group["label"] == label, "block"].dropna())
    binary = label if label in {"bwa", "minibwa"} else "bwa-mem2"
    expected = index_family(binary, dense=dense)
    if blocks != {expected}:
        raise ValueError(
            f"{arch}: arena arm {label!r} records index family {sorted(blocks)}, "
            f"expected {expected!r}; the arena's index config differs from this one"
        )


def _check_index_blocks(group: pd.DataFrame, arch: str, hint: str) -> None:
    """Refuse measured rows whose index family was never recorded."""
    if (group["wall_seconds"].notna() & group["block"].isna()).any():
        raise ValueError(f"{arch}: arena rows with a wall but no index block; {hint}")


def _check_round_inventory(
    group: pd.DataFrame, arch: str, hint: str, *, headline_rounds: int, context_rounds: int
) -> None:
    """Refuse an arm or block whose medians would not come from the configured rounds.

    Every arm of a block runs once per round, so each arm has the block's full round
    count, and that count is `headline_rounds` for the candidate's block and
    `context_rounds` for every other (``arena.reps`` / ``arena.context_reps``), the
    numbers the README states. An arm SKIPPED in every round (a release that predates
    ``--fast``) is no measurement at all, which the per-arm checks handle; one with a
    wall in only some rounds would silently publish a median over fewer rounds.
    """
    has_wall = group["wall_seconds"].notna().groupby(group["label"])
    partial = sorted(label for label, walls in has_wall if walls.any() and not walls.all())
    if partial:
        raise ValueError(f"{arch}: arena arm(s) {partial} have rounds without a wall; {hint}")
    counts = group.groupby(["block", "label"]).size()
    short = sorted(
        label
        for block, by_label in counts.groupby(level="block")
        for (_, label), n in by_label.items()
        if n < by_label.max()
    )
    if short:
        raise ValueError(f"{arch}: arena arm(s) {short} ran fewer rounds than their block; {hint}")
    for block, by_label in counts.groupby(level="block"):
        labels = {label for _, label in by_label.index}
        expected = headline_rounds if _CANDIDATE_LABEL in labels else context_rounds
        block_rows = group[group["block"] == block]
        for label, arm in block_rows.groupby("label"):
            if set(arm["rep"]) != set(range(1, expected + 1)) and len(arm) == expected:
                raise ValueError(f"{arch}: arena arm {label!r} has unexpected round identities")
        if int(by_label.max()) != expected:
            raise ValueError(
                f"{arch}: arena block {block!r} ran {int(by_label.max())} measured rounds, but "
                f"the config says {expected} (arena.reps / arena.context_reps); the README "
                "would misstate the round count"
            )


def _check_comparators(
    group: pd.DataFrame, arch: str, comparators: dict[str, float | None]
) -> None:
    """Check the index family of every measured comparator."""
    for label, value in comparators.items():
        if value is not None:
            _check_recorded_index(group, arch, label, False)


def collect_host_walls(  # noqa: PLR0913 -- keyword-only, mirrors the CLI's inputs
    *,
    db_path: Path,
    fg_labs_sha: str,
    version: str,
    arena_sa_stride: int,
    headline_rounds: int,
    context_rounds: int,
) -> dict[str, HostWalls]:
    """Median walls per README host from `fg_labs_sha`'s arena, candidate named `version`.

    Each median is rounded to the 2 decimals the README prints. Raises ``ValueError``
    when a host has no arena rows, an arm with a wall in only some rounds or fewer
    rounds than its block, no candidate stock or ``--fast`` wall, a missing
    required comparator, a release arm that ran without a wall, or a release arm whose
    recorded index family is not the one a stride-2 arena uses.

    :param arena_sa_stride: the suffix-array stride the arena's dense arms used; only 2,
        the stride the README describes, is accepted.
    :param headline_rounds: measured rounds the candidate's block must have run.
    :param context_rounds: measured rounds every other block must have run.
    """
    policy = _dense_policy(arena_sa_stride)
    hint = _COLLECT_HINT.format(sha=fg_labs_sha)
    raw = arena_rows(db_path, fg_labs_sha)
    hosts: dict[str, HostWalls] = {}
    for arch, _ in README_HOSTS:
        group = raw[raw["arch"] == arch]
        if group.empty:
            raise ValueError(f"no arena rows for {arch} at {fg_labs_sha}; {hint}")
        _check_index_blocks(group, arch, hint)
        medians = (
            group.dropna(subset=["wall_seconds"])
            .groupby(["label", "mode"])["wall_seconds"]
            .median()
            .round(2)
            .to_dict()
        )

        def wall(label: str, mode: str, medians: dict[object, float] = medians) -> float | None:
            value = medians.get((label, mode))
            return None if value is None else float(value)

        releases: list[ReleaseRow] = []
        for label in mode_chain(group, "default"):
            if label == _CANDIDATE_LABEL:
                name, binary = version, _CANDIDATE_BINARY
            elif RELEASE_LABEL.match(label):
                name, binary = release_label_to_version(label), f"bwa-mem3.{label}"
            else:
                continue  # an interim ladder build, not a release
            if name == version and label != _CANDIDATE_LABEL:
                raise ValueError(
                    f"--version {version} is already on the arena ladder as {label!r}; "
                    "pass the version the candidate shipped as"
                )
            stock = wall(label, "default")
            if stock is None:
                raise ValueError(f"{arch}: release arm {label!r} has no wall (SKIPPED?); {hint}")
            fast_label = fast_sibling_label(label)
            fast = wall(fast_label, "fast")
            if label == _CANDIDATE_LABEL and fast is None:
                raise ValueError(f"{arch}: candidate has no --fast wall; {hint}")
            dense = policy.uses_dense_sa(label, binary)
            for arm in (label, fast_label) if fast is not None else (label,):
                _check_recorded_index(group, arch, arm, dense)
            releases.append(ReleaseRow(name, stock, fast, dense))
        if not releases or releases[-1].version != version:
            raise ValueError(f"{arch}: no candidate ({_CANDIDATE_LABEL}) arena row; {hint}")
        # After the per-release checks, whose errors name the more specific cause.
        _check_round_inventory(
            group, arch, hint, headline_rounds=headline_rounds, context_rounds=context_rounds
        )
        comparators = {label: wall(label, "default") for label, _ in ARENA_COMPARATORS}
        _check_comparators(group, arch, comparators)
        missing = [c for c in REQUIRED_COMPARATORS[arch] if comparators[c] is None]
        if missing:
            raise ValueError(f"{arch}: comparator arm(s) {missing} have no wall; {hint}")
        hosts[arch] = HostWalls(
            comparators=comparators,
            releases=releases,
            headline_rounds=headline_rounds,
            context_rounds=context_rounds,
        )
    return hosts


def _pair(values: list[float | None], fmt: Callable[[float | None], str]) -> str:
    present = [fmt(v) for v in values if v is not None]
    return " / ".join(present) if present else EM_DASH


def _ratio(num: float | None, den: float | None) -> float | None:
    return None if num is None or den is None else num / den


def render_host_table(host: HostWalls) -> list[str]:
    """The README release-history table for one host, as ``> |`` blockquote lines."""
    comps = [host.comparators[label] for label, _ in ARENA_COMPARATORS]
    lines = [_HOST_HEADER, "> |---|" + "---:|" * (len(comps) + 2)]
    for (_, name), own in zip(ARENA_COMPARATORS, comps, strict=True):
        ratios = [_pair([_ratio(comp, own)], format_speedup) for comp in comps]
        if own is None:
            ratios = [EM_DASH] * len(comps)
        lines.append(f"> | {name} | {format_seconds(own)} | {' | '.join(ratios)} | {EM_DASH} |")

    prev_stock = host.comparators["bwa-mem2-upstream"]
    prev_fast: float | None = None
    for index, row in enumerate(host.releases):
        walls = [row.stock_wall, row.fast_wall]
        fast_prev = _ratio(prev_fast, row.fast_wall)
        cells = [
            _pair(walls, format_seconds),
            *(_pair([_ratio(comp, w) for w in walls], format_speedup) for comp in comps),
            _pair([_ratio(prev_stock, row.stock_wall), fast_prev], format_vs_prev),
        ]
        star = "\\*" if row.dense else ""
        if index == len(host.releases) - 1:
            name = f"**{row.version}**{star}"
            cells = [f"**{cell}**" for cell in cells]
        else:
            name = f"{row.version}{star}"
        lines.append(f"> | {name} | {' | '.join(cells)} |")
        prev_stock = row.stock_wall
        if row.fast_wall is not None:
            prev_fast = row.fast_wall
    return lines


def render_headline_row(label: str, host: HostWalls) -> str:
    """The README headline-table row for one host: the candidate vs each comparator."""
    current = host.releases[-1]
    walls = [current.stock_wall, current.fast_wall]
    vs = [
        _pair([_ratio(host.comparators[comp], w) for w in walls], format_speedup)
        for comp, _ in ARENA_COMPARATORS
    ]
    return f"| {label} | {_pair(walls, format_seconds)} | {' | '.join(vs)} |"


def _table_span(lines: list[str], heading: str, next_heading: str | None) -> tuple[int, int]:
    """``[first, end)`` of the release table under `heading`, before `next_heading`."""
    start = next((i for i, line in enumerate(lines) if re.search(heading, line)), None)
    if start is None:
        raise ValueError(f"README has no section matching {heading!r}")
    stop = len(lines)
    if next_heading is not None:
        following = (i for i in range(start + 1, len(lines)) if re.search(next_heading, lines[i]))
        found = next(following, None)
        if found is None:
            raise ValueError(f"README has no section matching {next_heading!r}")
        stop = found
    stop = next((i for i in range(start + 1, stop) if re.match(r"^#{1,6}\s", lines[i])), stop)
    for line in lines[start + 1 : stop]:
        if re.match(r"^\s*>\s*\|", line) and not line.startswith(_TABLE_ROW_PREFIX):
            raise ValueError(f"README section {heading!r}: unsupported table row prefix")
    first = next((i for i in range(start + 1, stop) if lines[i].startswith(_TABLE_HEADER)), None)
    if first is None:
        raise ValueError(f"README section {heading!r} has no release table")
    if lines[first] != _HOST_HEADER:
        # `_table_versions` reads `wall_s` by position, so the columns must be as written.
        raise ValueError(
            f"README section {heading!r}: release table header must be {_HOST_HEADER!r}, "
            f"found {lines[first]!r}"
        )
    if first + 1 >= stop or not lines[first + 1].startswith("> |---|"):
        raise ValueError(f"README section {heading!r}: release table header has no separator")
    end = first
    while end < stop and lines[end].startswith(_TABLE_ROW_PREFIX):
        end += 1
    # A blank line inside the table would end it early and leave the rest behind.
    if any(lines[i].startswith(_TABLE_ROW_PREFIX) for i in range(end, stop)):
        raise ValueError(f"README section {heading!r}: release table is split by a blank line")
    return first, end


def _table_versions(lines: list[str]) -> dict[str, bool]:
    """Each release row's version, mapped to whether its ``wall_s`` shows a ``--fast`` wall."""
    versions: dict[str, bool] = {}
    for line in lines[2:]:  # The validated header and separator are not data rows.
        cells = line.split("|")
        if len(cells) < 4:  # noqa: PLR2004 -- the blockquote marker, release and wall_s
            raise ValueError(f"README release row has no wall_s cell: {line!r}")
        name = cells[1].strip()
        if name in {display for _, display in ARENA_COMPARATORS}:
            continue
        if (m := _ROW_NAME.fullmatch(name)) is None:
            raise ValueError(f"README has an unreadable release row: {line!r}")
        version = m.group(1)
        if version in versions:
            raise ValueError(f"README has a duplicate release row for {version!r}")
        versions[version] = "/" in cells[2]
    return versions


def _headline_rows(lines: list[str]) -> dict[str, int]:
    """Line index of each headline row, found only inside the headline table.

    The header must name the comparators in `ARENA_COMPARATORS` order, which is the order
    `render_headline_row` writes them: a README whose columns were reordered or dropped
    would otherwise get every ratio under the wrong aligner.
    """
    headers = [i for i, line in enumerate(lines) if re.match(r"^\s*\|\s*arch\s*\|", line)]
    if len(headers) != 1 or lines[headers[0]] != _HEADLINE_HEADER:
        found = [lines[i] for i in headers]
        raise ValueError(
            f"README headline table header must appear exactly once, as {_HEADLINE_HEADER!r}; "
            f"found {found}"
        )
    header = headers[0]
    separator = lines[header + 1] if header + 1 < len(lines) else ""
    if not _SEPARATOR.match(separator) or separator.count("|") != _HEADLINE_HEADER.count("|"):
        # Without it the rows still sit under the header, but nothing renders as a table.
        raise ValueError(
            f"README headline table separator under the header is missing or malformed: "
            f"{separator!r}"
        )
    end = header + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    rows: dict[str, int] = {}
    for label in HEADLINE_ROWS.values():
        found_at = [
            i
            for i in range(header + 2, end)
            if re.match(rf"^\s*\|\s*{re.escape(label)}\s*\|", lines[i])
        ]
        if len(found_at) != 1:
            raise ValueError(
                f"README headline row '| {label} |' found {len(found_at)} times, not 1"
            )
        rows[label] = found_at[0]
    return rows


def _move_prose(text: str, previous: str, new: str, rounds: tuple[int, int]) -> str:
    """Move the README's current-release and stride-2 range markers to `new`.

    Each marker must be present exactly as the previous release left it; a README whose
    prose has drifted fails loudly rather than being half-updated. When `previous` is
    `new` (a rerun) nothing moves, but every marker is still checked. `rounds` is the
    (candidate block, other blocks) round count the arena ran, which the README states.
    """
    headline_rounds, context_rounds = rounds
    star = r"\*"
    edits = (
        (f"current release ({previous}{star})", f"current release ({new}{star})"),
        (f"{star} {previous}, like v0.13.0 through ", f"{star} {new}, like v0.13.0 through "),
        (
            f"through {previous} are all measured on it in the tables below, "
            f"so {previous}'s `vs prev`",
            f"through {new} are all measured on it in the tables below, so {new}'s `vs prev`",
        ),
        (
            f"{headline_rounds} rounds for v0.13.0 through {previous}, {context_rounds} for the",
            f"{headline_rounds} rounds for v0.13.0 through {new}, {context_rounds} for the",
        ),
        (f"{star} v0.13.0 through {previous} rows use", f"{star} v0.13.0 through {new} rows use"),
    )
    for old, replacement in edits:
        # A digit before the marker means it matched inside a longer number ("15 rounds"
        # for "5 rounds"), which is a different claim, not this marker.
        if len(re.findall(rf"(?<!\d){re.escape(old)}", text)) != 1 or text.count(old) != 1:
            raise ValueError(f"README prose marker not found exactly once: {old!r}")
        text = text.replace(old, replacement)
    # The footnote's "like v0.13.0 through <the release before previous>," now ends at previous.
    footnote = rf"(\\\* {re.escape(new)}, like v0\.13\.0 through )v[\d.]+,"
    if previous == new:
        count = len(re.findall(footnote, text))
    else:
        text, count = re.subn(footnote, rf"\g<1>{previous},", text)
    if count != 1:
        raise ValueError("README footnote 'like v0.13.0 through ...' not found")
    return text


def update_readme(readme: str, hosts: dict[str, HostWalls], version: str) -> str:
    """Return `readme` with every arena table and the prose markers moved to `version`.

    Re-running for the README's current version refreshes the tables and leaves the
    prose as it is, after checking its markers the same way.
    """
    if not _VERSION.fullmatch(version):
        raise ValueError(f"--version must look like v0.18.0, got {version!r}")
    match = _CURRENT_RELEASE.search(readme)
    if match is None:
        raise ValueError("README has no 'current release (vX.Y.Z\\*)' headline sentence")
    previous = match.group(1)
    lines = readme.split("\n")
    headings = [heading for _, heading in README_HOSTS]
    for index, (arch, heading) in enumerate(README_HOSTS):
        following = headings[index + 1] if index + 1 < len(headings) else None
        first, end = _table_span(lines, heading, following)
        table = render_host_table(hosts[arch])
        old, new = _table_versions(lines[first:end]), _table_versions(table)
        dropped = sorted(set(old) - set(new))
        if dropped:
            raise ValueError(
                f"{arch}: the README lists {dropped} but the arena has no measurement for "
                "them (a narrowed arena?); use the full bless arena"
            )
        lost_fast = sorted(v for v, has_fast in old.items() if has_fast and not new[v])
        if lost_fast:
            raise ValueError(
                f"{arch}: the README has --fast walls for {lost_fast} but the arena has no "
                "--fast measurement for them (a narrowed arena?); use the full bless arena"
            )
        lines = lines[:first] + table + lines[end:]
    headline = _headline_rows(lines)
    for arch, label in HEADLINE_ROWS.items():
        lines[headline[label]] = render_headline_row(label, hosts[arch])
    # Every host is collected against the same configured round counts.
    host = hosts[README_HOSTS[0][0]]
    rounds = (host.headline_rounds, host.context_rounds)
    return _move_prose("\n".join(lines), previous, version, rounds)


def generate_readme_tables(  # noqa: PLR0913 -- keyword-only, mirrors the CLI flags
    *,
    db_path: Path,
    fg_labs_sha: str,
    version: str,
    readme: Path,
    arena_sa_stride: int,
    headline_rounds: int,
    context_rounds: int,
    out: Path | None,
) -> Path:
    """Rewrite `readme`'s arena tables (to `out`, or in place). Returns the path written.

    Nothing is written unless every check passes.
    """
    hosts = collect_host_walls(
        db_path=db_path,
        fg_labs_sha=fg_labs_sha,
        version=version,
        arena_sa_stride=arena_sa_stride,
        headline_rounds=headline_rounds,
        context_rounds=context_rounds,
    )
    text = update_readme(readme.read_text(encoding="utf-8"), hosts, version)
    target = out if out is not None else readme
    target.write_text(text, encoding="utf-8")
    return target


__all__ = [
    "collect_host_walls",
    "generate_readme_tables",
    "render_headline_row",
    "render_host_table",
    "update_readme",
]
