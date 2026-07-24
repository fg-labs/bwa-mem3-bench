"""Ingest tests for the thread-scaling ladder."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench.storage.ingest import ingest_scaling
from bwa_mem3_bench.storage.sqlite import connect

LADDER_ROWS = 4  # rungs in the fixture below: 1, 16x2, 64
PROCESS_16_REP1 = 91.0

LADDER = (
    "threads\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\n"
    "1\t1\t1400.0\t1395.0\t16500.0\t1390.2\n"
    "16\t1\t93.1\t1403.0\t16520.0\t91.0\n"
    "16\t2\t93.5\t1405.0\t16521.0\t91.4\n"
    "64\t1\t28.0\t1600.0\t16800.0\tNA\n"
)


def _write_ladder(root: Path, sha: str, text: str = LADDER) -> None:
    cell = root / sha / "wgs-5M" / "c8g64"
    cell.mkdir(parents=True)
    (cell / "scaling.tsv").write_text(text)


def test_ingest_scaling_reads_every_rung(tmp_path: Path) -> None:
    _write_ladder(tmp_path / "scaling", "abc123")
    conn = connect(tmp_path / "db.sqlite")
    assert (
        ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="abc123") == LADDER_ROWS
    )
    rows = conn.execute(
        "select threads, rep, wall_seconds from scaling order by threads, rep"
    ).fetchall()
    assert rows == [(1, 1, 1400.0), (16, 1, 93.1), (16, 2, 93.5), (64, 1, 28.0)]


def test_na_process_becomes_null_not_a_crash(tmp_path: Path) -> None:
    """One unparseable PROCESS() must not lose the whole ladder."""
    _write_ladder(tmp_path / "scaling", "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="abc123")
    (proc,) = conn.execute("select process_seconds from scaling where threads=64").fetchone()
    assert proc is None
    (proc16,) = conn.execute(
        "select process_seconds from scaling where threads=16 and rep=1"
    ).fetchone()
    assert proc16 == PROCESS_16_REP1


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    """Re-collecting a run must update rows, not duplicate them."""
    _write_ladder(tmp_path / "scaling", "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="abc123")
    ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="abc123")
    (n,) = conn.execute("select count(*) from scaling").fetchone()
    assert n == LADDER_ROWS


def test_missing_ladder_is_not_an_error(tmp_path: Path) -> None:
    """Runs that never requested thread_scaling must ingest cleanly as zero."""
    (tmp_path / "scaling").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="nosuchsha") == 0
