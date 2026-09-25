"""Tests for the published results pages (`bench results`).

The database is built programmatically: one fake release with a handful of
datasets across an x86 and an Arm instance, an upstream baseline, an arena and
a thread-scaling ladder -- enough to exercise every page.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.release_allowances import ReleaseAllowance
from bwa_mem3_bench.report import results
from bwa_mem3_bench.report.results_data import (
    DATASETS,
    build_snapshot,
    release_label_to_version,
)
from bwa_mem3_bench.storage.ingest import baseline_sha_for
from bwa_mem3_bench.storage.sqlite import (
    connect,
    upsert_accuracy,
    upsert_arena,
    upsert_comparison,
    upsert_run,
    upsert_scaling,
    upsert_trial,
)
from bwa_mem3_bench.workflow_config import WorkflowConfig, load_config

PREV_SHA = "a" * 40
SHA = "b" * 40
INSTANCE_ID = "i-0123456789abcdef0"
AZ = "us-east-1b"


@pytest.fixture(scope="module")
def config() -> WorkflowConfig:
    return load_config(REPO_ROOT / "config")


def _allowances() -> list[ReleaseAllowance]:
    return [
        ReleaseAllowance(PREV_SHA, "fg-labs/bwa-mem3#1", "2026-01-01", "prev", 0.0),
        ReleaseAllowance(SHA, "fg-labs/bwa-mem3#2", "2026-02-01", "this", 0.0, ("c" * 40,)),
    ]


def _trial(  # noqa: PLR0913
    conn: sqlite3.Connection, sha: str, sample: str, arch: str, rep: int, wall: float
) -> int:
    return upsert_trial(
        conn,
        fg_labs_sha=sha,
        sample=sample,
        arch=arch,
        rep=rep,
        wall_seconds=wall,
        max_rss_mb=16 * 1024.0,
        cpu_time=wall * 16 * 0.9,
        io_read_mb=0.0,
        io_write_mb=0.0,
        mean_load=0.0,
        reads_processed=10_000_000,
        instance_type="c6a.4xlarge",
        availability_zone=AZ,
        spot_price=0.1234,
        instance_id=INSTANCE_ID,
        status="ok",
        process_seconds=wall - 1,
    )


def _compare(  # noqa: PLR0913
    conn: sqlite3.Connection,
    trial_id: int,
    kind: str,
    pct: float,
    by_class: dict[str, Any] | None = None,
    placement: dict[str, Any] | None = None,
) -> None:
    upsert_comparison(
        conn,
        trial_id=trial_id,
        kind=kind,
        concordant=int(pct * 100),
        total=10_000,
        concordance_pct=pct,
        by_class_json=json.dumps(by_class or {}),
        placement_json=json.dumps(placement) if placement else None,
    )


def _seed(db: Path, *, meth_placement: bool = True) -> None:  # noqa: PLR0915
    conn = connect(db)
    for sha in (SHA, baseline_sha_for("v2.2.1")):
        upsert_run(conn, fg_labs_sha=sha)
    # wgs-5M: 3 reps on x86 (c6a) and Arm (c8g); walls 10/11/30 so the median
    # (11) differs from both the min (10) and the mean (17).
    for arch in ("c6a", "c8g"):
        for rep, wall in enumerate((10.0, 11.0, 30.0), start=1):
            tid = _trial(conn, SHA, "wgs-5M", arch, rep, wall)
            _compare(conn, tid, "vs-golden", 100.0)
            if arch == "c6a":
                _compare(conn, tid, "vs-baseline", 100.0)
            else:
                _compare(conn, tid, "vs-x86", 100.0)
            fid = _trial(conn, SHA, "wgs-5M-fast", arch, rep, wall / 2)
            _compare(
                conn,
                fid,
                "vs-default",
                94.0,
                {"pos_diff": {"count": 300, "pct": 3.0}, "tag_diff": {"count": 200, "pct": 2.0}},
            )
    for rep in (1, 2, 3):
        _trial(conn, baseline_sha_for("v2.2.1"), "wgs-5M", "c6a", rep, 22.0)
    # meth: m7i only, one rep.
    tid = _trial(conn, SHA, "meth-twist-emseq-5M", "m7i", 1, 50.0)
    _compare(
        conn,
        tid,
        "vs-baseline",
        72.0,
        placement={"relocated_pct": 0.126} if meth_placement else None,
    )
    # arena: one arch, candidate + one release + comparators.
    for label, mode, wall in (
        ("fg-labs-default", "default", 40.0),
        ("fg-labs-fast", "fast", 20.0),
        ("v110", "default", 50.0),
        ("v110-fast", "fast", 25.0),
        ("bwa", "default", 200.0),
        ("bwa-mem2-upstream", "default", 100.0),
        ("minibwa", "default", 32.0),
    ):
        for rep in (1, 2, 3):
            upsert_arena(
                conn,
                fg_labs_sha=SHA,
                arch="m8a",
                label=label,
                mode=mode,
                rep=rep,
                wall_seconds=wall,
                cpu_time=wall * 16,
                max_rss_mb=8 * 1024.0,
                process_seconds=wall - 1,
                instance_id=INSTANCE_ID,
            )
    for threads, t in ((1, 64.0), (2, 32.0), (4, 20.0)):
        upsert_scaling(
            conn,
            fg_labs_sha=SHA,
            sample="wgs-5M",
            arch="c8g64",
            threads=threads,
            rep=1,
            wall_seconds=t + 1,
            cpu_time=None,
            max_rss_mb=None,
            process_seconds=t,
        )
    bins = {
        "0": {"total": 10, "correct": 4, "mismapped": 6},
        "20-29": {"total": 40, "correct": 39, "mismapped": 1},
        "60+": {"total": 50, "correct": 50, "mismapped": 0},
    }
    for tool in ("fg-labs", "baseline", "minibwa"):
        upsert_accuracy(
            conn,
            fg_labs_sha=SHA,
            sample="sim-wgs-place",
            arch="c6a",
            rep=1,
            tool=tool,
            placement_total=100,
            placement_correct_pct=93.0,
            placement_mismapped_pct=7.0,
            placement_unmapped_pct=0.0,
            placement_json=json.dumps({"bins": bins}),
            variant_bearing_reads=10,
            md_concordant_pct=95.0,
            nm_concordant_pct=99.0,
            by_class_json=None,
            meth_n_cpg=None,
            meth_pearson_r=None,
            meth_rmse=None,
        )
    conn.close()


def _snapshot(db: Path, config: WorkflowConfig, version: str = "v0.13.0") -> dict[str, Any]:
    return build_snapshot(
        db_path=db,
        fg_labs_sha=SHA,
        version=version,
        previous_version="v0.12.0",
        allowances=_allowances(),
        config=config,
        sweep_sa_stride=4,
        arena_sa_stride=2,
    )


@pytest.fixture
def snap(tmp_path: Path, config: WorkflowConfig) -> dict[str, Any]:
    db = tmp_path / "bench.db"
    _seed(db)
    return _snapshot(db, config)


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(root.rglob("*")) if p.is_file())


def test_writes_every_page_for_the_datasets_that_ran(tmp_path: Path, snap: dict[str, Any]) -> None:
    root = tmp_path / "results"
    results.write_release(root, snap)
    release = root / "releases" / "v0.13.0"
    names = sorted(p.name for p in release.iterdir())
    assert names == sorted(
        [
            "README.md",
            "accuracy.md",
            "data.json",
            "meth-twist-emseq-5M.md",
            "scaling.md",
            "wgs-5M.md",
        ]
    )
    assert (root / "README.md").exists()


def test_host_and_cost_details_are_never_published(tmp_path: Path, snap: dict[str, Any]) -> None:
    root = tmp_path / "results"
    results.write_release(root, snap)
    text = _all_text(root)
    for secret in (INSTANCE_ID, AZ, "0.1234", "s3://"):
        assert secret not in text


def test_sweep_uses_the_median_and_orients_speedup_bwa_mem3_faster(
    snap: dict[str, Any],
) -> None:
    wgs = next(d for d in snap["datasets"] if d["sample"] == "wgs-5M")
    c6a = next(c for c in wgs["speed"] if c["arch"] == "c6a")
    assert c6a["arms"]["default"]["wall_s"] == 11.0  # median of 10/11/30  # noqa: PLR2004
    page = results.render_dataset_page(wgs, snap)
    # bwa-mem2 22 s / bwa-mem3 11 s: bwa-mem3 is 2x faster, so 2.00x.
    assert "| 22.00 | 2.00x |" in page


def test_arm_rows_have_no_bwa_mem2_ratio(snap: dict[str, Any]) -> None:
    wgs = next(d for d in snap["datasets"] if d["sample"] == "wgs-5M")
    page = results.render_dataset_page(wgs, snap)
    arm_row = next(line for line in page.splitlines() if line.startswith("| c8g ("))
    assert arm_row.count("—") >= 2  # noqa: PLR2004 -- no bwa-mem2 time, no ratio


def test_arena_ratios_mean_bwa_mem3_faster(snap: dict[str, Any]) -> None:
    readme = results.render_release_readme(snap)
    # bwa 200 s vs bwa-mem3 40 s -> 5.00x; minibwa 32 s vs 40 s -> 0.80x (slower).
    assert "| bwa | 200.00 | 5.00x | 10.00x |" in readme
    assert "| minibwa | 32.00 | 0.80x | 1.60x |" in readme
    assert "v0.11.0" in readme  # the v110 ladder label is shown as a version
    assert "stride-2" in readme  # the denser arena index is disclosed


def test_fast_divergence_is_broken_down_not_just_a_percentage(snap: dict[str, Any]) -> None:
    wgs = next(d for d in snap["datasets"] if d["sample"] == "wgs-5M")
    page = results.render_dataset_page(wgs, snap)
    assert "94.0000%" in page
    assert "position 3.000%" in page
    assert "aux tags 2.000%" in page
    # Identical results on both instances collapse into one row.
    assert "| c6a, c8g | 94.0000% |" in page


def test_meth_reports_confident_relocation_not_concordance(snap: dict[str, Any]) -> None:
    meth = next(d for d in snap["datasets"] if d["sample"] == "meth-twist-emseq-5M")
    page = results.render_dataset_page(meth, snap)
    vs_bwameth = page.split("### vs bwameth")[1].split("###")[0]
    assert "0.126%" in vs_bwameth
    assert "72.0000%" not in vs_bwameth


def test_meth_without_the_placement_metric_says_not_measured(
    tmp_path: Path, config: WorkflowConfig
) -> None:
    db = tmp_path / "bench.db"
    _seed(db, meth_placement=False)
    snap = _snapshot(db, config)
    meth = next(d for d in snap["datasets"] if d["sample"] == "meth-twist-emseq-5M")
    vs_bwameth = results.render_dataset_page(meth, snap).split("### vs bwameth")[1]
    assert "_Not measured for this release._" in vs_bwameth.split("###")[0]
    assert "72.0000%" not in vs_bwameth.split("###")[0]


def test_accuracy_summarises_confident_reads(snap: dict[str, Any]) -> None:
    page = results.render_accuracy_page(snap)
    # 90 of 100 reads at MAPQ >= 20, 1 of those 90 mismapped.
    assert "| bwa-mem3 | 93.00% | 7.00% | 90.00% | 1.111% |" in page
    assert "| bwa-mem2 |" in page


def test_scaling_efficiency(snap: dict[str, Any]) -> None:
    page = results.render_scaling_page(snap)
    assert "| 4 | 20.00 | 80.0% | 1 |" in page  # 64 / (4 x 20)


def test_published_release_is_frozen_without_force(tmp_path: Path, snap: dict[str, Any]) -> None:
    root = tmp_path / "results"
    results.write_release(root, snap)
    with pytest.raises(FileExistsError, match="frozen"):
        results.write_release(root, snap)
    results.write_release(root, snap, force=True)


def test_rerender_reproduces_the_pages_from_snapshots_alone(
    tmp_path: Path, snap: dict[str, Any]
) -> None:
    root = tmp_path / "results"
    results.write_release(root, snap)
    before = {p: p.read_text() for p in root.rglob("*.md")}
    for path in before:
        path.write_text("stale")
    results.rerender(root)
    assert {p: p.read_text() for p in root.rglob("*.md")} == before


def test_index_leads_with_the_latest_and_keeps_older_releases(
    tmp_path: Path, snap: dict[str, Any]
) -> None:
    root = tmp_path / "results"
    older = {**snap, "version": "v0.9.0"}
    results.write_release(root, older)
    results.write_release(root, snap)
    index = (root / "README.md").read_text()
    assert "## Latest: v0.13.0" in index
    assert index.index("[v0.13.0](releases/v0.13.0") < index.index("[v0.9.0](releases/v0.9.0")
    assert (root / "releases" / "v0.9.0" / "README.md").exists()


def test_rejects_a_sha_that_is_not_a_blessed_release(
    tmp_path: Path, config: WorkflowConfig
) -> None:
    db = tmp_path / "bench.db"
    _seed(db)
    with pytest.raises(ValueError, match="not a blessed release"):
        build_snapshot(
            db_path=db,
            fg_labs_sha="f" * 40,
            version="v9.9.9",
            previous_version="v9.9.8",
            allowances=_allowances(),
            config=config,
            sweep_sa_stride=8,
            arena_sa_stride=8,
        )


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("v021", "v0.2.1"),
        ("v100", "v0.10.0"),
        ("v110", "v0.11.0"),
        ("394f8f8", "394f8f8 (interim build)"),
    ],
)
def test_release_label_to_version(label: str, expected: str) -> None:
    assert release_label_to_version(label) == expected


def test_every_published_dataset_is_a_configured_sample(config: WorkflowConfig) -> None:
    for dataset in DATASETS:
        assert dataset.sample in config.samples


def test_committed_results_are_current() -> None:
    """The committed pages must match what their committed snapshots render to.

    Fails when a renderer change was not followed by `cli bench results-render`,
    or when a page was hand-edited.
    """
    root = REPO_ROOT / "results"
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path, text in results.expected_files(root).items()
        if not path.exists() or path.read_text() != text
    ]
    assert not stale, f"regenerate with `cli bench results-render`: {stale}"


def test_thread_counts_and_arena_sample_come_from_the_snapshot(snap: dict[str, Any]) -> None:
    """Pages are a function of the snapshot: no thread count or sample is hardcoded."""
    changed = {**snap, "threads": 32, "arena_threads": 24, "arena_sample": "wes-5M"}
    wgs = next(d for d in changed["datasets"] if d["sample"] == "wgs-5M")
    page = results.render_dataset_page(wgs, changed)
    readme = results.render_release_readme(changed)
    index = results.render_index([changed])
    assert "32 threads" in page
    assert "wall x 32 threads" in page
    assert "`wes-5M`" in readme and "24 threads" in readme
    assert "`wes-5M`" in index and "24 threads" in index
    for text in (page, readme, index):
        assert "16 threads" not in text


@pytest.mark.parametrize(
    ("stride", "expected"),
    [(8, "stock stride-8 suffix-array index"), (4, "denser stride-4 suffix-array index")],
)
def test_sweep_index_is_disclosed_on_dataset_pages(
    snap: dict[str, Any], stride: int, expected: str
) -> None:
    changed = {**snap, "sweep_sa_stride": stride}
    wgs = next(d for d in changed["datasets"] if d["sample"] == "wgs-5M")
    assert expected in results.render_dataset_page(wgs, changed)


def test_comparator_version_and_instance_count_come_from_the_snapshot(
    snap: dict[str, Any],
) -> None:
    changed = {**snap, "comparators": {**snap["comparators"], "bwa-mem2": "v9.9.9"}}
    wgs = next(d for d in changed["datasets"] if d["sample"] == "wgs-5M")
    page = results.render_dataset_page(wgs, changed)
    assert "### vs bwa-mem2 v9.9.9" in page
    assert "v2.2.1" not in page
    # The fixture's datasets ran on three instance types (c6a, c8g, m7i).
    assert "across 3 AWS instance types" in results.render_release_readme(changed)
