"""SQLite schema definitions for the benchmark.db."""

from __future__ import annotations

# Increment this whenever the schema changes in a backward-incompatible way.
SCHEMA_VERSION = 8

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
    -- The specific EC2 instance, not just its type. `instance_type` cannot
    -- identify a host -- every m7i trial shares `m7i.4xlarge` -- and the host is
    -- what varies: the same binary measured 18.89-25.01 s across reps on one
    -- (sample, arch), while two reps that shared an instance agreed to 0.36%.
    -- NULL for trials predating the IMDSv2 fix, whose meta.json has no host at
    -- all. See fg-labs/bwa-mem3-bench#56.
    instance_id         TEXT,
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
    -- JSON blob of compare-bams NON-PRIMARY divergence metrics: the
    -- supplementary axis (supp_*) and the secondary axis (sec_*), each carrying
    -- totals, count-mismatch templates, unmatched, matched, and content_diffs,
    -- plus total_templates. NULL for older rows / comparisons produced before
    -- compare-bams emitted them; the sec_*/matched/content_diffs members are
    -- likewise absent from rows written before those axes existed.
    -- NOTE: supp_unmatched's DEFINITION changed when the pairing key gained the
    -- read end, so values are not comparable across that boundary -- see
    -- NonPrimaryTally::unmatched in tools/compare-bams/src/report.rs.
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
    -- Phase breakdown from bwa-mem3's runtime profile. These EXPLAIN the
    -- efficiency number rather than just reporting it.
    --
    -- Critically, read_io_seconds lives INSIDE process_seconds. If FASTQ
    -- reading does not scale with thread count it becomes a large fraction of
    -- PROCESS() at high thread counts, so an efficiency computed from PROCESS()
    -- alone would attribute an IO limit to the aligner. Keeping the phases
    -- separate is what distinguishes those two cases.
    main_mem_seconds REAL,
    read_io_seconds  REAL,
    sam_io_seconds   REAL,
    kernel_seconds   REAL,
    -- The EC2 instance the whole ladder ran on. Constant across every row of a
    -- given (sha, sample, arch) by construction, and denormalised onto each row
    -- so a rung can be joined to `host_probes` without a second lookup.
    --
    -- The ladder had no host attribution at all before this column, which is why
    -- the v0.9.0 T(1) anomaly needed a same-day control run to settle: T(1) fell
    -- 17% across three releases whose diffs contain no SIMD, FMI or Makefile
    -- change, and nothing on record could say whether the three anchors were even
    -- comparable machines. NULL for ladders ingested before the rule emitted
    -- meta.json.
    instance_id      TEXT,
    UNIQUE (fg_labs_sha, sample, arch, threads, rep)
);

-- tachyon host-contention readings, one row per (run, sample, arch, phase).
--
-- WHY A SEPARATE TABLE. The reading describes the HOST, not the trial: many
-- trials (every rung of a ladder) share one instance and therefore one reading.
-- Denormalising it onto `trials`/`scaling` would repeat the same measurement
-- across rows and imply a per-row precision it does not have. Keyed by the cell
-- that produced it, with `instance_id` recorded as the join column to whatever
-- else ran on that machine.
--
-- WHAT THE SCORE IS FOR. `instance_id` says two runs used different hosts; it
-- cannot say one was starved of memory bandwidth by a co-tenant. That is the
-- mechanism actually observed on the v0.9.0 bless: cpu_seconds rose ~20% for
-- byte-identical work while all 16 vCPUs stayed present and mean_load stayed
-- flat, so no vCPU was lost — each core simply retired instructions more slowly.
--
-- USE IT TO FILTER, NEVER AS A DIVISOR. Normalising a wall time by this score
-- assumes probe and workload degrade proportionally, which is plausible and
-- unverified; and the probe is not purely a memory probe (per its own docs, 8
-- CPU-only spinners with no memory traffic still cost ~18% of the score).
-- Filtering only assumes a bad score means a bad host. See
-- fg-labs/bwa-mem3-bench#56.
CREATE TABLE IF NOT EXISTS host_probes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_labs_sha         TEXT NOT NULL REFERENCES runs(fg_labs_sha),
    sample              TEXT NOT NULL,
    arch                TEXT NOT NULL,
    -- 'pre' / 'post' — the probe runs either side of the timed work. A pair that
    -- agrees is evidence the work ran under stable conditions; a pair that
    -- diverges says the measurements it brackets are not comparable to each
    -- other, which no amount of within-run replication can detect.
    phase               TEXT NOT NULL,
    instance_id         TEXT,
    -- Provenance, because a score is only comparable to another score taken with
    -- the same probe. `probe_version` is what tachyon REPORTED (authoritative)
    -- rather than the version the image meant to install; `rustc` is captured at
    -- image build time because the runtime image has no Rust toolchain and
    -- `cargo +stable install` floats the compiler between rebuilds.
    probe_version       TEXT,
    rustc               TEXT,
    -- NULL when the probe could not run (`status` = 'unavailable'), which reads
    -- as "not measured" rather than as a fast host.
    m_accesses_per_sec  REAL,
    ns_per_access       REAL,
    threads             INTEGER,
    working_set_mb_per_thread REAL,
    seconds             REAL,
    status              TEXT,
    UNIQUE (fg_labs_sha, sample, arch, phase)
);

CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(fg_labs_sha);
CREATE INDEX IF NOT EXISTS idx_trials_sample_arch ON trials(sample, arch);
CREATE INDEX IF NOT EXISTS idx_accuracy_run ON accuracy(fg_labs_sha);
CREATE INDEX IF NOT EXISTS idx_scaling_run ON scaling(fg_labs_sha);
-- On instance_id, for the CROSS-CELL question: "what else ran on that machine, and
-- how contended was it", which is a join across runs. The within-cell question --
-- "what were this ladder's own readings" -- joins on
-- (fg_labs_sha, sample, arch) instead, served by the UNIQUE constraint above.
--
-- Keep the two apart. One instance can run more than one ladder, so joining a
-- ladder to its probes on instance_id ALONE cross-joins: measured on a
-- two-cell fixture it returned 8 rows where 4 are correct.
CREATE INDEX IF NOT EXISTS idx_host_probes_instance ON host_probes(instance_id);
"""
