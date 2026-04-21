"""Unit tests for the SQLite storage layer."""

from pathlib import Path

import pytest

from bwa_mem3_bench.storage.sqlite import connect, upsert_comparison, upsert_run, upsert_trial


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "benchmark.db"


def test_connect_creates_tables(db_path: Path) -> None:
    conn = connect(db_path)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {row[0] for row in cur.fetchall()}
    assert {"runs", "trials", "comparisons"} <= names
    conn.close()


def test_upsert_run_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc1234", upstream_tag="v2.2.1", status="complete")
    upsert_run(conn, fg_labs_sha="abc1234", upstream_tag="v2.2.1", status="complete")
    cur = conn.execute("SELECT COUNT(*) FROM runs WHERE fg_labs_sha = ?", ("abc1234",))
    assert cur.fetchone()[0] == 1
    conn.close()


def test_upsert_trial_returns_id(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc1234", status="complete")
    trial_id = upsert_trial(
        conn,
        fg_labs_sha="abc1234",
        sample="smoke-1M",
        arch="c7g",
        rep=1,
        wall_seconds=12.3,
        max_rss_mb=1024.0,
        cpu_time=48.0,
        io_read_mb=200.0,
        io_write_mb=50.0,
        mean_load=380.0,
        reads_processed=2003,
        instance_type="local",
        availability_zone="local",
        spot_price=None,
        status="ok",
    )
    assert isinstance(trial_id, int)
    assert trial_id > 0
    conn.close()


def test_upsert_comparison_links_to_trial(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc1234", status="complete")
    trial_id = upsert_trial(
        conn,
        fg_labs_sha="abc1234",
        sample="smoke-1M",
        arch="c7g",
        rep=1,
        wall_seconds=12.3,
        max_rss_mb=1024.0,
        cpu_time=48.0,
        io_read_mb=200.0,
        io_write_mb=50.0,
        mean_load=380.0,
        reads_processed=2003,
        instance_type="local",
        availability_zone="local",
        spot_price=None,
        status="ok",
    )
    upsert_comparison(
        conn,
        trial_id=trial_id,
        kind="vs-baseline",
        concordant=2003,
        total=2003,
        concordance_pct=100.0,
        by_class_json='{"PosDiff": {"count": 0}}',
    )
    cur = conn.execute(
        "SELECT kind, concordance_pct FROM comparisons WHERE trial_id = ?",
        (trial_id,),
    )
    row = cur.fetchone()
    assert row == ("vs-baseline", 100.0)
    conn.close()
