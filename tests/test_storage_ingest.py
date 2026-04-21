"""Tests for ingest.walk_run → SQLite."""

from pathlib import Path

import pytest

from bwa_mem3_bench.storage.ingest import (
    _parse_bwa_stderr,
    baseline_sha_for,
    ingest_baseline,
    ingest_run,
)
from bwa_mem3_bench.storage.sqlite import connect

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_run"
BASELINE_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_baseline"

# Values matched against the synthetic bwa.stderr.log fixture under
# tests/fixtures/synthetic_run/abc1234/smoke-1M/c7g/rep-1/benchmarks/.
_FIXTURE_PROCESS_SECONDS = 8.76
_FIXTURE_INDEX_READ_SECONDS = 2.44


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "benchmark.db"


def test_ingest_creates_run_and_trial_and_comparison(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    assert n == 1

    runs = conn.execute("SELECT fg_labs_sha FROM runs").fetchall()
    assert runs == [("abc1234",)]

    trial = conn.execute(
        "SELECT sample, arch, rep, wall_seconds, instance_type, reads_processed,"
        " process_seconds, index_read_seconds FROM trials"
    ).fetchone()
    assert trial == (
        "smoke-1M",
        "c7g",
        1,
        12.345,
        "c7g.4xlarge",
        2003,
        _FIXTURE_PROCESS_SECONDS,
        _FIXTURE_INDEX_READ_SECONDS,
    )

    comp = conn.execute(
        "SELECT kind, concordance_pct, total, concordant FROM comparisons"
    ).fetchone()
    assert comp == ("vs-baseline", 100.0, 2003, 2003)

    conn.close()


def test_parse_bwa_stderr_extracts_process_and_index_read(tmp_path: Path) -> None:
    log = tmp_path / "bwa.stderr.log"
    log.write_text(
        'Looking to launch executable "/usr/local/bin/bwa-mem2.fg-labs.avx2"\n'
        "* Done reading Index!!\n"
        "\tIndex read time avg: 2.44, (2.44, 2.44)\n"
        "\n\tOverall time (sec) (Excluding Index reading time):\n"
        "\tPROCESS() (Total compute time + (read + SAM) IO time) : 8.76\n"
        "\tMEM_PROCESS_SEQ() (Total compute time (Kernel + SAM)), avg: 7.88, (7.88, 7.88)\n"
    )
    process, index_read = _parse_bwa_stderr(log)
    assert process == _FIXTURE_PROCESS_SECONDS
    assert index_read == _FIXTURE_INDEX_READ_SECONDS


def test_parse_bwa_stderr_missing_file_returns_none(tmp_path: Path) -> None:
    process, index_read = _parse_bwa_stderr(tmp_path / "does-not-exist.log")
    assert process is None
    assert index_read is None


def test_parse_bwa_stderr_truncated_log_returns_partial(tmp_path: Path) -> None:
    """Log truncated mid-init (e.g., bwa OOM-killed) — Index read present, PROCESS missing."""
    log = tmp_path / "truncated.log"
    log.write_text(
        "* Done reading Index!!\n"
        "\tIndex read time avg: 2.44, (2.44, 2.44)\n"
        "\n[std::bad_alloc... bwa crashed]\n"
    )
    process, index_read = _parse_bwa_stderr(log)
    assert process is None
    assert index_read == _FIXTURE_INDEX_READ_SECONDS


def test_ingest_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    n = ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    assert n == 1

    trials = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
    assert trials == (1,)
    comparisons = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()
    assert comparisons == (1,)
    conn.close()


def test_ingest_unknown_sha_raises(db_path: Path) -> None:
    conn = connect(db_path)
    with pytest.raises(FileNotFoundError):
        ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="does-not-exist")
    conn.close()


EXPECTED_BASELINE_TRIALS = 3  # 2 reps for c7g + 1 rep for c6a in the fixture


def test_ingest_baseline_inserts_synthetic_sha_trials(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    assert n == EXPECTED_BASELINE_TRIALS

    synthetic = baseline_sha_for("v2.2.1")
    runs = conn.execute(
        "SELECT fg_labs_sha, upstream_tag, status FROM runs WHERE fg_labs_sha = ?",
        (synthetic,),
    ).fetchone()
    assert runs == (synthetic, "v2.2.1", "baseline")

    rows = conn.execute(
        "SELECT sample, arch, rep, wall_seconds FROM trials "
        "WHERE fg_labs_sha = ? ORDER BY arch, rep",
        (synthetic,),
    ).fetchall()
    assert rows == [
        ("smoke-1M", "c6a", 1, 14.2),
        ("smoke-1M", "c7g", 1, 20.0),
        ("smoke-1M", "c7g", 2, 21.0),
    ]

    # No comparisons should be created for baseline-only ingestion.
    comp_count = conn.execute(
        """
        SELECT COUNT(*) FROM comparisons c
        JOIN trials t ON t.id = c.trial_id
        WHERE t.fg_labs_sha = ?
        """,
        (synthetic,),
    ).fetchone()
    assert comp_count == (0,)
    conn.close()


def test_ingest_baseline_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    assert n == EXPECTED_BASELINE_TRIALS
    total = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE fg_labs_sha = ?",
        (baseline_sha_for("v2.2.1"),),
    ).fetchone()
    assert total == (EXPECTED_BASELINE_TRIALS,)
    conn.close()


def test_ingest_baseline_missing_tool_returns_zero(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v999.9.9")
    assert n == 0
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert runs == (0,)
    conn.close()
