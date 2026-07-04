"""Tests for `bench compare` report generation (vs-upstream + `--fast` vs-default)."""

import json
from pathlib import Path

from bwa_mem3_bench.report.compare import generate_compare
from bwa_mem3_bench.storage import VS_BASELINE, VS_DEFAULT
from bwa_mem3_bench.storage.sqlite import connect, upsert_comparison, upsert_run, upsert_trial

_SHA = "abc1234"


def _trial(conn, sample: str, arch: str) -> int:
    return upsert_trial(
        conn,
        fg_labs_sha=_SHA,
        sample=sample,
        arch=arch,
        rep=1,
        wall_seconds=100.0,
        max_rss_mb=1000.0,
        cpu_time=400.0,
        io_read_mb=10.0,
        io_write_mb=5.0,
        mean_load=300.0,
        reads_processed=2000,
        instance_type=None,
        availability_zone=None,
        spot_price=None,
        status="ok",
    )


def _seed(db: Path) -> None:
    conn = connect(db)
    upsert_run(conn, fg_labs_sha=_SHA, status="complete")
    # Default arm vs upstream — near-perfect concordance.
    base = _trial(conn, "wgs-5M", "c6a")
    upsert_comparison(
        conn,
        trial_id=base,
        kind=VS_BASELINE,
        concordant=1999,
        total=2000,
        concordance_pct=99.95,
        by_class_json=json.dumps({"pos_diff": {"count": 1}}),
        supp_json=None,
    )
    # Fast arm vs its default sibling — divergence concentrated at low MAPQ.
    fast = _trial(conn, "wgs-5M-fast", "c6a")
    upsert_comparison(
        conn,
        trial_id=fast,
        kind=VS_DEFAULT,
        concordant=1980,
        total=2000,
        concordance_pct=99.00,
        by_class_json=json.dumps({"mapq_diff_lt30": {"count": 18}, "mapq_diff_ge60": {"count": 2}}),
        supp_json=None,
    )
    conn.commit()
    conn.close()


def test_generate_compare_renders_both_sections(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    _seed(db)
    out = tmp_path / "compare.md"
    generate_compare(db_path=db, fg_labs_sha=_SHA, out_md=out)
    md = out.read_text()

    # vs-upstream section + the default arm's concordance.
    assert "## Concordance vs upstream bwa-mem2" in md
    assert "99.9500" in md
    # `--fast` preset vs-default section + the fast arm + its MAPQ-stratified
    # by-class breakdown (the headline check of PR #189's low-MAPQ-only claim).
    assert "## `--fast` preset vs default bwa-mem3" in md
    assert "wgs-5M-fast" in md
    assert "mapq_diff_lt30" in md


def test_generate_compare_omits_fast_section_when_absent(tmp_path: Path) -> None:
    """With no vs-default rows, the report renders the upstream section only —
    no empty `--fast` heading."""
    db = tmp_path / "benchmark.db"
    conn = connect(db)
    upsert_run(conn, fg_labs_sha=_SHA, status="complete")
    base = _trial(conn, "wgs-5M", "c6a")
    upsert_comparison(
        conn,
        trial_id=base,
        kind=VS_BASELINE,
        concordant=2000,
        total=2000,
        concordance_pct=100.0,
        by_class_json=json.dumps({}),
        supp_json=None,
    )
    conn.commit()
    conn.close()

    out = tmp_path / "compare.md"
    generate_compare(db_path=db, fg_labs_sha=_SHA, out_md=out)
    md = out.read_text()
    assert "## Concordance vs upstream bwa-mem2" in md
    assert "`--fast` preset vs default" not in md
