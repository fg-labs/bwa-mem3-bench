"""Unit tests for the SQLite storage layer."""

import sqlite3
from pathlib import Path

import pytest

from bwa_mem3_bench.storage.sqlite import (
    EXPECTED_SCHEMA_VERSION,
    connect,
    upsert_comparison,
    upsert_run,
    upsert_trial,
)

# A pre-supp-metrics (v2) comparisons table — no supp_json column.
_V2_SCHEMA = """
PRAGMA user_version = 2;
CREATE TABLE runs (fg_labs_sha TEXT PRIMARY KEY, fg_labs_branch TEXT,
    upstream_tag TEXT, submitted_at TIMESTAMP, status TEXT);
CREATE TABLE trials (id INTEGER PRIMARY KEY AUTOINCREMENT, fg_labs_sha TEXT,
    sample TEXT, arch TEXT, rep INTEGER, instance_type TEXT, availability_zone TEXT,
    spot_price REAL, wall_seconds REAL, max_rss_mb REAL, cpu_time REAL, io_read_mb REAL,
    io_write_mb REAL, mean_load REAL, reads_processed INTEGER, process_seconds REAL,
    index_read_seconds REAL, status TEXT, UNIQUE(fg_labs_sha, sample, arch, rep));
CREATE TABLE comparisons (trial_id INTEGER, kind TEXT, concordant INTEGER, total INTEGER,
    concordance_pct REAL, by_class_json TEXT, UNIQUE(trial_id, kind));
"""

# A v1 DB: trials lacks process_seconds/index_read_seconds AND comparisons lacks
# supp_json — exercises the full v1→v2→v3 chain.
_V1_SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE runs (fg_labs_sha TEXT PRIMARY KEY, fg_labs_branch TEXT,
    upstream_tag TEXT, submitted_at TIMESTAMP, status TEXT);
CREATE TABLE trials (id INTEGER PRIMARY KEY AUTOINCREMENT, fg_labs_sha TEXT,
    sample TEXT, arch TEXT, rep INTEGER, instance_type TEXT, availability_zone TEXT,
    spot_price REAL, wall_seconds REAL, max_rss_mb REAL, cpu_time REAL, io_read_mb REAL,
    io_write_mb REAL, mean_load REAL, reads_processed INTEGER, status TEXT,
    UNIQUE(fg_labs_sha, sample, arch, rep));
CREATE TABLE comparisons (trial_id INTEGER, kind TEXT, concordant INTEGER, total INTEGER,
    concordance_pct REAL, by_class_json TEXT, UNIQUE(trial_id, kind));
"""


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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_supp_json_column(db_path: Path) -> None:
    conn = connect(db_path)
    assert "supp_json" in _columns(conn, "comparisons")
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    conn.close()


def test_v2_db_migrates_to_v3_adding_supp_json(db_path: Path) -> None:
    # Build a v2 DB with one row, then let connect() forward-migrate it.
    raw = sqlite3.connect(db_path)
    raw.executescript(_V2_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "supp_json" in _columns(conn, "comparisons")
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    # existing data survives the migration
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_v1_db_migrates_through_to_v3(db_path: Path) -> None:
    # Full chain: v1 → v2 (process/index cols) → v3 (supp_json).
    raw = sqlite3.connect(db_path)
    raw.executescript(_V1_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert {"process_seconds", "index_read_seconds"} <= _columns(conn, "trials")
    assert "supp_json" in _columns(conn, "comparisons")
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_connect_rejects_newer_db_without_mutating_it(db_path: Path) -> None:
    # A DB newer than this code must be refused *without* being written to.
    # SCHEMA_SQL sets `PRAGMA user_version`, so the newer-version guard has to
    # run before executescript — otherwise the newer DB's version is clobbered
    # down to EXPECTED before the raise.
    newer = EXPECTED_SCHEMA_VERSION + 1
    raw = sqlite3.connect(db_path)
    raw.executescript(_V2_SCHEMA)  # tables; version overridden on the next line
    raw.execute(f"PRAGMA user_version = {newer}")
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('sentinel', 'complete')")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="newer than this"):
        connect(db_path)

    check = sqlite3.connect(db_path)
    (ver,) = check.execute("PRAGMA user_version").fetchone()
    check.close()
    assert ver == newer  # version pragma left untouched, not downgraded


def test_upsert_comparison_round_trips_supp_json(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc", status="complete")
    trial_id = upsert_trial(
        conn,
        fg_labs_sha="abc",
        sample="wes-5M",
        arch="c6a",
        rep=1,
        wall_seconds=1.0,
        max_rss_mb=1.0,
        cpu_time=1.0,
        io_read_mb=1.0,
        io_write_mb=1.0,
        mean_load=1.0,
        reads_processed=1,
        instance_type=None,
        availability_zone=None,
        spot_price=None,
        status="ok",
    )
    upsert_comparison(
        conn,
        trial_id=trial_id,
        kind="vs-baseline",
        concordant=1,
        total=1,
        concordance_pct=100.0,
        by_class_json="{}",
        supp_json='{"supp_unmatched": 9, "supp_count_mismatch_templates": 5}',
    )
    (got,) = conn.execute(
        "SELECT supp_json FROM comparisons WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    assert got == '{"supp_unmatched": 9, "supp_count_mismatch_templates": 5}'
    conn.close()
