"""SQLite schema definitions for the benchmark.db."""

from __future__ import annotations

# Increment this whenever the schema changes in a backward-incompatible way.
SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA user_version = 3;

CREATE TABLE IF NOT EXISTS runs (
    fg_labs_sha     TEXT PRIMARY KEY,
    fg_labs_branch  TEXT,
    upstream_tag    TEXT,
    submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS trials (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_labs_sha         TEXT NOT NULL REFERENCES runs(fg_labs_sha),
    sample              TEXT NOT NULL,
    arch                TEXT NOT NULL,
    rep                 INTEGER NOT NULL,
    instance_type       TEXT,
    availability_zone   TEXT,
    spot_price          REAL,
    wall_seconds        REAL,
    max_rss_mb          REAL,
    cpu_time            REAL,
    io_read_mb          REAL,
    io_write_mb         REAL,
    mean_load           REAL,
    reads_processed     INTEGER,
    -- Parsed from bwa-mem2's stderr profiling output. process_seconds is
    -- the value of `PROCESS() (Total compute time + (read + SAM) IO time)`
    -- and excludes index loading, making it the apples-to-apples kernel
    -- speedup metric. index_read_seconds is `Index read time avg`,
    -- diagnostic for warm-vs-cold cache behavior. NULL when bwa.stderr.log
    -- is missing or unparseable.
    process_seconds     REAL,
    index_read_seconds  REAL,
    status              TEXT,
    UNIQUE (fg_labs_sha, sample, arch, rep)
);

CREATE TABLE IF NOT EXISTS comparisons (
    trial_id         INTEGER NOT NULL REFERENCES trials(id),
    kind             TEXT NOT NULL,
    concordant       INTEGER,
    total            INTEGER,
    concordance_pct  REAL,
    by_class_json    TEXT,
    -- JSON blob of compare-bams supplementary-disagreement metrics
    -- (supp_count_mismatch_pct, supp_unmatched_pct, totals, total_templates).
    -- NULL for older rows / comparisons produced before compare-bams emitted them.
    supp_json        TEXT,
    UNIQUE (trial_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(fg_labs_sha);
CREATE INDEX IF NOT EXISTS idx_trials_sample_arch ON trials(sample, arch);
"""
