"""Every upsert's ``ON CONFLICT`` clause must actually overwrite.

Each `upsert_*` in `bwa_mem3_bench.storage.sqlite` exists so that re-collecting a
run updates its rows in place. The tests that covered that re-ingested the
IDENTICAL input and asserted only a row count — which proves nothing was
APPENDED, not that a re-collection LANDS. Measured on the suite at the time
(fg-labs/bwa-mem3-bench#62): neutering a clause to a self-assignment of its own
key column left three of six with zero failures across 468 tests, `upsert_trial`
among them. That is the table holding `wall_seconds` — the benchmark measurement
itself — so a regression there would serve the FIRST collect's numbers forever,
indistinguishable from correct in every report we generate.

The mutation to check this with is a no-op UPDATE, not ``DO NOTHING``. Three of
the six upserts use ``RETURNING id`` (`upsert_trial`, `upsert_accuracy`,
`upsert_host_probe`), so ``DO NOTHING`` returns no row and the caller dies on
``TypeError`` — a loud crash the tests catch incidentally, for the wrong reason,
which hides which clauses are genuinely unasserted. A column dropped from the SET
is the regression that actually happens: `instance_id` was exactly that in #60.

Four invariants, the first three table-driven over one case per upsert:

1. every writable column is overwritten by a second upsert
   (`test_conflict_clause_overwrites_every_writable_column`),
2. the two payloads differ in every column, so 1 cannot be weakened into vacuity
   one value at a time (`test_the_two_payloads_differ_in_every_column`),
3. the payload covers every column the table has, so ADDING a column without
   adding it here fails (`test_every_column_of_the_table_is_covered`),
4. every `upsert_*` the module exports has a case at all
   (`test_every_upsert_in_the_storage_layer_has_a_case`).

3 and 4 are what make this outlive the commit that added it: both derive their
expectation from the code under test — ``PRAGMA table_info`` and the module's own
exports — rather than restating it, so neither can silently fall behind a new
column or a new upsert. Every invariant here was mutation-verified; where a
mutation turned out NOT to be caught, the reason is recorded at the test rather
than papered over (see `test_status_moves_when_a_tagless_upsert_carries_only_it`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bwa_mem3_bench.storage import sqlite as storage
from bwa_mem3_bench.storage.sqlite import (
    connect,
    upsert_accuracy,
    upsert_comparison,
    upsert_host_probe,
    upsert_run,
    upsert_scaling,
    upsert_trial,
)

SHA = "abc1234"
SAMPLE = "smoke-1M"
ARCH = "c7g"
REP = 1

# The autoincrement surrogate key, where a table has one. Never part of a
# payload: SQLite assigns it. `runs` (keyed on the SHA) and `comparisons` (keyed
# on trial_id + kind) have no such column.
_AUTO_ID = "id"


@dataclass(frozen=True)
class UpsertCase:
    """One upsert function, with two payloads that must differ in every field.

    ``prepare`` creates whatever rows the upsert's foreign keys require and
    returns the kwargs identifying the row — for `comparisons` that includes a
    ``trial_id`` that cannot be known until the trial exists.

    ``unwritten`` names columns the upsert never sets, which are therefore not
    expected in a payload. Keeping them explicit rather than filtering them out
    silently means adding a column and forgetting it in the SET still fails
    `test_every_column_of_the_table_is_covered`; only a deliberate entry here
    excuses one.
    """

    table: str
    upsert: Callable[..., Any]
    prepare: Callable[[sqlite3.Connection], dict[str, Any]]
    first: dict[str, Any]
    second: dict[str, Any]
    unwritten: frozenset[str] = field(default_factory=frozenset)


def _prepare_run(_conn: sqlite3.Connection) -> dict[str, Any]:
    return {"fg_labs_sha": SHA}


def _prepare_trial(conn: sqlite3.Connection) -> dict[str, Any]:
    upsert_run(conn, fg_labs_sha=SHA, status="complete")
    return {"fg_labs_sha": SHA, "sample": SAMPLE, "arch": ARCH, "rep": REP}


def _prepare_cell(conn: sqlite3.Connection) -> dict[str, Any]:
    """A `runs` row plus the (sha, sample, arch) a scaling/probe row hangs off."""
    upsert_run(conn, fg_labs_sha=SHA, status="complete")
    return {"fg_labs_sha": SHA, "sample": SAMPLE, "arch": ARCH}


def _prepare_comparison(conn: sqlite3.Connection) -> dict[str, Any]:
    """A comparison needs a real trial id, so the trial is created first."""
    trial_id = upsert_trial(conn, **_prepare_trial(conn), **_TRIAL_FIRST)
    return {"trial_id": trial_id, "kind": "vs_baseline"}


_TRIAL_FIRST: dict[str, Any] = {
    "instance_type": "c7g.4xlarge",
    "availability_zone": "us-east-1b",
    "instance_id": "i-0a6621e058441b4e6",
    "measured_at": "2026-08-01T00:00:00Z",
    "spot_price": 0.21,
    "wall_seconds": 12.3,
    "max_rss_mb": 1024.0,
    "cpu_time": 47.6,
    "io_read_mb": 200.0,
    "io_write_mb": 50.0,
    "mean_load": 380.0,
    "reads_processed": 1_000_000,
    "status": "ok",
    "process_seconds": 8.7,
    "index_read_seconds": 2.4,
}

# A re-collection of the same cell: a different host, a different day, and every
# measurement moved. Nothing is shared with the first payload -- enforced by
# `test_the_two_payloads_differ_in_every_column`.
_TRIAL_SECOND: dict[str, Any] = {
    "instance_type": "c8g.4xlarge",
    "availability_zone": "us-east-1d",
    "instance_id": "i-0b7732f169552c5f7",
    "measured_at": "2026-08-04T00:00:00Z",
    "spot_price": 0.19,
    "wall_seconds": 11.1,
    "max_rss_mb": 1100.0,
    "cpu_time": 44.2,
    "io_read_mb": 210.0,
    "io_write_mb": 55.0,
    "mean_load": 391.0,
    "reads_processed": 1_000_001,
    "status": "complete",
    "process_seconds": 7.9,
    "index_read_seconds": 2.1,
}

CASES: tuple[UpsertCase, ...] = (
    UpsertCase(
        table="runs",
        upsert=upsert_run,
        prepare=_prepare_run,
        first={"fg_labs_branch": "main", "upstream_tag": "v2.2.1", "status": "running"},
        second={"fg_labs_branch": "dev", "upstream_tag": "v2.3.0", "status": "complete"},
        # Assigned by SQLite's DEFAULT CURRENT_TIMESTAMP; the upsert never names
        # it, so a re-collect deliberately keeps the ORIGINAL submission time.
        unwritten=frozenset({"submitted_at"}),
    ),
    UpsertCase(
        table="trials",
        upsert=upsert_trial,
        prepare=_prepare_trial,
        first=_TRIAL_FIRST,
        second=_TRIAL_SECOND,
    ),
    UpsertCase(
        table="comparisons",
        upsert=upsert_comparison,
        prepare=_prepare_comparison,
        first={
            "concordant": 900,
            "total": 1000,
            "concordance_pct": 90.0,
            "by_class_json": '{"a": 1}',
            "supp_json": '{"supp_total": 1}',
        },
        second={
            "concordant": 995,
            "total": 1001,
            "concordance_pct": 99.4,
            "by_class_json": '{"a": 2}',
            "supp_json": '{"supp_total": 2}',
        },
    ),
    UpsertCase(
        table="accuracy",
        upsert=upsert_accuracy,
        prepare=lambda conn: {**_prepare_trial(conn), "tool": "fg-labs"},
        first={
            "placement_total": 100,
            "placement_correct_pct": 90.0,
            "placement_mismapped_pct": 5.0,
            "placement_unmapped_pct": 5.0,
            "placement_json": '{"a": 1}',
            "variant_bearing_reads": 10,
            "md_concordant_pct": 98.0,
            "nm_concordant_pct": 97.0,
            "by_class_json": '{"b": 1}',
            "meth_n_cpg": 1000,
            "meth_pearson_r": 0.90,
            "meth_rmse": 0.10,
        },
        second={
            "placement_total": 101,
            "placement_correct_pct": 95.0,
            "placement_mismapped_pct": 3.0,
            "placement_unmapped_pct": 2.0,
            "placement_json": '{"a": 2}',
            "variant_bearing_reads": 11,
            "md_concordant_pct": 99.0,
            "nm_concordant_pct": 98.5,
            "by_class_json": '{"b": 2}',
            "meth_n_cpg": 1001,
            "meth_pearson_r": 0.95,
            "meth_rmse": 0.05,
        },
    ),
    UpsertCase(
        table="scaling",
        upsert=upsert_scaling,
        prepare=lambda conn: {**_prepare_cell(conn), "threads": 16, "rep": REP},
        first={
            "wall_seconds": 93.1,
            "cpu_time": 1403.0,
            "max_rss_mb": 16520.0,
            "process_seconds": 91.0,
            "main_mem_seconds": 1.3,
            "read_io_seconds": 7.2,
            "sam_io_seconds": 3.1,
            "kernel_seconds": 48.3,
            "instance_id": "i-0a6621e058441b4e6",
        },
        second={
            "wall_seconds": 91.4,
            "cpu_time": 1399.0,
            "max_rss_mb": 16510.0,
            "process_seconds": 89.6,
            "main_mem_seconds": 1.1,
            "read_io_seconds": 7.0,
            "sam_io_seconds": 2.9,
            "kernel_seconds": 47.1,
            "instance_id": "i-0b7732f169552c5f7",
        },
    ),
    UpsertCase(
        table="host_probes",
        upsert=upsert_host_probe,
        prepare=lambda conn: {**_prepare_cell(conn), "phase": "pre"},
        first={
            "instance_id": "i-0a6621e058441b4e6",
            "probe_version": "0.1.0",
            "rustc": "rustc 1.97.1 (8bab26f4f 2026-07-14)",
            "m_accesses_per_sec": 28.4,
            "ns_per_access": 126.0,
            "threads": 64,
            "working_set_mb_per_thread": 64.0,
            "seconds": 10.0,
            "status": "ok",
        },
        second={
            "instance_id": "i-0b7732f169552c5f7",
            "probe_version": "0.2.0",
            "rustc": "rustc 1.98.0 (0000000000 2026-08-01)",
            "m_accesses_per_sec": 17.9,
            "ns_per_access": 222.0,
            "threads": 32,
            "working_set_mb_per_thread": 128.0,
            "seconds": 5.0,
            "status": "unavailable",
        },
    ),
)

_CASE_IDS = tuple(case.table for case in CASES)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "benchmark.db"


def _row(conn: sqlite3.Connection, case: UpsertCase, key: dict[str, Any]) -> sqlite3.Row:
    """The single row the case's key identifies, as a mapping by column name."""
    where = " AND ".join(f"{col} = ?" for col in key)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {case.table} WHERE {where}", tuple(key.values())).fetchall()
    assert len(rows) == 1, f"expected exactly one {case.table} row, got {len(rows)}"
    return rows[0]


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_conflict_clause_overwrites_every_writable_column(db_path: Path, case: UpsertCase) -> None:
    """A second upsert on the same key must land, column for column.

    The row count is asserted too, but it is the weaker half: it is what the old
    tests checked ALONE, and it passes under any conflict clause that does not
    insert a duplicate — including one that overwrites nothing.
    """
    conn = connect(db_path)
    key = case.prepare(conn)
    case.upsert(conn, **key, **case.first)
    case.upsert(conn, **key, **case.second)
    row = _row(conn, case, key)
    stored = {col: row[col] for col in case.second}
    assert stored == case.second
    conn.close()


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_the_two_payloads_differ_in_every_column(case: UpsertCase) -> None:
    """Guards the check above from going vacuous one column at a time.

    A payload column that repeats its first-write value asserts nothing: it reads
    as covered while proving only that the value did not spontaneously change.
    """
    assert set(case.first) == set(case.second)
    same = [col for col in case.second if case.first[col] == case.second[col]]
    assert not same, f"{case.table}: payloads must differ in every column, shared: {same}"


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_every_column_of_the_table_is_covered(db_path: Path, case: UpsertCase) -> None:
    """Adding a column must force it into a payload — or into ``unwritten``.

    This is what keeps the guarantee from decaying: without it, a column added to
    a table and forgotten in its ``DO UPDATE SET`` would be covered by nothing,
    which is the state three of these six tables were already in.
    """
    conn = connect(db_path)
    key = case.prepare(conn)
    # `id` is dropped rather than treated as covered: `runs` and `comparisons`
    # have no such column, so folding it into the covered set would make those
    # two compare unequal by exactly that name.
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({case.table})")} - {_AUTO_ID}
    covered = set(case.second) | set(key) | case.unwritten
    assert columns == covered, (
        f"{case.table}: uncovered {sorted(columns - covered)}, stale {sorted(covered - columns)}"
    )
    conn.close()


def test_every_upsert_in_the_storage_layer_has_a_case() -> None:
    """A NEW upsert must land here, not merely be free to.

    Coverage is only self-maintaining if the case list is checked against the
    module rather than against itself, so this counts `upsert_*` functions in
    `sqlite.py` — the same enumeration `#62` did by hand.
    """
    exported = {
        name
        for name in dir(storage)
        if name.startswith("upsert_") and callable(getattr(storage, name))
    }
    covered = {case.upsert.__name__ for case in CASES}
    assert exported == covered, f"upserts with no case: {sorted(exported - covered)}"


# --------------------------------------------------------------------------- #
# `upsert_run` is the one clause that is not a plain overwrite.
#
#     fg_labs_branch = COALESCE(excluded.fg_labs_branch, runs.fg_labs_branch)
#     upstream_tag   = COALESCE(excluded.upstream_tag,   runs.upstream_tag)
#     status         = excluded.status
#
# The asymmetry is load-bearing rather than incidental, and the case above cannot
# see it: it writes both payloads non-NULL, which is the path where COALESCE and
# a plain overwrite agree.
# --------------------------------------------------------------------------- #


def test_a_tagless_upsert_preserves_run_metadata(db_path: Path) -> None:
    """`ingest_scaling` upserts the run with no branch and no tag.

    A ladder is routinely ingested for a SHA whose standard sweep already ran, so
    if either column overwrote unconditionally, collecting a ladder would erase
    the sweep's provenance. Dropping either COALESCE, or swapping its arms, is
    silent without this.
    """
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=SHA, fg_labs_branch="main", upstream_tag="v2.2.1")
    upsert_run(conn, fg_labs_sha=SHA, status="complete")
    row = conn.execute(
        "SELECT fg_labs_branch, upstream_tag FROM runs WHERE fg_labs_sha = ?", (SHA,)
    ).fetchone()
    assert row == ("main", "v2.2.1")
    conn.close()


def test_status_moves_when_a_tagless_upsert_carries_only_it(db_path: Path) -> None:
    """A run that went `running` → `complete` must not be pinned at the first value.

    Note what this does NOT prove. Wrapping `status` in a COALESCE for symmetry
    with its two neighbours would be the natural-looking mistake, but it is
    behaviour-preserving here and this test stays green through it: the parameter
    is `status: str = "complete"`, so `excluded.status` is never NULL and the
    COALESCE never selects its second arm. Only widening the parameter to accept
    None would make that mutation reachable — at which point this needs a case
    for it. Verified by mutation rather than assumed.

    What it does pin is that `status` is in the SET at all, on the tagless path
    where it is the only column carrying a value.
    """
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=SHA, fg_labs_branch="main", status="running")
    upsert_run(conn, fg_labs_sha=SHA, status="complete")
    (status,) = conn.execute("SELECT status FROM runs WHERE fg_labs_sha = ?", (SHA,)).fetchone()
    assert status == "complete"
    conn.close()
