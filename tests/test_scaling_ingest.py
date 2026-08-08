"""Ingest tests for the thread-scaling ladder."""

from __future__ import annotations

import json
from pathlib import Path

from bwa_mem3_bench.storage.ingest import ingest_scaling
from bwa_mem3_bench.storage.sqlite import connect

LADDER_ROWS = 4  # rungs in the fixture below: 1, 16x2, 64
PROCESS_16_REP1 = 91.0

SAMPLE = "wgs-5M"
ARCH = "c8g64"
# A second cell for the scoping test. One instance can run more than one ladder,
# which is exactly why a join must not key on the host alone.
OTHER_SAMPLE = "wes-5M"

INSTANCE_ID = "i-0a6621e058441b4e6"
# Where a re-collected ladder landed the second time: a re-run gets a fresh spot
# host, so the recorded attribution has to follow it rather than stick.
OTHER_INSTANCE_ID = "i-0b7732f169552c5f7"
PROBE_PHASES = 2
# 64 MB per thread, tachyon's default, as the JSONL carries it (bytes).
PROBE_WORKING_SET_BYTES = 67108864
PROBE_WORKING_SET_MB = 64.0
PROBE_PRE_RATE = 28.4
PROBE_POST_RATE = 17.9
# What a re-collection of the same cell reports, so an in-place update is
# distinguishable from a no-op that merely leaves the first values behind.
PROBE_POST_RATE_RECOLLECTED = 21.3

WALL_64 = 28.0
# What the top rung reports when the cell is re-collected. Only a CHANGED value
# can tell an in-place update from an `ON CONFLICT DO NOTHING`.
WALL_64_RECOLLECTED = 26.4


def _ladder(*, wall_64: float = WALL_64) -> str:
    """A ladder TSV: rungs at 1, 16 (two reps) and 64 threads.

    Parameterised on the one value a re-collection changes, so the two variants
    cannot drift apart in any other field.
    """
    return (
        "threads\trep\twall_s\tcpu_s\tmax_rss_mb\tprocess_s\n"
        "1\t1\t1400.0\t1395.0\t16500.0\t1390.2\n"
        "16\t1\t93.1\t1403.0\t16520.0\t91.0\n"
        "16\t2\t93.5\t1405.0\t16521.0\t91.4\n"
        f"64\t1\t{wall_64}\t1600.0\t16800.0\tNA\n"
    )


LADDER = _ladder()


def _cell(root: Path, sha: str, sample: str = SAMPLE, arch: str = ARCH) -> Path:
    """A ladder cell. Defaults to the one nearly every fixture here writes into.

    Extracted because the literal appeared in seven places, so renaming the sample
    or arch meant seven edits and six chances to miss one. The parameters exist so
    a test can build a SECOND cell — needed to prove that probes stay scoped to
    their own cell when two of them share a host.
    """
    return root / sha / sample / arch


def _write_ladder(
    root: Path, sha: str, text: str = LADDER, sample: str = SAMPLE, arch: str = ARCH
) -> None:
    cell = _cell(root, sha, sample, arch)
    # exist_ok so a test can re-write a cell it already wrote, which is what a
    # re-collection of the same run looks like on disk.
    cell.mkdir(parents=True, exist_ok=True)
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
    assert rows == [(1, 1, 1400.0), (16, 1, 93.1), (16, 2, 93.5), (64, 1, WALL_64)]


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
    """Re-collecting a run must update rows in place, not duplicate them.

    The second collect carries a changed wall time, the way a genuine re-run
    looks. Ingesting the IDENTICAL ladder twice would pass under an
    `ON CONFLICT DO NOTHING` as well — it proves only that nothing was appended,
    not that a re-collection actually lands.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    _write_ladder(root, "abc123", text=_ladder(wall_64=WALL_64_RECOLLECTED))
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    (n,) = conn.execute("select count(*) from scaling").fetchone()
    assert n == LADDER_ROWS
    (wall,) = conn.execute("select wall_seconds from scaling where threads=64 and rep=1").fetchone()
    assert wall == WALL_64_RECOLLECTED


def test_missing_ladder_is_not_an_error(tmp_path: Path) -> None:
    """Runs that never requested thread_scaling must ingest cleanly as zero."""
    (tmp_path / "scaling").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=tmp_path / "scaling", fg_labs_sha="nosuchsha") == 0


# --------------------------------------------------------------------------- #
# Host attribution: which machine the ladder ran on, and how contended it was.
#
# The ladder had neither until schema v8. That absence is why the v0.9.0 T(1)
# anomaly needed a same-day control run to settle -- T(1) fell 17% across three
# releases with no SIMD/FMI/Makefile change between them, and nothing on record
# could say whether the three anchors were even comparable machines.
# --------------------------------------------------------------------------- #


def _write_meta(
    root: Path,
    sha: str,
    instance_id: str = INSTANCE_ID,
    sample: str = SAMPLE,
    arch: str = ARCH,
) -> None:
    cell = _cell(root, sha, sample, arch)
    (cell / "meta.json").write_text(
        json.dumps(
            {
                "fg_labs_sha": sha,
                "sample": sample,
                "arch": arch,
                "rep": 0,
                "instance_type": "c8g.16xlarge",
                "availability_zone": "us-east-1b",
                "instance_id": instance_id,
                "kernel": "6.1.0",
            }
        )
    )


def _probe_records(*, post_rate: float = PROBE_POST_RATE) -> list[dict[str, object]]:
    """The pair of readings a healthy ladder emits, one per phase.

    ``post_rate`` is a knob so a test can re-collect a cell with a CHANGED
    reading: ingesting the identical pair twice cannot tell an upsert from an
    ``ON CONFLICT DO NOTHING``, since both leave the right values in place.
    """
    return [
        {
            "phase": "pre",
            "status": "ok",
            "million_accesses_per_sec": PROBE_PRE_RATE,
            "ns_per_access": 126.0,
            "threads": 64,
            "working_set_bytes_per_thread": PROBE_WORKING_SET_BYTES,
            "seconds": 10.0,
            "probe_version": "0.1.0",
            "rustc": "rustc 1.97.1 (8bab26f4f 2026-07-14)",
        },
        {
            "phase": "post",
            "status": "ok",
            "million_accesses_per_sec": post_rate,
            "ns_per_access": 222.0,
            "threads": 64,
            "working_set_bytes_per_thread": PROBE_WORKING_SET_BYTES,
            "seconds": 10.0,
            "probe_version": "0.1.0",
            "rustc": "rustc 1.97.1 (8bab26f4f 2026-07-14)",
        },
    ]


def _write_probes(
    root: Path,
    sha: str,
    *,
    records: list[dict[str, object]] | None = None,
    sample: str = SAMPLE,
    arch: str = ARCH,
) -> None:
    cell = _cell(root, sha, sample, arch)
    if records is None:
        records = _probe_records()
    (cell / "host-probe.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


def test_ladder_rungs_record_the_host_they_ran_on(tmp_path: Path) -> None:
    """Every rung carries the instance id, since one job means one machine."""
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_meta(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    ids = conn.execute("select distinct instance_id from scaling").fetchall()
    assert ids == [(INSTANCE_ID,)]


def test_a_recollected_ladder_records_the_host_it_actually_ran_on(tmp_path: Path) -> None:
    """A re-run lands on a fresh spot host, and the attribution must follow it.

    `upsert_scaling` overwrites `instance_id` on conflict for exactly this case.
    Were it left out of the update set, the rungs would keep pointing at the
    machine of the FIRST collect — attribution that looks authoritative and names
    the wrong host, which is worse than none at all.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_meta(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    _write_meta(root, "abc123", instance_id=OTHER_INSTANCE_ID)
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    ids = conn.execute("select distinct instance_id from scaling").fetchall()
    assert ids == [(OTHER_INSTANCE_ID,)]


def test_unknown_instance_id_becomes_null_not_the_sentinel(tmp_path: Path) -> None:
    """`unknown` must not be stored verbatim: it would JOIN to other unknowns.

    Two ladders that both failed to reach IMDS would then match each other and
    claim they shared a machine — the exact opposite of the truth, and worse than
    no attribution because it looks like data. NULL never equals NULL in SQL, so
    an unattributed row correctly fails to join instead.

    Asserted on BOTH new columns. They are written by two different upserts, and
    the false-join hazard is the same on either side, so pinning only `scaling`
    would leave a probe row free to reintroduce the sentinel.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_meta(root, "abc123", instance_id="unknown")
    _write_probes(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    ids = conn.execute("select distinct instance_id from scaling").fetchall()
    assert ids == [(None,)]
    probe_ids = conn.execute("select distinct instance_id from host_probes").fetchall()
    assert probe_ids == [(None,)]


def test_host_probes_are_ingested_with_provenance(tmp_path: Path) -> None:
    """A score is only comparable to another taken with the same probe build."""
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_meta(root, "abc123")
    _write_probes(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    rows = conn.execute(
        "select phase, m_accesses_per_sec, working_set_mb_per_thread, probe_version, "
        "rustc, instance_id, status from host_probes order by phase"
    ).fetchall()
    assert len(rows) == PROBE_PHASES
    post, pre = rows  # alphabetical: post, pre
    assert pre[0] == "pre" and pre[1] == PROBE_PRE_RATE
    assert post[0] == "post" and post[1] == PROBE_POST_RATE
    # Stored in MB, the unit the probe's own guidance reasons in.
    assert pre[2] == PROBE_WORKING_SET_MB
    assert pre[3] == "0.1.0"
    assert pre[4].startswith("rustc ")
    # The reading is joinable to the rungs it brackets.
    assert pre[5] == INSTANCE_ID
    assert pre[6] == "ok"


def test_probe_readings_join_to_the_rungs_they_bracket(tmp_path: Path) -> None:
    """The point of recording instance_id on both: one query, no manual lookup.

    Attributing the v0.9.0 perf finding required pulling meta.json out of S3 by
    hand, one object at a time. This is that query — which is why it has to be the
    RIGHT query: joining on `instance_id` alone cross-joins, because one instance
    can run more than one ladder and both tables are keyed per cell. The fixture
    builds a second cell on the same host to prove the scoping holds.

    `instance_id` stays in the join as well as the cell key: it is redundant for
    correctness once the cell matches, but it states the invariant the rows are
    supposed to satisfy, so a future ingest bug that attributed a probe to the
    wrong host would surface here as a missing row rather than as a silent
    mismatch.
    """
    root = tmp_path / "scaling"
    for sample in (SAMPLE, OTHER_SAMPLE):
        _write_ladder(root, "abc123", sample=sample)
        _write_meta(root, "abc123", sample=sample)
        _write_probes(root, "abc123", sample=sample)
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")

    # Both cells landed on the same instance, so a host-only join would return
    # each rung's probes twice over.
    hosts = conn.execute("select distinct instance_id from host_probes").fetchall()
    assert hosts == [(INSTANCE_ID,)]

    rows = conn.execute(
        """
        SELECT s.threads, p.phase, p.m_accesses_per_sec
        FROM scaling s JOIN host_probes p
          USING (fg_labs_sha, sample, arch, instance_id)
        WHERE s.threads = 1 AND s.sample = ?
        ORDER BY p.phase
        """,
        (SAMPLE,),
    ).fetchall()
    assert rows == [(1, "post", PROBE_POST_RATE), (1, "pre", PROBE_PRE_RATE)]


def test_unavailable_probe_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    """ "Tried and could not measure" is a different fact from "never looked".

    Only a row with null measurements distinguishes them; dropping the record
    would make a probe-less image indistinguishable from a probe that ran.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_probes(
        root,
        "abc123",
        records=[
            {
                "phase": "pre",
                "status": "unavailable",
                "million_accesses_per_sec": None,
                "ns_per_access": None,
                "threads": None,
                "seconds": None,
                "probe_version": None,
                "rustc": None,
            }
        ],
    )
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    rows = conn.execute("select status, m_accesses_per_sec from host_probes").fetchall()
    assert rows == [("unavailable", None)]


def test_a_malformed_probe_line_does_not_lose_the_ladder(tmp_path: Path) -> None:
    """The readings are diagnostic; a bad one must not cost us the measurements."""
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    cell = _cell(root, "abc123")
    (cell / "host-probe.jsonl").write_text(
        'not json at all\n\n{"phase": "post", "status": "ok", '
        '"million_accesses_per_sec": 17.9}\n{"status": "ok"}\n'
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    # The unparseable line and the phase-less record are skipped; the good one lands.
    rows = conn.execute("select phase from host_probes").fetchall()
    assert rows == [("post",)]


def test_a_well_formed_record_with_a_wrong_typed_field_does_not_lose_the_ladder(
    tmp_path: Path,
) -> None:
    """Valid JSON is not enough — the FIELD types have to be checked too.

    `{"threads": {}}` decodes fine, so a JSON-only guard passes it straight to
    SQLite, where parameter binding raises `ProgrammingError`. That propagates out
    of `ingest_scaling` and persists ZERO rungs of a ~45-minute ladder. Measured
    before the fix: `Error binding parameter 10: type 'dict' is not supported`,
    with 0 scaling rows written.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_probes(
        root,
        "abc123",
        records=[
            {
                "phase": "pre",
                "status": "ok",
                "million_accesses_per_sec": PROBE_PRE_RATE,
                "threads": {},  # the field that used to abort the ingest
                "ns_per_access": ["nope"],
                "working_set_bytes_per_thread": {"a": 1},
                "probe_version": {"v": 1},
                "rustc": ["rustc"],
                "seconds": 10.0,
            }
        ],
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    # The usable fields survive; each unusable one is NULL rather than fatal.
    row = conn.execute(
        "select phase, m_accesses_per_sec, threads, ns_per_access, "
        "working_set_mb_per_thread, probe_version, rustc, seconds from host_probes"
    ).fetchone()
    assert row == ("pre", PROBE_PRE_RATE, None, None, None, None, None, 10.0)


def test_non_finite_and_bool_probe_values_are_rejected(tmp_path: Path) -> None:
    """`json.loads` accepts NaN/Infinity, and bools are ints in Python.

    SQLite silently stores NaN as NULL but keeps inf as inf, so an unchecked
    non-finite value would persist as a number it never was. A bool would store as
    1, which is indistinguishable from a real single-threaded reading.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    cell = _cell(root, "abc123")
    # Written as raw text: json.dumps would emit these same literals, but spelling
    # them out shows exactly what a producer could hand us.
    (cell / "host-probe.jsonl").write_text(
        '{"phase": "pre", "status": "ok", "million_accesses_per_sec": Infinity, '
        '"ns_per_access": NaN, "threads": true, "seconds": 10.0}\n'
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    row = conn.execute(
        "select phase, m_accesses_per_sec, ns_per_access, threads, seconds from host_probes"
    ).fetchone()
    assert row == ("pre", None, None, None, 10.0)


def test_an_out_of_range_integer_does_not_lose_the_ladder(tmp_path: Path) -> None:
    """A JSON integer has no width limit, and both conversion paths raise.

    Type-checking alone is not enough. `math.isfinite(huge_int)` raises
    `OverflowError: int too large to convert to float`, and handing a huge int to
    SQLite raises `OverflowError: Python int too large to convert to SQLite
    INTEGER`. Either aborts `ingest_scaling` and persists ZERO rungs — the same
    outcome as the wrong-type case, through a different door. Measured with a
    400-digit value before the fix: 0 scaling rows, twice.

    Floats need no such guard: `json.loads` maps an over-large literal to `inf`,
    which the finiteness check already rejects.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    cell = _cell(root, "abc123")
    huge = "9" * 400
    (cell / "host-probe.jsonl").write_text(
        f'{{"phase": "pre", "status": "ok", "million_accesses_per_sec": {huge}, '
        f'"threads": {huge}, "ns_per_access": -{huge}, "seconds": 10.0}}\n'
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    row = conn.execute(
        "select phase, m_accesses_per_sec, threads, ns_per_access, seconds from host_probes"
    ).fetchone()
    assert row == ("pre", None, None, None, 10.0)


def test_an_integer_just_past_sqlite_range_is_rejected(tmp_path: Path) -> None:
    """The boundary itself: 2**63-1 stores, 2**63 does not.

    A magnitude guard is easy to write one off — and the off-by-one direction
    that matters is the permissive one, which raises on bind rather than storing
    NULL.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_probes(
        root,
        "abc123",
        records=[
            {"phase": "pre", "status": "ok", "threads": 2**63 - 1},
            {"phase": "post", "status": "ok", "threads": 2**63},
        ],
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    rows = dict(conn.execute("select phase, threads from host_probes").fetchall())
    assert rows == {"pre": 2**63 - 1, "post": None}


def test_a_json_list_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A top-level array decodes fine but has no `.get`; it must not raise."""
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    cell = _cell(root, "abc123")
    (cell / "host-probe.jsonl").write_text('["phase", "pre"]\n"just a string"\n7\n')
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    (n,) = conn.execute("select count(*) from host_probes").fetchone()
    assert n == 0


def test_a_non_string_phase_is_skipped(tmp_path: Path) -> None:
    """`phase` is the row identity and its column is NOT NULL.

    A truthy non-string (`{"a": 1}`) previously reached the upsert via `str(phase)`,
    which would have persisted the literal `"{'a': 1}"` as a phase name.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_probes(
        root,
        "abc123",
        records=[
            {"phase": {"a": 1}, "status": "ok", "million_accesses_per_sec": 1.0},
            {"phase": 7, "status": "ok", "million_accesses_per_sec": 1.0},
            {"phase": "post", "status": "ok", "million_accesses_per_sec": PROBE_POST_RATE},
        ],
    )
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    rows = conn.execute("select phase from host_probes").fetchall()
    assert rows == [("post",)]


def test_ladder_without_the_new_artifacts_still_ingests(tmp_path: Path) -> None:
    """Ladders collected before meta.json / host-probe.jsonl existed must load.

    Every historical ladder in S3 is in exactly this shape, so a hard requirement
    here would make old runs un-reingestable.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    assert ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123") == LADDER_ROWS
    ids = conn.execute("select distinct instance_id from scaling").fetchall()
    assert ids == [(None,)]
    (n,) = conn.execute("select count(*) from host_probes").fetchone()
    assert n == 0


def test_host_probe_ingest_is_idempotent(tmp_path: Path) -> None:
    """Re-collecting must update the reading in place, not append a second one.

    The second ingest deliberately carries a DIFFERENT post reading. Feeding the
    identical pair twice would leave the right values in place under an
    ``ON CONFLICT DO NOTHING`` too, so it proves only that nothing was appended —
    not that a re-collection actually lands. A ladder re-run on the same host
    after a fix is exactly the case that must overwrite.
    """
    root = tmp_path / "scaling"
    _write_ladder(root, "abc123")
    _write_meta(root, "abc123")
    _write_probes(root, "abc123")
    conn = connect(tmp_path / "db.sqlite")
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    _write_probes(root, "abc123", records=_probe_records(post_rate=PROBE_POST_RATE_RECOLLECTED))
    ingest_scaling(conn, scaling_root=root, fg_labs_sha="abc123")
    (n,) = conn.execute("select count(*) from host_probes").fetchone()
    assert n == PROBE_PHASES
    # The count alone proves only that no row was APPENDED. `upsert_host_probe`
    # overwrites every non-key column on conflict, so a regression that wrote NULL
    # over a good reading would keep the count at 2 and pass unnoticed — as would
    # one that stopped writing at all and left the first ingest's values behind.
    rows = conn.execute(
        "select phase, m_accesses_per_sec, instance_id from host_probes order by phase"
    ).fetchall()
    assert rows == [
        ("post", PROBE_POST_RATE_RECOLLECTED, INSTANCE_ID),
        ("pre", PROBE_PRE_RATE, INSTANCE_ID),
    ]
