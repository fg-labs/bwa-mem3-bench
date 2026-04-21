"""`bench` subcommand umbrella for reports."""

from __future__ import annotations

import sys
from pathlib import Path

from bwa_mem3_bench import DB_PATH, LOCAL_MIRROR_ROOT, REPO_ROOT
from bwa_mem3_bench.report.compare import generate_compare
from bwa_mem3_bench.report.full_report import generate_full_report
from bwa_mem3_bench.report.performance import generate_performance
from bwa_mem3_bench.report.regression import check_regression
from bwa_mem3_bench.report.speedup import generate_speedup
from bwa_mem3_bench.report.summary import generate_summary
from bwa_mem3_bench.report.trend import generate_trend
from bwa_mem3_bench.workflow_config import load_config

_DEFAULT_UPSTREAM_TAG = "v2.2.1"


def _resolve_upstream_tag(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        return load_config(REPO_ROOT / "config").upstream_tag
    except (FileNotFoundError, KeyError, OSError):
        return _DEFAULT_UPSTREAM_TAG


def summary(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit a markdown one-pager for a completed run."""
    out_md = out or (LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "summary.md")
    generate_summary(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_md=out_md)
    print(f"wrote {out_md}")


def report(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit a full performance report (tables + plots).

    :param fg_labs_sha: fg-labs SHA.
    :param out: output directory. Defaults to `runs/<sha>/report/`.
    """
    out_dir = out or (LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "report")
    generate_performance(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_dir=out_dir)
    print(f"wrote {out_dir}")


def compare(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit drift report (vs upstream baseline).

    :param fg_labs_sha: fg-labs SHA.
    :param out: output .md path. Defaults to `runs/<sha>/compare.md`.
    """
    out_md = out or (LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "compare.md")
    generate_compare(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_md=out_md)
    print(f"wrote {out_md}")


def regression(*, fg_labs_sha: str, prev: str, out: Path | None = None) -> None:
    """Check `fg_labs_sha` for regressions vs a previous `prev` SHA.

    Exits non-zero on regression (intended for CI gating).

    :param fg_labs_sha: fg-labs SHA to evaluate.
    :param prev: previous fg-labs SHA to compare against.
    :param out: output .md path. Defaults to `runs/<sha>/regression.md`.
    """
    out_md = out or (LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "regression.md")
    ok, report = check_regression(db_path=DB_PATH, new_sha=fg_labs_sha, prev_sha=prev)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report)
    print(report)
    sys.exit(0 if ok else 1)


def trend(*, last: int = 20, out: Path | None = None) -> None:
    """Emit a trend report of the last N commits."""
    out_dir = out or (REPO_ROOT / "data" / "benchmarks")
    generate_trend(db_path=DB_PATH, out_dir=out_dir, last=last)
    print(f"wrote {out_dir}/trend.md")


def full_report(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit the complete report (summary + performance + compare)."""
    out_dir = out or (LOCAL_MIRROR_ROOT / "runs" / fg_labs_sha / "full-report")
    generate_full_report(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_dir=out_dir)
    print(f"wrote {out_dir}/full.md")


def speedup(
    *,
    fg_labs_sha: str,
    upstream_tag: str | None = None,
    out: Path | None = None,
) -> None:
    """Emit the headline speedup table (fg-labs vs upstream baseline).

    Markdown is written to stdout by default; pass ``--out path/file.md`` to
    redirect to a file.

    :param fg_labs_sha: fg-labs SHA whose trials to compare.
    :param upstream_tag: upstream bwa-mem2 tag (e.g. ``v2.2.1``). Defaults to
        the value in ``config/defaults.yaml``.
    :param out: optional path to write the markdown to.
    """
    tag = _resolve_upstream_tag(upstream_tag)
    text = generate_speedup(
        db_path=DB_PATH,
        fg_labs_sha=fg_labs_sha,
        upstream_tag=tag,
        out_md=out,
    )
    if out is None:
        print(text)
    else:
        print(f"wrote {out}")
