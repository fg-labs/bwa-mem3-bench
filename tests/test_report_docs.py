"""Tests for the mdbook doc generator (divergence catalog + release table)."""

import json
from pathlib import Path

import pytest

from bwa_mem3_bench.registry import DivergenceEntry
from bwa_mem3_bench.report.docs import (
    inject_between_markers,
    parse_releases,
    render_divergence_catalog,
    render_release_table,
)
from bwa_mem3_bench.storage.sqlite import connect, upsert_comparison, upsert_run, upsert_trial


def test_parse_releases_orders_pairs() -> None:
    assert parse_releases("v0.2.0=44cbaec, v0.2.1=89bd589") == [
        ("v0.2.0", "44cbaec"),
        ("v0.2.1", "89bd589"),
    ]


def test_parse_releases_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="label=sha"):
        parse_releases("v0.2.0")


def test_render_divergence_catalog_lists_entries() -> None:
    entries = [
        DivergenceEntry(
            id="FG-PRIMARY-DRIFT",
            pr="fg-labs/bwa-mem3#123",
            date="2026-06-07",
            summary="tie-breaks",
            affected="primary_alignment",
            expected_drift_pct=0.10,
            samples=("wgs-5M", "wes-5M"),
        )
    ]
    out = render_divergence_catalog(entries)
    assert "FG-PRIMARY-DRIFT" in out
    assert "wgs-5M, wes-5M" in out
    assert "0.1000" in out


def test_render_divergence_catalog_empty() -> None:
    assert "No divergences" in render_divergence_catalog([])


def test_inject_between_markers_replaces_region() -> None:
    text = "intro\n<!-- T:start -->\nOLD\n<!-- T:end -->\noutro\n"
    out = inject_between_markers(text, "T", "NEW")
    assert "OLD" not in out
    assert "NEW" in out
    assert out.startswith("intro")
    assert out.rstrip().endswith("outro")
    # markers preserved
    assert "<!-- T:start -->" in out and "<!-- T:end -->" in out


def test_inject_between_markers_missing_raises() -> None:
    with pytest.raises(ValueError, match="markers"):
        inject_between_markers("no markers here", "T", "x")


def _seed_release(  # noqa: PLR0913
    db: Path, sha: str, sample: str, concordance: float, supp: dict, arch: str = "c6a"
) -> None:
    conn = connect(db)
    upsert_run(conn, fg_labs_sha=sha, status="complete")
    tid = upsert_trial(
        conn,
        fg_labs_sha=sha,
        sample=sample,
        arch=arch,
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
        trial_id=tid,
        kind="vs-baseline",
        concordant=1,
        total=1,
        concordance_pct=concordance,
        by_class_json="{}",
        supp_json=json.dumps(supp),
    )
    conn.close()


def test_render_release_table_includes_data_and_notes_missing(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    _seed_release(
        db,
        "44cbaec",
        "wes-5M",
        99.9996,
        {"supp_query_total": 5123, "supp_baseline_total": 5118, "supp_count_mismatch_templates": 5},
    )
    out = render_release_table(db, [("v0.2.0", "44cbaec"), ("v0.2.1", "89bd589")])
    assert "v0.2.0" in out
    assert "99.9996" in out
    assert "5123" in out
    # v0.2.1 has no data → noted, not silently dropped
    assert "No data for: v0.2.1" in out


def test_render_release_table_supp_aligns_with_min_concordance_row(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    # Same sample, two archs: the min-concordance arch (c6a, 99.90) carries
    # supp_query=100; the higher-concordance arch (c7a, 100.0) carries 200.
    # The table must report the min row's supp (100), not an arbitrary row's.
    _seed_release(db, "x", "wgs-5M", 99.90, {"supp_query_total": 100}, arch="c6a")
    _seed_release(db, "x", "wgs-5M", 100.0, {"supp_query_total": 200}, arch="c7a")
    out = render_release_table(db, [("rel", "x")])
    # exactly one wgs-5M row, with concordance 99.90 and supp_query 100
    rows = [ln for ln in out.splitlines() if "wgs-5M" in ln]
    assert len(rows) == 1
    assert "99.9000" in rows[0]
    assert "| 100 |" in rows[0]
    assert "200" not in rows[0]
