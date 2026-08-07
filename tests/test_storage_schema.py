"""Unit tests for the SQLite storage layer."""

import sqlite3
from pathlib import Path

import pytest

from bwa_mem3_bench.storage.sqlite import (
    EXPECTED_SCHEMA_VERSION,
    connect,
    upsert_accuracy,
    upsert_comparison,
    upsert_host_probe,
    upsert_run,
    upsert_trial,
)

# One reading either side of the timed work.
_PROBE_PHASES = 2
# An arbitrary second value for the in-place-update assertion.
_REVISED_RATE = 30.1

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


# A v3 DB: runs/trials/comparisons present (with supp_json) but no `accuracy`
# table — exercises the v3→v4 migration that creates it.
_V3_SCHEMA = """
PRAGMA user_version = 3;
CREATE TABLE runs (fg_labs_sha TEXT PRIMARY KEY, fg_labs_branch TEXT,
    upstream_tag TEXT, submitted_at TIMESTAMP, status TEXT);
CREATE TABLE trials (id INTEGER PRIMARY KEY AUTOINCREMENT, fg_labs_sha TEXT,
    sample TEXT, arch TEXT, rep INTEGER, instance_type TEXT, availability_zone TEXT,
    spot_price REAL, wall_seconds REAL, max_rss_mb REAL, cpu_time REAL, io_read_mb REAL,
    io_write_mb REAL, mean_load REAL, reads_processed INTEGER, process_seconds REAL,
    index_read_seconds REAL, status TEXT, UNIQUE(fg_labs_sha, sample, arch, rep));
CREATE TABLE comparisons (trial_id INTEGER, kind TEXT, concordant INTEGER, total INTEGER,
    concordance_pct REAL, by_class_json TEXT, supp_json TEXT, UNIQUE(trial_id, kind));
"""


# A v4 DB: everything through `accuracy`, but no `scaling` table — the state
# EVERY production DB is in before this schema bump, since SCHEMA_VERSION goes
# straight from 4 to 6.
_V4_SCHEMA = (
    _V3_SCHEMA.replace("PRAGMA user_version = 3;", "PRAGMA user_version = 4;")
    + """
CREATE TABLE accuracy (id INTEGER PRIMARY KEY AUTOINCREMENT, fg_labs_sha TEXT,
    sample TEXT, arch TEXT, rep INTEGER, tool TEXT, placement_total INTEGER,
    placement_correct_pct REAL, placement_mismapped_pct REAL,
    placement_unmapped_pct REAL, placement_json TEXT, variant_bearing_reads INTEGER,
    md_concordant_pct REAL, nm_concordant_pct REAL, by_class_json TEXT,
    meth_n_cpg INTEGER, meth_pearson_r REAL, meth_rmse REAL,
    UNIQUE(fg_labs_sha, sample, arch, rep, tool));
"""
)

# A v5 DB: has a `scaling` table, but from before the phase-breakdown columns —
# the one state where the v5→v6 ALTERs are needed.
_V5_SCHEMA = (
    _V4_SCHEMA.replace("PRAGMA user_version = 4;", "PRAGMA user_version = 5;")
    + """
CREATE TABLE scaling (id INTEGER PRIMARY KEY AUTOINCREMENT, fg_labs_sha TEXT,
    sample TEXT, arch TEXT, threads INTEGER, rep INTEGER, wall_seconds REAL,
    cpu_time REAL, max_rss_mb REAL, process_seconds REAL,
    UNIQUE(fg_labs_sha, sample, arch, threads, rep));
"""
)

# The columns the v5→v6 step adds to `scaling`.
_V6_SCALING_COLUMNS = {
    "main_mem_seconds",
    "read_io_seconds",
    "sam_io_seconds",
    "kernel_seconds",
}

# A v7 DB: trials has instance_id and `scaling` carries the phase columns, but
# `scaling` has no instance_id and there is no `host_probes` table — the state
# every production DB is in before the v8 bump.
_V7_SCHEMA = (
    _V5_SCHEMA.replace("PRAGMA user_version = 5;", "PRAGMA user_version = 7;")
    .replace(
        "reads_processed INTEGER, process_seconds REAL",
        "reads_processed INTEGER, instance_id TEXT, process_seconds REAL",
    )
    .replace(
        "cpu_time REAL, max_rss_mb REAL, process_seconds REAL,\n"
        "    UNIQUE(fg_labs_sha, sample, arch, threads, rep));",
        "cpu_time REAL, max_rss_mb REAL, process_seconds REAL,\n"
        "    main_mem_seconds REAL, read_io_seconds REAL, sam_io_seconds REAL,\n"
        "    kernel_seconds REAL, UNIQUE(fg_labs_sha, sample, arch, threads, rep));",
    )
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "benchmark.db"


def test_connect_creates_tables(db_path: Path) -> None:
    conn = connect(db_path)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {row[0] for row in cur.fetchall()}
    assert {"runs", "trials", "comparisons", "accuracy", "scaling", "host_probes"} <= names
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


def test_v1_db_migrates_through_to_latest(db_path: Path) -> None:
    # Full chain: v1 → v2 (process/index cols) → v3 (supp_json) → v4 (accuracy).
    raw = sqlite3.connect(db_path)
    raw.executescript(_V1_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert {"process_seconds", "index_read_seconds"} <= _columns(conn, "trials")
    assert "supp_json" in _columns(conn, "comparisons")
    assert "accuracy" in _tables(conn)  # v4 table created on the v1→latest path
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v3_db_migrates_to_v4_adding_accuracy_table(db_path: Path) -> None:
    # A v3 DB (no accuracy table) gains it on connect(), data preserved.
    raw = sqlite3.connect(db_path)
    raw.executescript(_V3_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "accuracy" in _tables(conn)
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_v4_db_migrates_to_v6_without_altering_a_fresh_scaling_table(db_path: Path) -> None:
    """A v4 DB gets `scaling` from executescript, so the v6 ALTERs must NOT run.

    This is the state every production DB is in — SCHEMA_VERSION jumps 4 → 6 —
    so a migration guard that also matched v4 would raise "duplicate column
    name" on the very first `connect()` after upgrading. Every other migration
    test starts from a fresh v0 DB, which is how that slipped through.
    """
    raw = sqlite3.connect(db_path)
    raw.executescript(_V4_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "scaling" in _tables(conn)
    assert _columns(conn, "scaling") >= _V6_SCALING_COLUMNS
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_v5_db_migrates_to_v6_adding_the_phase_columns(db_path: Path) -> None:
    """A v5 DB owns a phase-less `scaling` table, so the v6 ALTERs must run."""
    raw = sqlite3.connect(db_path)
    raw.executescript(_V5_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.execute(
        "INSERT INTO scaling(fg_labs_sha, sample, arch, threads, rep, wall_seconds) "
        "VALUES ('old', 'wgs-5M', 'c8g64', 16, 1, 92.5)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert _columns(conn, "scaling") >= _V6_SCALING_COLUMNS
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    # The pre-existing rung survives, with NULL phases until it is re-ingested.
    assert conn.execute(
        "SELECT wall_seconds, kernel_seconds FROM scaling WHERE fg_labs_sha = 'old'"
    ).fetchone() == (92.5, None)
    conn.close()


def test_v7_db_migrates_to_v8_adding_host_attribution(db_path: Path) -> None:
    """A v7 DB owns an instance_id-less `scaling`, so the v8 ALTER must run.

    This is the state every production DB is in before the bump, including the
    real 7,130-row one. The pre-existing rung must survive with NULL — which is
    also the honest value, since nothing recorded the host at the time.
    """
    raw = sqlite3.connect(db_path)
    raw.executescript(_V7_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.execute(
        "INSERT INTO scaling(fg_labs_sha, sample, arch, threads, rep, wall_seconds) "
        "VALUES ('old', 'wgs-5M', 'c8g64', 1, 1, 1342.3)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "instance_id" in _columns(conn, "scaling")
    assert "host_probes" in _tables(conn)
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute(
        "SELECT wall_seconds, instance_id FROM scaling WHERE fg_labs_sha = 'old'"
    ).fetchone() == (1342.3, None)
    conn.close()


def test_v4_db_migrates_to_v8_without_altering_a_fresh_scaling_table(db_path: Path) -> None:
    """A pre-v5 DB gets `scaling` from executescript, already carrying instance_id.

    So the v8 ALTER must NOT run for it — the same trap the v5→v6 step
    documented. A guard of `existing_version < 8` alone would raise "duplicate
    column name" on the first connect() after upgrading such a DB.
    """
    raw = sqlite3.connect(db_path)
    raw.executescript(_V4_SCHEMA)
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "instance_id" in _columns(conn, "scaling")
    assert "host_probes" in _tables(conn)
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_v8_db_migrates_to_v9_adding_measured_at(db_path: Path) -> None:
    """A v8 DB owns a `trials` without `measured_at`, so the v9 ALTER must run.

    Unlike the v8 step there is no lower bound to get right: `trials` exists in
    every schema this code has ever written, so no version gets it freshly
    CREATEd by the executescript and then ALTERed twice.
    """
    raw = sqlite3.connect(db_path)
    raw.executescript(_V7_SCHEMA.replace("PRAGMA user_version = 7;", "PRAGMA user_version = 8;"))
    raw.execute("INSERT INTO runs(fg_labs_sha, status) VALUES ('old', 'complete')")
    raw.execute(
        "INSERT INTO trials(fg_labs_sha, sample, arch, rep, wall_seconds) "
        "VALUES ('old', 'wgs-5M', 'm7i', 1, 19.41)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    assert "measured_at" in _columns(conn, "trials")
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == EXPECTED_SCHEMA_VERSION
    # The pre-existing trial survives; NULL is the honest value for a cell
    # measured before anything recorded when it ran.
    assert conn.execute(
        "SELECT wall_seconds, measured_at FROM trials WHERE fg_labs_sha = 'old'"
    ).fetchone() == (19.41, None)
    conn.close()


def test_upsert_host_probe_is_keyed_on_phase(db_path: Path) -> None:
    """Two phases coexist; re-recording one phase updates it in place."""
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc", status="complete")
    for phase, rate in (("pre", 28.4), ("post", 17.9)):
        upsert_host_probe(
            conn,
            fg_labs_sha="abc",
            sample="wgs-5M",
            arch="c8g64",
            phase=phase,
            instance_id="i-abc",
            probe_version="0.1.0",
            rustc="rustc 1.97.1",
            m_accesses_per_sec=rate,
            ns_per_access=126.0,
            threads=64,
            working_set_mb_per_thread=64.0,
            seconds=10.0,
            status="ok",
        )
    assert conn.execute("SELECT COUNT(*) FROM host_probes").fetchone()[0] == _PROBE_PHASES

    upsert_host_probe(
        conn,
        fg_labs_sha="abc",
        sample="wgs-5M",
        arch="c8g64",
        phase="pre",
        instance_id="i-abc",
        probe_version="0.1.0",
        rustc="rustc 1.97.1",
        m_accesses_per_sec=_REVISED_RATE,
        ns_per_access=140.0,
        threads=64,
        working_set_mb_per_thread=64.0,
        seconds=10.0,
        status="ok",
    )
    assert conn.execute("SELECT COUNT(*) FROM host_probes").fetchone()[0] == _PROBE_PHASES
    (rate,) = conn.execute(
        "SELECT m_accesses_per_sec FROM host_probes WHERE phase = 'pre'"
    ).fetchone()
    assert rate == _REVISED_RATE
    conn.close()


def test_upsert_accuracy_round_trips(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc", status="complete")
    row_id = upsert_accuracy(
        conn,
        fg_labs_sha="abc",
        sample="sim-meth-vars",
        arch="m7i",
        rep=1,
        tool="fg-labs",
        placement_total=1000,
        placement_correct_pct=99.5,
        placement_mismapped_pct=0.3,
        placement_unmapped_pct=0.2,
        placement_json='{"all": {"total": 1000}}',
        variant_bearing_reads=42,
        md_concordant_pct=None,  # "NA" → NULL
        nm_concordant_pct=None,
        by_class_json='{"mirror": {"n_expected": 10}}',
        meth_n_cpg=500,
        meth_pearson_r=0.97,
        meth_rmse=0.04,
    )
    assert isinstance(row_id, int) and row_id > 0
    got = conn.execute(
        "SELECT tool, placement_correct_pct, variant_bearing_reads, "
        "md_concordant_pct, meth_pearson_r FROM accuracy WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert got == ("fg-labs", 99.5, 42, None, 0.97)
    conn.close()


def test_upsert_accuracy_is_idempotent_on_key(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha="abc", status="complete")
    key = {
        "fg_labs_sha": "abc",
        "sample": "sim-wgs-vars",
        "arch": "c6a",
        "rep": 1,
        "tool": "minibwa",
    }
    common = {
        "placement_total": 100,
        "placement_correct_pct": 90.0,
        "placement_mismapped_pct": 5.0,
        "placement_unmapped_pct": 5.0,
        "placement_json": "{}",
        "variant_bearing_reads": 1,
        "md_concordant_pct": None,
        "nm_concordant_pct": None,
        "by_class_json": "{}",
        "meth_n_cpg": None,
        "meth_pearson_r": None,
        "meth_rmse": None,
    }
    upsert_accuracy(conn, **key, **common)
    upsert_accuracy(conn, **key, **{**common, "placement_correct_pct": 95.0})
    rows = conn.execute(
        "SELECT placement_correct_pct FROM accuracy WHERE fg_labs_sha = 'abc'"
    ).fetchall()
    assert rows == [(95.0,)]  # one row, updated in place
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
