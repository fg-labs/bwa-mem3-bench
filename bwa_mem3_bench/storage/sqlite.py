"""SQLite connect + upsert helpers for the benchmark DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bwa_mem3_bench.storage.schema import SCHEMA_SQL, SCHEMA_VERSION

EXPECTED_SCHEMA_VERSION = SCHEMA_VERSION

# Older DBs are auto-migrated in place; column additions via ALTER TABLE, new
# tables via the `CREATE TABLE IF NOT EXISTS` in SCHEMA_SQL (run unconditionally
# by connect()). Existing rows keep NULL for new columns until `cli collect`
# re-ingests them.
#   v1 → v2: added trials.process_seconds + trials.index_read_seconds.
#   v2 → v3: added comparisons.supp_json (compare-bams supplementary metrics).
#   v3 → v4: added the `accuracy` table (holodeck truth-based eval) — a new
#            table, so no ALTER is needed; executescript creates it in place.
#   v4 → v5: added the `scaling` table (thread-scaling ladder) — likewise a new
#            table, created in place by executescript, no ALTER needed.
_SCHEMA_V1 = 1
_SCHEMA_V2 = 2
_SCHEMA_V3 = 3


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the benchmark SQLite DB and ensure tables exist.

    Reads user_version *before* applying SCHEMA_SQL so we can tell an older DB
    from a freshly-created one — SCHEMA_SQL sets user_version unconditionally to
    the current version, so reading after would always show the latest.

    Older DBs are forward-migrated in place, oldest step first
    (v1→v2: trials.process_seconds/index_read_seconds; v2→v3:
    comparisons.supp_json). Raises RuntimeError if the DB is *newer* than this
    code understands (user_version > EXPECTED_SCHEMA_VERSION).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    (existing_version,) = conn.execute("PRAGMA user_version").fetchone()
    # Guard a too-new DB *before* any write. SCHEMA_SQL sets user_version, so
    # running it first would clobber a newer DB's version pragma before we raise.
    if existing_version > EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"benchmark.db schema version {existing_version} is newer than this "
            f"code supports (expected {EXPECTED_SCHEMA_VERSION}); upgrade the tool "
            f"or rebuild the DB"
        )
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    if existing_version == 0:
        # Fresh DB; SCHEMA_SQL just created the tables at the current version.
        return conn
    # Forward-migrate in place, oldest step first. Each ALTER runs only when
    # coming from a version that predates it, so a column is never added twice.
    if existing_version <= _SCHEMA_V1:
        conn.execute("ALTER TABLE trials ADD COLUMN process_seconds REAL")
        conn.execute("ALTER TABLE trials ADD COLUMN index_read_seconds REAL")
    if existing_version <= _SCHEMA_V2:
        conn.execute("ALTER TABLE comparisons ADD COLUMN supp_json TEXT")
    if existing_version < EXPECTED_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {EXPECTED_SCHEMA_VERSION}")
        conn.commit()
    return conn


def upsert_run(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    fg_labs_sha: str,
    fg_labs_branch: str | None = None,
    upstream_tag: str | None = None,
    status: str = "complete",
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (fg_labs_sha, fg_labs_branch, upstream_tag, status)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(fg_labs_sha) DO UPDATE SET
            fg_labs_branch = COALESCE(excluded.fg_labs_branch, runs.fg_labs_branch),
            upstream_tag   = COALESCE(excluded.upstream_tag,   runs.upstream_tag),
            status         = excluded.status
        """,
        (fg_labs_sha, fg_labs_branch, upstream_tag, status),
    )
    if commit:
        conn.commit()


def upsert_trial(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    fg_labs_sha: str,
    sample: str,
    arch: str,
    rep: int,
    wall_seconds: float,
    max_rss_mb: float,
    cpu_time: float,
    io_read_mb: float,
    io_write_mb: float,
    mean_load: float,
    reads_processed: int,
    instance_type: str | None,
    availability_zone: str | None,
    spot_price: float | None,
    status: str,
    process_seconds: float | None = None,
    index_read_seconds: float | None = None,
    commit: bool = True,
) -> int:
    """Insert or update a trial row; returns the trial id."""
    row = conn.execute(
        """
        INSERT INTO trials (
            fg_labs_sha, sample, arch, rep, instance_type, availability_zone,
            spot_price, wall_seconds, max_rss_mb, cpu_time, io_read_mb,
            io_write_mb, mean_load, reads_processed, status,
            process_seconds, index_read_seconds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fg_labs_sha, sample, arch, rep) DO UPDATE SET
            instance_type = excluded.instance_type,
            availability_zone = excluded.availability_zone,
            spot_price = excluded.spot_price,
            wall_seconds = excluded.wall_seconds,
            max_rss_mb = excluded.max_rss_mb,
            cpu_time = excluded.cpu_time,
            io_read_mb = excluded.io_read_mb,
            io_write_mb = excluded.io_write_mb,
            mean_load = excluded.mean_load,
            reads_processed = excluded.reads_processed,
            status = excluded.status,
            process_seconds = excluded.process_seconds,
            index_read_seconds = excluded.index_read_seconds
        RETURNING id
        """,
        (
            fg_labs_sha,
            sample,
            arch,
            rep,
            instance_type,
            availability_zone,
            spot_price,
            wall_seconds,
            max_rss_mb,
            cpu_time,
            io_read_mb,
            io_write_mb,
            mean_load,
            reads_processed,
            status,
            process_seconds,
            index_read_seconds,
        ),
    ).fetchone()
    if commit:
        conn.commit()
    return int(row[0])


def upsert_accuracy(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    fg_labs_sha: str,
    sample: str,
    arch: str,
    rep: int,
    tool: str,
    placement_total: int | None,
    placement_correct_pct: float | None,
    placement_mismapped_pct: float | None,
    placement_unmapped_pct: float | None,
    placement_json: str | None,
    variant_bearing_reads: int | None,
    md_concordant_pct: float | None,
    nm_concordant_pct: float | None,
    by_class_json: str | None,
    meth_n_cpg: int | None,
    meth_pearson_r: float | None,
    meth_rmse: float | None,
    commit: bool = True,
) -> int:
    """Insert or update one accuracy row (one aligner arm of one sim cell).

    Keyed by ``(fg_labs_sha, sample, arch, rep, tool)``; re-ingesting a run
    overwrites in place. Returns the row id.
    """
    row = conn.execute(
        """
        INSERT INTO accuracy (
            fg_labs_sha, sample, arch, rep, tool,
            placement_total, placement_correct_pct, placement_mismapped_pct,
            placement_unmapped_pct, placement_json,
            variant_bearing_reads, md_concordant_pct, nm_concordant_pct, by_class_json,
            meth_n_cpg, meth_pearson_r, meth_rmse
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fg_labs_sha, sample, arch, rep, tool) DO UPDATE SET
            placement_total = excluded.placement_total,
            placement_correct_pct = excluded.placement_correct_pct,
            placement_mismapped_pct = excluded.placement_mismapped_pct,
            placement_unmapped_pct = excluded.placement_unmapped_pct,
            placement_json = excluded.placement_json,
            variant_bearing_reads = excluded.variant_bearing_reads,
            md_concordant_pct = excluded.md_concordant_pct,
            nm_concordant_pct = excluded.nm_concordant_pct,
            by_class_json = excluded.by_class_json,
            meth_n_cpg = excluded.meth_n_cpg,
            meth_pearson_r = excluded.meth_pearson_r,
            meth_rmse = excluded.meth_rmse
        RETURNING id
        """,
        (
            fg_labs_sha,
            sample,
            arch,
            rep,
            tool,
            placement_total,
            placement_correct_pct,
            placement_mismapped_pct,
            placement_unmapped_pct,
            placement_json,
            variant_bearing_reads,
            md_concordant_pct,
            nm_concordant_pct,
            by_class_json,
            meth_n_cpg,
            meth_pearson_r,
            meth_rmse,
        ),
    ).fetchone()
    if commit:
        conn.commit()
    return int(row[0])


def upsert_comparison(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    trial_id: int,
    kind: str,
    concordant: int,
    total: int,
    concordance_pct: float,
    by_class_json: str,
    supp_json: str | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO comparisons
            (trial_id, kind, concordant, total, concordance_pct, by_class_json, supp_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trial_id, kind) DO UPDATE SET
            concordant = excluded.concordant,
            total = excluded.total,
            concordance_pct = excluded.concordance_pct,
            by_class_json = excluded.by_class_json,
            supp_json = excluded.supp_json
        """,
        (trial_id, kind, concordant, total, concordance_pct, by_class_json, supp_json),
    )
    if commit:
        conn.commit()


def upsert_scaling(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    fg_labs_sha: str,
    sample: str,
    arch: str,
    threads: int,
    rep: int,
    wall_seconds: float | None,
    cpu_time: float | None,
    max_rss_mb: float | None,
    process_seconds: float | None,
    commit: bool = True,
) -> None:
    """Insert or update one rung of a thread-scaling ladder.

    Keyed on (sha, sample, arch, threads, rep) so re-ingesting a run is
    idempotent, matching how `upsert_trial` behaves for the standard sweep.
    """
    conn.execute(
        """
        INSERT INTO scaling
            (fg_labs_sha, sample, arch, threads, rep,
             wall_seconds, cpu_time, max_rss_mb, process_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fg_labs_sha, sample, arch, threads, rep) DO UPDATE SET
            wall_seconds = excluded.wall_seconds,
            cpu_time = excluded.cpu_time,
            max_rss_mb = excluded.max_rss_mb,
            process_seconds = excluded.process_seconds
        """,
        (
            fg_labs_sha,
            sample,
            arch,
            threads,
            rep,
            wall_seconds,
            cpu_time,
            max_rss_mb,
            process_seconds,
        ),
    )
    if commit:
        conn.commit()
