"""Test bench summary: generates a markdown dashboard for a run."""

from pathlib import Path

from bwa_mem3_bench.report.summary import generate_summary
from bwa_mem3_bench.storage.ingest import ingest_run
from bwa_mem3_bench.storage.sqlite import connect

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_run"


def test_summary_includes_perf_and_concordance(tmp_path: Path) -> None:
    db = tmp_path / "benchmark.db"
    conn = connect(db)
    ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")

    out_md = tmp_path / "summary.md"
    generate_summary(db_path=db, fg_labs_sha="abc1234", out_md=out_md)
    text = out_md.read_text()

    assert "abc1234" in text
    assert "smoke-1M" in text
    assert "c7g" in text
    assert "Concordance" in text
    assert "100" in text
    assert "12.35" in text or "12.3" in text
    conn.close()
