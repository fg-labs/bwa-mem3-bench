"""Tests for `bench readme-tables` (`bwa_mem3_bench.report.readme_tables`)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from bwa_mem3_bench.cli import main
from bwa_mem3_bench.commands import _bless_release, bench
from bwa_mem3_bench.report.readme_tables import (
    collect_host_walls,
    generate_readme_tables,
    render_host_table,
    update_readme,
)
from bwa_mem3_bench.storage.sqlite import connect, upsert_arena, upsert_run

SHA = "feedface"
STRIDE = 2  # the shipped arena config (dense_sa_shift: 1)

# m8a walls as (label, mode, wall). m8g gets every wall doubled, so a table landing under
# the wrong host heading is detectable. m8g has no upstream bwa-mem2 (no NEON build).
# `394f8f8` is the interim ladder build, which the README neither shows nor uses as a
# predecessor. v0.7.0 predates `--fast`... except here it has one, to seed the fast chain;
# v0.2.1 has none. v130 is after `ARENA_DENSE_AFTER`, so it is starred.
_M8A: list[tuple[str, str, float]] = [
    ("bwa", "default", 200.0),
    ("minibwa", "default", 40.0),
    ("bwa-mem2-upstream", "default", 125.0),
    ("v021", "default", 100.0),
    ("v070", "default", 80.0),
    ("v070-fast", "fast", 40.0),
    ("394f8f8", "default", 70.0),
    ("394f8f8-fast", "fast", 35.0),
    ("v080", "default", 64.0),
    ("v080-fast", "fast", 32.0),
    ("v130", "default", 50.0),
    ("v130-fast", "fast", 25.0),
    ("fg-labs-default", "default", 40.0),
    ("fg-labs-fast", "fast", 20.0),
]
Row = tuple[str, str, str, float | None, int]  # arch, label, mode, wall, rep
# The shipped arena.reps / arena.context_reps: the candidate's (dense) block runs 5
# measured rounds, every other block 2. Each round repeats the arm's wall.
HEADLINE_ROUNDS, CONTEXT_ROUNDS = 5, 2
_DENSE = ("v130", "fg-labs-")


def _rounds(label: str) -> range:
    return range(1, (HEADLINE_ROUNDS if label.startswith(_DENSE) else CONTEXT_ROUNDS) + 1)


_ROWS: list[Row] = [
    *[("m8a", label, mode, wall, rep) for label, mode, wall in _M8A for rep in _rounds(label)],
    *[
        ("m8g", label, mode, wall * 2, rep)
        for label, mode, wall in _M8A
        if label != "bwa-mem2-upstream"
        for rep in _rounds(label)
    ],
]

_README = """\
## Performance

Wall-clock speedup of the current release (v0.9.0\\*) against `bwa`. Cells are `stock / --fast`.

| arch | wall_s | vs bwa | vs bwa-mem2 | vs minibwa |
|---|---:|---:|---:|---:|
| ARM | 1.00 / 1.00 | 1.00x / 1.00x | — | 1.00x / 1.00x |
| x86 | 1.00 / 1.00 | 1.00x / 1.00x | 1.00x / 1.00x | 1.00x / 1.00x |

\\* v0.9.0, like v0.13.0 through v0.8.0, uses the dense index. v0.13.0 through v0.9.0 are all \
measured on it in the tables below, so v0.9.0's `vs prev` is a like-for-like comparison.

> **Graviton4 (m8g, arm64/NEON)**
>
> | release | wall_s | vs bwa | vs bwa-mem2 | vs minibwa | vs prev |
> |---|---:|---:|---:|---:|---:|
> | bwa | 1.00 | 1.00x | — | 1.00x | — |
>
> **AMD (m8a, x86-64/AVX-512)**
>
> | release | wall_s | vs bwa | vs bwa-mem2 | vs minibwa | vs prev |
> |---|---:|---:|---:|---:|---:|
> | bwa | 1.00 | 1.00x | 1.00x | 1.00x | — |
>
> 5 rounds for v0.13.0 through v0.9.0, 2 for the rest. \\* v0.13.0 through v0.9.0 rows use it.
"""


def _block(label: str) -> str:
    """The index family the arena records for an arm (v0.13.0 on reads the dense index)."""
    if label in ("bwa", "minibwa"):
        return label
    dense = label.startswith(_DENSE)
    return "bwa-mem2-dense" if dense else "bwa-mem2-stock"


def _seed(db_path: Path, rows: list[Row], blocks: dict[str, str | None]) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=SHA, status="complete")
    for arch, label, mode, wall, rep in rows:
        upsert_arena(
            conn,
            fg_labs_sha=SHA,
            arch=arch,
            label=label,
            mode=mode,
            rep=rep,
            wall_seconds=wall,
            cpu_time=None,
            max_rss_mb=None,
            process_seconds=None,
            block=blocks.get(label, _block(label)),
        )


def _db(
    tmp_path: Path, rows: list[Row] = _ROWS, blocks: dict[str, str | None] | None = None
) -> Path:
    """Seed the arena; `blocks` overrides the index family recorded for named labels."""
    db_path = tmp_path / "benchmark.db"
    _seed(db_path, rows, blocks or {})
    return db_path


def _hosts(  # type: ignore[type-arg]
    db_path: Path,
    version: str = "v0.10.0",
    stride: int = STRIDE,
    rounds: tuple[int, int] = (HEADLINE_ROUNDS, CONTEXT_ROUNDS),
) -> dict:
    return collect_host_walls(
        db_path=db_path,
        fg_labs_sha=SHA,
        version=version,
        arena_sa_stride=stride,
        headline_rounds=rounds[0],
        context_rounds=rounds[1],
    )


def _generate(
    db_path: Path, readme: Path, version: str = "v0.10.0", out: Path | None = None
) -> Path:
    return generate_readme_tables(
        db_path=db_path,
        fg_labs_sha=SHA,
        version=version,
        readme=readme,
        arena_sa_stride=STRIDE,
        headline_rounds=HEADLINE_ROUNDS,
        context_rounds=CONTEXT_ROUNDS,
        out=out,
    )


def test_host_table_rows_and_ratios(tmp_path: Path) -> None:
    assert render_host_table(_hosts(_db(tmp_path))["m8a"]) == [
        "> | release | wall_s | vs bwa | vs bwa-mem2 | vs minibwa | vs prev |",
        "> |---|---:|---:|---:|---:|---:|",
        "> | bwa | 200.00 | 1.00x | 0.62x | 0.20x | — |",
        "> | bwa-mem2 | 125.00 | 1.60x | 1.00x | 0.32x | — |",
        "> | minibwa | 40.00 | 5.00x | 3.12x | 1.00x | — |",
        # The first release's stock predecessor is upstream bwa-mem2: 125 / 100.
        "> | v0.2.1 | 100.00 | 2.00x | 1.25x | 0.40x | 1.250x |",
        # No fast predecessor yet, so only the stock vs prev.
        "> | v0.7.0 | 80.00 / 40.00 | 2.50x / 5.00x | 1.56x / 3.12x | 0.50x / 1.00x | 1.250x |",
        # The interim 394f8f8 build is skipped: v0.8.0's predecessor is v0.7.0 (80 / 64).
        "> | v0.8.0 | 64.00 / 32.00 | 3.12x / 6.25x | 1.95x / 3.91x | 0.62x / 1.25x"
        " | 1.250x / 1.250x |",
        "> | v0.13.0\\* | 50.00 / 25.00 | 4.00x / 8.00x | 2.50x / 5.00x | 0.80x / 1.60x"
        " | 1.280x / 1.280x |",
        "> | **v0.10.0**\\* | **40.00 / 20.00** | **5.00x / 10.00x** | **3.12x / 6.25x**"
        " | **1.00x / 2.00x** | **1.250x / 1.250x** |",
    ]


def test_host_without_bwa_mem2_leaves_its_cells_blank(tmp_path: Path) -> None:
    table = render_host_table(_hosts(_db(tmp_path))["m8g"])
    assert table[3] == "> | bwa-mem2 | — | — | — | — | — |"
    # No bwa-mem2 seed, so the first release has no predecessor.
    assert table[5] == "> | v0.2.1 | 200.00 | 2.00x | — | 0.40x | — |"


def test_walls_are_rep_medians_and_ratios_use_the_printed_walls(tmp_path: Path) -> None:
    # Candidate stock rounds 1.0 / 2.003 / 2.004 / 9.0 / 9.5: median 2.004, printed 2.00. The
    # predecessor (v130) is 2.50, so vs prev is 2.50 / 2.00 = 1.250x as printed, where the
    # unrounded median would give 2.50 / 2.004 = 1.248x.
    dense = ("fg-labs-default", "fg-labs-fast", "v130", "v130-fast")
    rows = [r for r in _ROWS if r[1] not in dense]
    for arch in ("m8a", "m8g"):
        for rep in range(1, HEADLINE_ROUNDS + 1):
            rows += [
                (arch, "v130", "default", 2.5, rep),
                (arch, "v130-fast", "fast", 25.0, rep),
                (arch, "fg-labs-fast", "fast", 20.0, rep),
            ]
        rows += [
            (arch, "fg-labs-default", "default", w, rep)
            for rep, w in enumerate((9.0, 2.003, 1.0, 9.5, 2.004), 1)
        ]
    table = render_host_table(_hosts(_db(tmp_path, rows))["m8a"])
    assert table[-1].startswith("> | **v0.10.0**\\* | **2.00 / 20.00** |")
    assert table[-1].endswith("| **1.250x / 1.250x** |")


def test_update_readme_rewrites_tables_and_moves_prose(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    _generate(_db(tmp_path), readme)
    text = readme.read_text(encoding="utf-8")
    assert "> | bwa | 1.00 |" not in text
    assert "current release (v0.10.0\\*)" in text
    assert "\\* v0.10.0, like v0.13.0 through v0.9.0, uses" in text
    assert "so v0.10.0's `vs prev`" in text
    assert "5 rounds for v0.13.0 through v0.10.0," in text
    assert "\\* v0.13.0 through v0.10.0 rows use" in text
    assert "| ARM | 80.00 / 40.00 | 5.00x / 10.00x | — | 1.00x / 2.00x |" in text
    assert "| x86 | 40.00 / 20.00 | 5.00x / 10.00x | 3.12x / 6.25x | 1.00x / 2.00x |" in text
    # Each host's table lands under its own heading: m8g's candidate wall is doubled.
    m8g, m8a = text.split("**AMD (m8a")
    assert "> | **v0.10.0**\\* | **80.00 / 40.00** |" in m8g
    assert "> | **v0.10.0**\\* | **40.00 / 20.00** |" in m8a
    assert text.count("> |---|---:|---:|---:|---:|---:|") == 2  # noqa: PLR2004


def test_out_writes_elsewhere_and_leaves_readme_alone(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    out = tmp_path / "preview.md"
    assert _generate(_db(tmp_path), readme, out=out) == out
    assert readme.read_text(encoding="utf-8") == _README
    assert "current release (v0.10.0\\*)" in out.read_text(encoding="utf-8")


def test_rerun_for_the_same_version_refreshes_tables_only(tmp_path: Path) -> None:
    text = update_readme(_README, _hosts(_db(tmp_path), version="v0.9.0"), "v0.9.0")
    assert "> | bwa | 1.00 |" not in text
    assert "> | **v0.9.0**\\* | **40.00 / 20.00** |" in text
    assert "current release (v0.9.0\\*)" in text
    assert "like v0.13.0 through v0.8.0," in text


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        (lambda r: r[0] == "m8g", "no arena rows for m8g"),
        (lambda r: r[1] == "minibwa" and r[0] == "m8a", r"comparator arm\(s\) \['minibwa'\]"),
        (lambda r: r[1] == "fg-labs-fast", "candidate has no --fast wall"),
        (lambda r: r[1] == "fg-labs-default", "no candidate"),
    ],
)
def test_incomplete_arena_fails_without_writing(tmp_path: Path, drop, message: str) -> None:  # type: ignore[no-untyped-def]
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _generate(_db(tmp_path, [r for r in _ROWS if not drop(r)]), readme)
    assert readme.read_text(encoding="utf-8") == _README


def test_skipped_release_arm_fails(tmp_path: Path) -> None:
    rows = [*_ROWS, *[("m8a", "v030", "default", None, rep) for rep in _rounds("v030")]]
    with pytest.raises(ValueError, match="'v030' has no wall"):
        _hosts(_db(tmp_path, rows))


def test_arm_skipped_in_every_round_is_not_a_partial_round(tmp_path: Path) -> None:
    # v0.2.1 predates --fast: its fast arm is SKIPPED in every round, which is no measurement.
    rows = [*_ROWS, *[("m8a", "v021-fast", "fast", None, rep) for rep in _rounds("v021")]]
    assert _hosts(_db(tmp_path, rows))["m8a"].releases[0].fast_wall is None


@pytest.mark.parametrize("arch", ["m8a", "m8g"])
@pytest.mark.parametrize("label", ["minibwa", "v080", "fg-labs-default", "394f8f8"])
def test_measured_row_without_index_block_fails_without_writing(
    tmp_path: Path, arch: str, label: str
) -> None:
    db_path = _db(tmp_path)
    # Add a legacy rep alongside a complete modern inventory. Its low wall must not
    # change the median while NULL-block rows disappear from the round-count checks.
    with connect(db_path) as conn:
        upsert_arena(
            conn,
            fg_labs_sha=SHA,
            arch=arch,
            label=label,
            mode="default",
            rep=99,
            wall_seconds=1.0,
            cpu_time=None,
            max_rss_mb=None,
            process_seconds=None,
            block=None,
        )
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    out = tmp_path / "preview.md"
    out.write_text("existing preview\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"{arch}: arena rows with a wall but no index block"):
        _generate(db_path, readme, out=out)
    assert readme.read_text(encoding="utf-8") == _README
    assert out.read_text(encoding="utf-8") == "existing preview\n"


def test_skipped_row_without_index_block_is_allowed(tmp_path: Path) -> None:
    rows = [*_ROWS, *[("m8a", "v021-fast", "fast", None, rep) for rep in _rounds("v021")]]
    assert (
        _hosts(_db(tmp_path, rows, blocks={"v021-fast": None}))["m8a"].releases[0].fast_wall is None
    )


def test_arm_missing_a_rounds_wall_fails(tmp_path: Path) -> None:
    # One of the candidate's rounds has no wall: a median of the rest would be published.
    rows = [
        r if (r[0], r[1], r[4]) != ("m8a", "fg-labs-default", 3) else (*r[:3], None, 3)
        for r in _ROWS
    ]
    with pytest.raises(ValueError, match=r"m8a: arena arm\(s\) \['fg-labs-default'\] have "):
        _hosts(_db(tmp_path, rows))


def test_arm_missing_a_round_fails(tmp_path: Path) -> None:
    # The candidate ran 4 of its block's 5 rounds: its median would come from fewer rounds.
    rows = [r for r in _ROWS if (r[0], r[1], r[4]) != ("m8g", "fg-labs-default", 3)]
    with pytest.raises(ValueError, match=r"m8g: arena arm\(s\) \['fg-labs-default'\] ran "):
        _hosts(_db(tmp_path, rows))


@pytest.mark.parametrize(
    ("rounds", "message"),
    [
        (
            (4, CONTEXT_ROUNDS),
            "block 'bwa-mem2-dense' ran 5 measured rounds, but the config says 4",
        ),
        ((HEADLINE_ROUNDS, 3), "ran 2 measured rounds, but the config says 3"),
    ],
)
def test_block_round_count_other_than_the_config_fails(
    tmp_path: Path, rounds: tuple[int, int], message: str
) -> None:
    # A whole block run short passes the per-arm checks; the config's count catches it.
    with pytest.raises(ValueError, match=re.escape(message)):
        _hosts(_db(tmp_path), rounds=rounds)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (", 2 for the rest.", ", 3 for the rest."),
        # "15 rounds" ends in the expected "5 rounds": the whole number must match.
        ("> 5 rounds for v0.13.0", "> 15 rounds for v0.13.0"),
    ],
)
def test_readme_round_count_that_differs_from_the_arena_fails(
    tmp_path: Path, old: str, new: str
) -> None:
    assert _README.count(old) == 1
    readme = _README.replace(old, new)
    with pytest.raises(ValueError, match="prose marker"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


def test_narrowed_arena_fails(tmp_path: Path) -> None:
    # The README already lists v0.7.0; an arena that did not run it must not delete the row.
    readme = _README.replace(
        "> | bwa | 1.00 | 1.00x | — | 1.00x | — |",
        "> | v0.7.0 | 1.00 | 1.00x | — | 1.00x | — |",
    )
    rows = [r for r in _ROWS if not r[1].startswith("v070")]
    with pytest.raises(ValueError, match=r"m8g: the README lists \['v0.7.0'\]"):
        update_readme(readme, _hosts(_db(tmp_path, rows)), "v0.10.0")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (("5 rounds for v0.13.0 through v0.9.0,", "five rounds,"), "prose marker"),
        (("| x86 |", "| x86-64 |"), r"'\| x86 \|' found 0 times"),
        (("| ARM | 1.00", "| **ARM** | 1.00"), r"'\| ARM \|' found 0 times"),
        (("**AMD (m8a", "**Intel (m7i"), "no section matching"),
        (("current release (v0.9.0\\*)", "latest release"), "headline sentence"),
    ],
)
def test_drifted_readme_fails(tmp_path: Path, edit: tuple[str, str], message: str) -> None:
    old, new = edit
    assert old in _README
    with pytest.raises(ValueError, match=message):
        update_readme(_README.replace(old, new, 1), _hosts(_db(tmp_path)), "v0.10.0")


def test_missing_m8g_table_does_not_overwrite_m8a(tmp_path: Path) -> None:
    # m8g's table header is gone: the search must stop at the m8a heading, not take m8a's.
    readme = _README.replace(
        "> | release | wall_s | vs bwa | vs bwa-mem2 | vs minibwa | vs prev |\n", "", 1
    )
    with pytest.raises(ValueError, match="Graviton4.*has no release table"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


@pytest.mark.parametrize("version", ["0.10.0", "v0.10", "v0.10.0-rc1", "v0.10.0\n"])
def test_version_must_be_vxyz(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="--version must look like"):
        update_readme(_README, _hosts(_db(tmp_path), version=version), version)


def test_version_already_on_the_ladder_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="already on the arena ladder as 'v130'"):
        _hosts(_db(tmp_path), version="v0.13.0")


@pytest.mark.parametrize("stride", [1, 3, 4, 8])
def test_stride_other_than_the_readmes_stride_2_is_refused(tmp_path: Path, stride: int) -> None:
    # The README's footnote describes the stride-2 index; stride 4 would still star rows.
    with pytest.raises(ValueError, match=f"stride {stride} is not the stride-2 index"):
        _hosts(_db(tmp_path), stride=stride)


@pytest.mark.parametrize(
    ("blocks", "message"),
    [
        # A stock-index arena (dense_sa_shift: 3) checked under today's stride-2 config.
        (
            {"v130": "bwa-mem2-stock", "v130-fast": "bwa-mem2-stock"},
            "records index family",
        ),
        (
            {"fg-labs-fast": "bwa-mem2-stock"},
            "records index family",
        ),
        (
            {"v080": "bwa-mem2-dense"},
            "records index family",
        ),
        # An arena from before paired rounds records no index at all.
        ({"v021": None}, "arena rows with a wall but no index block"),
    ],
)
def test_arena_whose_recorded_index_disagrees_is_refused(
    tmp_path: Path, blocks: dict[str, str | None], message: str
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(message)):
        generate_readme_tables(
            db_path=_db(tmp_path, blocks=blocks),
            fg_labs_sha=SHA,
            version="v0.10.0",
            readme=readme,
            arena_sa_stride=STRIDE,
            headline_rounds=HEADLINE_ROUNDS,
            context_rounds=CONTEXT_ROUNDS,
            out=None,
        )
    assert readme.read_text(encoding="utf-8") == _README


def test_arena_missing_a_fast_wall_the_readme_has_fails(tmp_path: Path) -> None:
    # The README has v0.8.0's --fast wall; an arena without v080-fast must not drop it.
    readme = _README.replace(
        "> | bwa | 1.00 | 1.00x | — | 1.00x | — |",
        "> | v0.8.0 | 1.00 / 1.00 | 1.00x / 1.00x | — | 1.00x / 1.00x | — |",
    )
    rows = [r for r in _ROWS if r[1] != "v080-fast"]
    with pytest.raises(ValueError, match=r"m8g: the README has --fast walls for \['v0.8.0'\]"):
        update_readme(readme, _hosts(_db(tmp_path, rows)), "v0.10.0")


@pytest.mark.parametrize("host", ["Graviton4", "AMD"])
@pytest.mark.parametrize("fast_first", [True, False])
def test_duplicate_release_rows_fail_without_writing(
    tmp_path: Path, host: str, fast_first: bool
) -> None:
    stock = "> | v0.8.0 | 1.00 | 1.00x | 1.00x | 1.00x | — |"
    fast = "> | **v0.8.0**\\* | 1.00 / 1.00 | 1.00x | 1.00x | 1.00x | — |"
    before, after = _README.split(f"> **{host}", 1)
    bwa = next(line for line in after.splitlines() if line.startswith("> | bwa |"))
    after = after.replace(bwa, "\n".join([fast, stock] if fast_first else [stock, fast]), 1)
    original = before + f"> **{host}" + after
    readme = tmp_path / "README.md"
    readme.write_text(original, encoding="utf-8")
    rows = [r for r in _ROWS if r[1] != "v080-fast"]
    with pytest.raises(ValueError, match=r"duplicate release row.*v0\.8\.0"):
        _generate(_db(tmp_path, rows), readme)
    assert readme.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("host", ["Graviton4", "AMD"])
@pytest.mark.parametrize(
    "name", ["[v0.7.0](https://example.com/release)", "unknown", "v0.7.0-extra"]
)
def test_unreadable_release_row_fails_without_writing(tmp_path: Path, host: str, name: str) -> None:
    before, after = _README.split(f"> **{host}", 1)
    after = after.replace("> | bwa |", f"> | {name} |", 1)
    original = before + f"> **{host}" + after
    readme = tmp_path / "README.md"
    readme.write_text(original, encoding="utf-8")
    out = tmp_path / "preview.md"
    rows = [r for r in _ROWS if not r[1].startswith("v070")]
    with pytest.raises(ValueError, match="unreadable release row"):
        _generate(_db(tmp_path, rows), readme, out=out)
    assert readme.read_text(encoding="utf-8") == original
    assert not out.exists()


def test_release_the_readme_shows_without_fast_may_stay_without_it(tmp_path: Path) -> None:
    # v0.2.1 never had --fast, so its stock-only README row is not a dropped measurement.
    readme = _README.replace(
        "> | bwa | 1.00 | 1.00x | — | 1.00x | — |",
        "> | v0.2.1 | 1.00 | 1.00x | — | 1.00x | — |",
    )
    text = update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")
    assert "> | v0.2.1 | 200.00 |" in text


@pytest.mark.parametrize(
    "header",
    [
        "| arch | wall_s | vs minibwa | vs bwa-mem2 | vs bwa |",  # swapped comparators
        "| arch | wall_s | vs bwa | vs minibwa |",  # a comparator column dropped
    ],
)
def test_headline_header_that_differs_fails(tmp_path: Path, header: str) -> None:
    readme = _README.replace("| arch | wall_s | vs bwa | vs bwa-mem2 | vs minibwa |", header)
    with pytest.raises(ValueError, match="headline table header"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


@pytest.mark.parametrize(
    "separator",
    [
        "",  # deleted: the rows would sit directly under the header, not render as a table
        "|---|---:|---:|---:|",  # one column short of the header
        "| --- | wall_s |",  # not a separator at all
    ],
)
def test_headline_separator_that_is_missing_or_malformed_fails(
    tmp_path: Path, separator: str
) -> None:
    old = "|---|---:|---:|---:|---:|\n"
    assert _README.count(old) == 1
    readme = _README.replace(old, f"{separator}\n" if separator else "")
    with pytest.raises(ValueError, match="headline table separator"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


def test_headline_row_outside_the_headline_table_fails(tmp_path: Path) -> None:
    # An `| x86 |` row elsewhere in the README is not the headline row.
    readme = _README.replace("| x86 | 1.00 / 1.00 |", "| amd | 1.00 / 1.00 |") + (
        "\n| x86 | unrelated |\n"
    )
    with pytest.raises(ValueError, match=r"'\| x86 \|' found 0 times"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A blank blockquote line splits the m8g table: its old rows would be left behind.
        (
            "> |---|---:|---:|---:|---:|---:|\n> | bwa | 1.00 | 1.00x | — |",
            ">\n> |---|---:|---:|---:|---:|---:|\n> | bwa | 1.00 | 1.00x | — |",
        ),
        # A plain blank line inside the m8a table.
        (
            "> |---|---:|---:|---:|---:|---:|\n> | bwa | 1.00 | 1.00x | 1.00x |",
            "> |---|---:|---:|---:|---:|---:|\n\n> | bwa | 1.00 | 1.00x | 1.00x |",
        ),
    ],
)
def test_host_table_split_by_a_blank_line_fails(tmp_path: Path, old: str, new: str) -> None:
    assert _README.count(old) == 1
    with pytest.raises(ValueError, match="split|separator"):
        update_readme(_README.replace(old, new), _hosts(_db(tmp_path)), "v0.10.0")


def test_host_table_header_that_differs_fails(tmp_path: Path) -> None:
    # wall_s moved after vs bwa-mem2: positional parsing would read an em-dash as the wall,
    # so a README --fast wall the arena lacks would go undetected and be deleted.
    header = "> | release | wall_s | vs bwa | vs bwa-mem2 | vs minibwa | vs prev |"
    readme = _README.replace(
        header, "> | release | vs bwa | vs bwa-mem2 | wall_s | vs minibwa | vs prev |", 1
    )
    with pytest.raises(ValueError, match="release table header must be"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


def test_truncated_release_row_fails_as_drift(tmp_path: Path) -> None:
    readme = _README.replace("> | bwa | 1.00 | 1.00x | — | 1.00x | — |", "> | v0.8.0", 1)
    with pytest.raises(ValueError, match="no wall_s cell"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")


def test_rerun_for_the_same_version_checks_the_prose_markers(tmp_path: Path) -> None:
    readme = _README.replace("5 rounds for v0.13.0 through v0.9.0,", "five rounds,")
    with pytest.raises(ValueError, match="prose marker"):
        update_readme(readme, _hosts(_db(tmp_path), version="v0.9.0"), "v0.9.0")


def test_bless_plan_names_the_commands_real_flags() -> None:
    plan = "\n".join(_bless_release._plan("a" * 40, "b" * 40))
    line = next(step for step in plan.splitlines() if "bench readme-tables" in step)
    flags = {flag.removeprefix("--").replace("-", "_") for flag in re.findall(r"--[a-z-]+", line)}
    params = set(inspect.signature(bench.readme_tables).parameters)
    assert {"fg_labs_sha", "version", "readme", "arena_sa_stride"} <= flags
    assert flags <= params


@pytest.mark.parametrize("arch", ["m8g", "m8a"])
@pytest.mark.parametrize("wall", ["1.00/1.00", "1.00 /1.00", "1.00/ 1.00"])
def test_compact_fast_wall_is_not_dropped(tmp_path: Path, arch: str, wall: str) -> None:
    heading = "Graviton4" if arch == "m8g" else "AMD"
    before, after = _README.split(f"> **{heading}", 1)
    original = (
        before + f"> **{heading}" + after.replace("> | bwa | 1.00 |", f"> | v0.8.0 | {wall} |", 1)
    )
    readme = tmp_path / "README.md"
    readme.write_text(original, encoding="utf-8")
    rows = [r for r in _ROWS if r[1] != "v080-fast"]
    with pytest.raises(ValueError, match="has --fast walls"):
        _generate(_db(tmp_path, rows), readme)
    assert readme.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("label", ["fg-labs-default", "minibwa"])
def test_stale_round_cannot_replace_missing_round(tmp_path: Path, label: str) -> None:
    rows = [(*r[:4], 99) if r[0] == "m8g" and r[1] == label and r[4] == 1 else r for r in _ROWS]
    with pytest.raises(ValueError, match="round identities"):
        _hosts(_db(tmp_path, rows))


@pytest.mark.parametrize("label", ["v080", "v080-fast", "bwa", "minibwa", "bwa-mem2-upstream"])
def test_wrong_non_dense_index_family_is_rejected(tmp_path: Path, label: str) -> None:
    with pytest.raises(ValueError, match="index family"):
        _hosts(_db(tmp_path, blocks={label: "wrong-family"}))


@pytest.mark.parametrize("host", ["Graviton4", "AMD"])
@pytest.mark.parametrize("prefix", [">  |", ">\t|"])
def test_noncanonical_final_row_prefix_fails_without_writing(
    tmp_path: Path, host: str, prefix: str
) -> None:
    original = update_readme(_README, _hosts(_db(tmp_path)), "v0.10.0")
    before, after = original.split(f"> **{host}", 1)
    after = after.replace("> | **v0.10.0**", f"{prefix} **v0.10.0**", 1)
    original = before + f"> **{host}" + after
    readme = tmp_path / "README.md"
    readme.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="table row prefix"):
        _generate(tmp_path / "benchmark.db", readme)
    assert readme.read_text(encoding="utf-8") == original


def test_unrelated_later_table_is_preserved(tmp_path: Path) -> None:
    suffix = "\n## Another section\n\n> | key | value |\n> |---|---|\n> | a | b |\n"
    text = update_readme(_README + suffix, _hosts(_db(tmp_path)), "v0.10.0")
    assert text.endswith(suffix)


def test_cli_requires_explicit_arena_stride(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "DB_PATH", _db(tmp_path))
    readme = tmp_path / "README.md"
    readme.write_text(_README, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "bench",
                "readme-tables",
                "--fg-labs-sha",
                SHA,
                "--version",
                "v0.10.0",
                "--readme",
                str(readme),
            ]
        )
    assert error.value.code == 2  # noqa: PLR2004 -- argparse usage error
    assert readme.read_text(encoding="utf-8") == _README


def test_fast_predecessor_chain_skips_stock_only_release(tmp_path: Path) -> None:
    rows = [r for r in _ROWS if r[1] != "v080-fast"]
    rows = [(*r[:3], 10.0, r[4]) if r[1] == "fg-labs-fast" else r for r in rows]
    table = render_host_table(_hosts(_db(tmp_path, rows))["m8a"])
    # v130: stock 64/50=1.280; fast skips stock-only v080, so 40/25=1.600.
    assert table[-2].endswith("| 1.280x / 1.600x |")
    # Candidate: stock 50/40=1.250; fast 25/10=2.500.
    assert table[-1].endswith("| **1.250x / 2.500x** |")


@pytest.mark.parametrize(
    "extra",
    [
        "  | arch | wall_s | vs bwa | vs bwa-mem2 | vs minibwa |\n"
        "  |---|---:|---:|---:|---:|\n  | x86 | 1 | 1 | 1 | 1 |\n",
        "  | x86 | 1 | 1 | 1 | 1 |\n",
        "|x86| 1 | 1 | 1 | 1 |\n",
    ],
)
def test_alternate_headline_duplicates_are_rejected(tmp_path: Path, extra: str) -> None:
    readme = _README
    marker = "\n\\* v0.9.0, like"
    assert readme.count(marker) == 1
    readme = readme.replace(marker, extra + marker)
    with pytest.raises(ValueError, match="headline.*(header|row)"):
        update_readme(readme, _hosts(_db(tmp_path)), "v0.10.0")
