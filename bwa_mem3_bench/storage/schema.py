"""SQLite schema definitions for the benchmark.db."""

from __future__ import annotations

# Increment this whenever the schema changes in a backward-incompatible way.
SCHEMA_VERSION = 5

# `user_version` is INTERPOLATED from SCHEMA_VERSION, never written literally.
# It used to be hardcoded, so bumping SCHEMA_VERSION without editing the PRAGMA
# left the DB stamped with the old number while the code believed it was current
# — migrations would then either re-run or be skipped depending on direction.
SCHEMA_SQL = f"""
PRAGMA user_version = {SCHEMA_VERSION};

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

-- Truth-based accuracy results (holodeck eval), one row per
-- (run, sim-sample, arch, rep, aligner arm). Distinct from `comparisons`,
-- which is tool-vs-tool agreement; this is graded against simulation truth.
-- `tool` is the aligner arm (`fg-labs`, `baseline`, `minibwa`); all arms of a
-- run share the run's `fg_labs_sha` (the eval outputs all live under
-- runs/<sha>/), so `tool` disambiguates them.
CREATE TABLE IF NOT EXISTS accuracy (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_labs_sha             TEXT NOT NULL REFERENCES runs(fg_labs_sha),
    sample                  TEXT NOT NULL,
    arch                    TEXT NOT NULL,
    rep                     INTEGER NOT NULL,
    tool                    TEXT NOT NULL,
    -- Placement + MAPQ calibration (<tool>.eval.txt). Headline ALL-bin rates as
    -- columns; the full per-MAPQ-bin table as JSON in placement_json.
    placement_total         INTEGER,
    placement_correct_pct   REAL,
    placement_mismapped_pct REAL,
    placement_unmapped_pct  REAL,
    placement_json          TEXT,
    -- Per-read variant representation (<tool>.variants.tsv). Per-class
    -- accumulators as JSON in by_class_json; the footer concordance stats as
    -- columns (NULL when holodeck wrote "NA" — no comparable golden tag).
    variant_bearing_reads   INTEGER,
    md_concordant_pct       REAL,
    nm_concordant_pct       REAL,
    by_class_json           TEXT,
    -- Methylation-level correlation (<tool>.meth.tsv; meth samples only —
    -- NULL columns for non-meth, whose .meth.tsv is an empty placeholder).
    meth_n_cpg              INTEGER,
    meth_pearson_r          REAL,
    meth_rmse               REAL,
    UNIQUE (fg_labs_sha, sample, arch, rep, tool)
);

-- Thread-scaling ladder (workflow/rules/scaling.smk), one row per
-- (sha, sample, arch, threads, rep).
--
-- A SEPARATE table rather than a `threads` column on trials: trials is
-- UNIQUE(fg_labs_sha, sample, arch, rep), and the ladder runs the same sample
-- and arch at several thread counts within one rep, which that constraint
-- forbids. Keeping it separate also keeps `trials` meaning "one row per
-- benchmark cell at the standard thread count", which every existing report
-- assumes.
--
-- Every row of a given (sha, sample, arch) comes from ONE Batch job on ONE
-- host, by construction — see the rule's docstring for why efficiency is only
-- meaningful measured that way.
CREATE TABLE IF NOT EXISTS scaling (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_labs_sha     TEXT NOT NULL REFERENCES runs(fg_labs_sha),
    sample          TEXT NOT NULL,
    arch            TEXT NOT NULL,
    threads         INTEGER NOT NULL,
    rep             INTEGER NOT NULL,
    wall_seconds    REAL,
    cpu_time        REAL,
    max_rss_mb      REAL,
    -- The aligner's own PROCESS() compute time, parsed with the same regex as
    -- trials.process_seconds so the two are directly comparable.
    process_seconds REAL,
    UNIQUE (fg_labs_sha, sample, arch, threads, rep)
);

CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(fg_labs_sha);
CREATE INDEX IF NOT EXISTS idx_trials_sample_arch ON trials(sample, arch);
CREATE INDEX IF NOT EXISTS idx_accuracy_run ON accuracy(fg_labs_sha);
CREATE INDEX IF NOT EXISTS idx_scaling_run ON scaling(fg_labs_sha);
"""
