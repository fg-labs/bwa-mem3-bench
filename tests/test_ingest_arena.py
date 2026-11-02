"""Tests for `ingest_arena`: legacy 7-column and paired-round 18-column rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bwa_mem3_bench.storage.ingest import ingest_arena
from bwa_mem3_bench.storage.sqlite import connect

SHA = "0a65bec1f47f8b773cf40b85d16833de010c8107"

_LEGACY_HEADER = "label\tmode\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s"
_FULL_HEADER = (
    _LEGACY_HEADER + "\tblock\tposition\twriter\tindex_read_s\tio_in_mb\tpage_cache_start_mb"
    "\tthp_fault_fallback\tcompact_stall\tpgmajfault\tmemfree_mb\tanon_huge_mb"
)


def _write_tsv(root: Path, arch: str, lines: list[str]) -> None:
    run_dir = root / SHA / arch
    run_dir.mkdir(parents=True)
    (run_dir / "arena.tsv").write_text("\n".join(lines) + "\n")


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM arena ORDER BY arch, label, rep").fetchall()
    conn.close()
    return rows


def test_ingests_paired_round_rows_with_host_state(tmp_path: Path) -> None:
    _write_tsv(
        tmp_path / "arena",
        "m8a",
        [
            _FULL_HEADER,
            "v151\tdefault\t0\t28.9\t337.9\t27305.7\t19.31\tbwa-mem2-dense\t1\tbam\t2.87"
            "\t5300.5\t31244\t812\t3\t54\t30100\t0",
            "fg-labs-default\tdefault\t1\t19.8\t295.8\t26884.2\t18.73\tbwa-mem2-dense\t2\tbam"
            "\t0.58\t0.46\t31244\t0\t0\t54\t29000\t23300",
            "v021-fast\tfast\t1\tNA\tNA\tNA\tNA\tbwa-mem2-stock\t3\tbam\tNA\tNA\tNA\t0\t0\t0\t28000\t0",
        ],
    )
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    assert ingest_arena(conn, arena_root=tmp_path / "arena", fg_labs_sha=SHA, sample="wgs-5M") == 3  # noqa: PLR2004
    conn.close()

    by_label = {row["label"]: row for row in _rows(db_path)}
    warmup = by_label["v151"]
    assert warmup["rep"] == 0
    assert warmup["block"] == "bwa-mem2-dense"
    assert warmup["position"] == 1
    assert warmup["writer"] == "bam"
    assert warmup["index_read_seconds"] == 2.87  # noqa: PLR2004
    assert warmup["io_in_mb"] == 5300.5  # noqa: PLR2004
    assert warmup["thp_fault_fallback"] == 812  # noqa: PLR2004
    assert warmup["compact_stall"] == 3  # noqa: PLR2004
    assert warmup["anon_huge_mb"] == 0
    skipped = by_label["v021-fast"]
    assert skipped["wall_seconds"] is None
    assert skipped["index_read_seconds"] is None
    assert skipped["block"] == "bwa-mem2-stock"


def test_still_ingests_legacy_seven_column_rows_with_null_host_state(tmp_path: Path) -> None:
    _write_tsv(
        tmp_path / "arena",
        "m8g",
        [_LEGACY_HEADER, "v151\tdefault\t1\t34.83\t523.7\t26695.9\t33.42"],
    )
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    assert ingest_arena(conn, arena_root=tmp_path / "arena", fg_labs_sha=SHA, sample="wgs-5M") == 1
    conn.close()
    (row,) = _rows(db_path)
    assert row["wall_seconds"] == 34.83  # noqa: PLR2004
    assert row["block"] is None
    assert row["writer"] is None


def test_skips_and_reports_a_row_that_does_not_match_the_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_tsv(
        tmp_path / "arena",
        "m8a",
        [_LEGACY_HEADER, "v151\tdefault\t1\t20\t1\t1\t1", "v140\tdefault\t1\t20.0"],
    )
    conn = connect(tmp_path / "benchmark.db")
    assert ingest_arena(conn, arena_root=tmp_path / "arena", fg_labs_sha=SHA, sample="wgs-5M") == 1
    conn.close()
    assert "skipped 1 row(s)" in capsys.readouterr().err


def test_maps_host_state_columns_by_header_name_not_position(tmp_path: Path) -> None:
    """A rule that reorders its extra columns must not misassign them."""
    header = _LEGACY_HEADER + "\twriter\tblock\tposition"
    _write_tsv(
        tmp_path / "arena",
        "m8a",
        [header, "v151\tdefault\t1\t20.0\t300\t27000\t19.0\tbam\tbwa-mem2-dense\t4"],
    )
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    ingest_arena(conn, arena_root=tmp_path / "arena", fg_labs_sha=SHA, sample="wgs-5M")
    conn.close()
    (row,) = _rows(db_path)
    assert (row["block"], row["writer"], row["position"]) == ("bwa-mem2-dense", "bam", 4)


def test_rejects_an_unknown_header_column(tmp_path: Path) -> None:
    _write_tsv(
        tmp_path / "arena", "m8a", [_LEGACY_HEADER + "\tbogus", "v151\tdefault\t1\t1\t1\t1\t1\tx"]
    )
    conn = connect(tmp_path / "benchmark.db")
    with pytest.raises(ValueError, match="bogus"):
        ingest_arena(conn, arena_root=tmp_path / "arena", fg_labs_sha=SHA, sample="wgs-5M")
    conn.close()


def test_reingesting_an_arch_replaces_its_earlier_rows(tmp_path: Path) -> None:
    """One arena.tsv is one job; a re-run's file must not leave the earlier job's
    rows behind for the report to pair with the new ones."""
    root = tmp_path / "arena"
    _write_tsv(
        root,
        "m8a",
        [_LEGACY_HEADER, "v151\tdefault\t1\t20\t1\t1\t1", "v140\tdefault\t1\t21\t1\t1\t1"],
    )
    _write_tsv(root, "m8g", [_LEGACY_HEADER, "v151\tdefault\t1\t30\t1\t1\t1"])
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    ingest_arena(conn, arena_root=root, fg_labs_sha=SHA, sample="wgs-5M")
    (root / SHA / "m8a" / "arena.tsv").write_text(
        _LEGACY_HEADER + "\nv151\tdefault\t1\t19\t1\t1\t1\n"
    )
    ingest_arena(conn, arena_root=root, fg_labs_sha=SHA, sample="wgs-5M")
    conn.close()
    rows = {(row["arch"], row["label"]): row["wall_seconds"] for row in _rows(db_path)}
    assert rows == {("m8a", "v151"): 19.0, ("m8g", "v151"): 30.0}


@pytest.mark.parametrize(
    "content", ["", _LEGACY_HEADER, _LEGACY_HEADER + "\nv151\tdefault\t1\t20.0"]
)
def test_reingesting_a_file_with_no_valid_row_keeps_the_earlier_rows(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty, header-only or fully malformed arena.tsv is not a job's result;
    it must not wipe the rows a good earlier file loaded, and it must say so."""
    root = tmp_path / "arena"
    _write_tsv(root, "m8a", [_LEGACY_HEADER, "v151\tdefault\t1\t20\t1\t1\t1"])
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    ingest_arena(conn, arena_root=root, fg_labs_sha=SHA, sample="wgs-5M")
    (root / SHA / "m8a" / "arena.tsv").write_text(content)
    ingest_arena(conn, arena_root=root, fg_labs_sha=SHA, sample="wgs-5M")
    conn.close()
    assert [(row["label"], row["wall_seconds"]) for row in _rows(db_path)] == [("v151", 20.0)]
    assert "no valid row" in capsys.readouterr().err
