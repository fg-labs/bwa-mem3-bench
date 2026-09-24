"""`bench` subcommand umbrella for reports."""

from __future__ import annotations

import sys
from pathlib import Path

from bwa_mem3_bench import DB_PATH, LOCAL_MIRROR_ROOT, REPO_ROOT
from bwa_mem3_bench.registry import DEFAULT_REGISTRY_PATH
from bwa_mem3_bench.release_allowances import DEFAULT_ALLOWANCES_PATH, load_allowances
from bwa_mem3_bench.report.accuracy import generate_accuracy
from bwa_mem3_bench.report.arena import generate_arena, generate_release_speedup
from bwa_mem3_bench.report.compare import generate_compare
from bwa_mem3_bench.report.docs import generate_docs, parse_releases
from bwa_mem3_bench.report.full_report import generate_full_report
from bwa_mem3_bench.report.performance import generate_performance
from bwa_mem3_bench.report.regression import check_regression
from bwa_mem3_bench.report.results import rerender, write_release
from bwa_mem3_bench.report.results_data import build_snapshot
from bwa_mem3_bench.report.speedup import generate_speedup
from bwa_mem3_bench.report.summary import generate_summary
from bwa_mem3_bench.report.trend import generate_trend
from bwa_mem3_bench.workflow_config import load_config

_DEFAULT_UPSTREAM_TAG = "v2.2.1"

# The committed results tree (see bwa_mem3_bench/report/results.py).
RESULTS_ROOT = REPO_ROOT / "results"


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


def docs(*, releases: str, out: Path | None = None) -> None:
    """Emit mdbook-ready divergence catalog + per-release table for bwa-mem3 docs.

    :param releases: ``label=sha,label=sha`` (e.g. ``v0.2.0=44cbaec,v0.2.1=89bd589``),
        in display order.
    :param out: output directory. Defaults to ``runs/docs/``.
    """
    out_dir = out or (LOCAL_MIRROR_ROOT / "runs" / "docs")
    paths = generate_docs(
        db_path=DB_PATH,
        releases=parse_releases(releases),
        registry_path=DEFAULT_REGISTRY_PATH,
        out_dir=out_dir,
    )
    for p in paths:
        print(f"wrote {p}")


def speedup(
    *,
    fg_labs_sha: str,
    upstream_tag: str | None = None,
    minibwa_sha: str | None = None,
    out: Path | None = None,
) -> None:
    """Emit the headline speedup table (fg-labs vs upstream baseline).

    Markdown is written to stdout by default; pass ``--out path/file.md`` to
    redirect to a file.

    :param fg_labs_sha: fg-labs SHA whose trials to compare.
    :param upstream_tag: upstream bwa-mem2 tag (e.g. ``v2.2.1``). Defaults to
        the value in ``config/defaults.yaml``.
    :param minibwa_sha: when given, add ``minibwa_speedup`` / ``minibwa_s``
        columns comparing against the ``minibwa-<sha>`` trials ingested by
        ``cli collect`` (the pinned SHA is in ``docker/build-arg-defaults.env``).
    :param out: optional path to write the markdown to.
    """
    tag = _resolve_upstream_tag(upstream_tag)
    text = generate_speedup(
        db_path=DB_PATH,
        fg_labs_sha=fg_labs_sha,
        upstream_tag=tag,
        out_md=out,
        minibwa_sha=minibwa_sha,
    )
    if out is None:
        print(text)
    else:
        print(f"wrote {out}")


def arena(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit the release-history "arena" comparison table.

    Every arm (bwa, upstream bwa-mem2, minibwa, every blessed bwa-mem3
    release, today's candidate in both modes) ran interleaved on ONE fixed
    on-demand host per arch — see `workflow/rules/arena.smk`. Markdown is
    written to stdout by default; pass ``--out path/file.md`` to redirect to
    a file.

    :param fg_labs_sha: fg-labs SHA whose arena rows to report.
    :param out: optional path to write the markdown to.
    """
    text = generate_arena(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_md=out)
    if out is None:
        print(text)
    else:
        print(f"wrote {out}")


def release_speedup(
    *,
    fg_labs_sha: str,
    arch: str,
    baseline_label: str = "bwa",
    out: Path | None = None,
) -> None:
    """Emit the release-history speedup table for one arena arch.

    One row per bwa-mem3 release (chronological), with stock and `--fast`
    columns, each measured against `baseline_label`'s own median wall time on
    the SAME arena host — see `workflow/rules/arena.smk`. Meant to feed the
    README's release-history speedup table on every bless. Markdown is
    written to stdout by default; pass ``--out path/file.md`` to redirect to
    a file.

    :param fg_labs_sha: fg-labs SHA whose arena rows to report.
    :param arch: arena arch to report (e.g. ``c7i``, ``c8g``).
    :param baseline_label: arena arm label to normalize speedups against.
    :param out: optional path to write the markdown to.
    """
    text = generate_release_speedup(
        db_path=DB_PATH,
        fg_labs_sha=fg_labs_sha,
        arch=arch,
        baseline_label=baseline_label,
        out_md=out,
    )
    if out is None:
        print(text)
    else:
        print(f"wrote {out}")


def accuracy(*, fg_labs_sha: str, out: Path | None = None) -> None:
    """Emit the truth-based alignment-accuracy report (holodeck eval).

    Renders placement + MAPQ calibration, per-read variant representation, and
    methylation-level correlation per sim dataset and aligner arm — graded
    against simulation truth, not tool-vs-tool agreement. Markdown is written to
    stdout by default; pass ``--out path/file.md`` to redirect to a file.

    :param fg_labs_sha: fg-labs SHA whose accuracy rows to report.
    :param out: optional path to write the markdown to.
    """
    text = generate_accuracy(db_path=DB_PATH, fg_labs_sha=fg_labs_sha, out_md=out)
    if out is None:
        print(text)
    else:
        print(f"wrote {out}")


def results(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    version: str,
    previous_version: str,
    sweep_sa_stride: int = 0,
    arena_sa_stride: int = 0,
    out: Path = RESULTS_ROOT,
    force: bool = False,
) -> None:
    """Publish a blessed release's results pages under ``results/``.

    Snapshots the release's aggregates from ``benchmark.db`` into
    ``results/releases/<version>/data.json`` and re-renders every page. Run
    after ``bless-golden``; commit the result in the bless PR.

    :param fg_labs_sha: the blessed golden SHA the release was benched at.
    :param version: the release version, e.g. ``v0.12.0``.
    :param previous_version: the prior blessed release, e.g. ``v0.11.0``.
    :param sweep_sa_stride: suffix-array stride of the sweep's bwa-mem3 index.
        ``0`` takes it from the current config -- pass ``8`` for a release at or
        before v0.12.0, which predates the denser index.
    :param arena_sa_stride: the same for the arena's bwa-mem3 arms.
    :param out: results root.
    :param force: overwrite an already-published release.
    """
    config = load_config(REPO_ROOT / "config")
    snap = build_snapshot(
        db_path=DB_PATH,
        fg_labs_sha=fg_labs_sha,
        version=version,
        previous_version=previous_version,
        allowances=load_allowances(DEFAULT_ALLOWANCES_PATH),
        config=config,
        sweep_sa_stride=sweep_sa_stride or 1 << config.sweep_dense_sa_shift,
        arena_sa_stride=arena_sa_stride or 1 << config.arena.dense_sa_shift,
    )
    for path in write_release(out, snap, force=force):
        print(f"wrote {path}")


def results_render(*, out: Path = RESULTS_ROOT) -> None:
    """Re-render every results page from the committed ``data.json`` snapshots.

    :param out: results root.
    """
    for path in rerender(out):
        print(f"wrote {path}")
