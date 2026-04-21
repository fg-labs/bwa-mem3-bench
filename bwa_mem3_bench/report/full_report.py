"""`bench full-report` — concatenate all reports into one markdown document."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench.report.compare import generate_compare
from bwa_mem3_bench.report.performance import generate_performance
from bwa_mem3_bench.report.summary import generate_summary


def generate_full_report(*, db_path: Path, fg_labs_sha: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    generate_summary(db_path=db_path, fg_labs_sha=fg_labs_sha, out_md=out_dir / "summary.md")
    generate_performance(db_path=db_path, fg_labs_sha=fg_labs_sha, out_dir=out_dir / "performance")
    generate_compare(db_path=db_path, fg_labs_sha=fg_labs_sha, out_md=out_dir / "compare.md")

    merged: list[str] = []
    for name in ("summary.md", "performance/report.md", "compare.md"):
        p = out_dir / name
        if p.exists():
            merged.append(p.read_text())
            merged.append("\n\n---\n\n")
    (out_dir / "full.md").write_text("".join(merged))
