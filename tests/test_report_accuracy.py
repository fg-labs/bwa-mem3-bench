"""Tests for `bench accuracy` (the truth-based accuracy report)."""

from __future__ import annotations

import json
import math
from pathlib import Path

from bwa_mem3_bench.report.accuracy import (
    build_accuracy_class_table,
    build_accuracy_table,
    render_accuracy_markdown,
)
from bwa_mem3_bench.storage.sqlite import connect, upsert_accuracy, upsert_run

FG_LABS_SHA = "abc1234"


def _meth_by_class() -> str:
    return json.dumps(
        {
            "conversion": {
                "confounded": True,
                "n_expected": 30,
                "n_represented": 30,
                "represented_pct": 100.0,
                "mean_mapq": 60.0,
                "mean_as": None,  # NA
            },
            "mirror": {
                "confounded": False,
                "n_expected": 20,
                "n_represented": 18,
                "represented_pct": 90.0,
                "mean_mapq": 55.0,
                "mean_as": 138.0,
            },
        }
    )


def _seed_db(db_path: Path) -> None:
    """Seed accuracy rows: a meth dataset (fg-labs + baseline arms) and a
    non-meth dataset (fg-labs + minibwa arms)."""
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")

    # Meth dataset: two arms, with per-class accumulators and a methylation r.
    for tool in ("fg-labs", "baseline"):
        upsert_accuracy(
            conn,
            fg_labs_sha=FG_LABS_SHA,
            sample="sim-meth-vars",
            arch="m7i",
            rep=1,
            tool=tool,
            placement_total=1000,
            placement_correct_pct=99.0,
            placement_mismapped_pct=0.8,
            placement_unmapped_pct=0.2,
            placement_json="{}",
            variant_bearing_reads=50,
            md_concordant_pct=97.5,
            nm_concordant_pct=None,
            by_class_json=_meth_by_class(),
            meth_n_cpg=500,
            meth_pearson_r=0.97,
            meth_rmse=0.04,
        )

    # Non-meth dataset: meth columns are NULL; single "all" variant class.
    for tool in ("fg-labs", "minibwa"):
        upsert_accuracy(
            conn,
            fg_labs_sha=FG_LABS_SHA,
            sample="sim-wgs-vars",
            arch="c6a",
            rep=1,
            tool=tool,
            placement_total=2000,
            placement_correct_pct=98.0,
            placement_mismapped_pct=1.5,
            placement_unmapped_pct=0.5,
            placement_json="{}",
            variant_bearing_reads=80,
            md_concordant_pct=99.0,
            nm_concordant_pct=99.0,
            by_class_json=json.dumps(
                {
                    "all": {
                        "confounded": False,
                        "n_expected": 80,
                        "n_represented": 79,
                        "represented_pct": 98.75,
                        "mean_mapq": 59.0,
                        "mean_as": 142.0,
                    }
                }
            ),
            meth_n_cpg=None,
            meth_pearson_r=None,
            meth_rmse=None,
        )
    conn.close()


def test_build_accuracy_table_orders_and_nulls_meth(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    df = build_accuracy_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)

    # Sorted by sample, then arm display order (fg-labs before baseline/minibwa).
    assert list(zip(df["sample"], df["tool"], strict=True)) == [
        ("sim-meth-vars", "fg-labs"),
        ("sim-meth-vars", "baseline"),
        ("sim-wgs-vars", "fg-labs"),
        ("sim-wgs-vars", "minibwa"),
    ]
    # Non-meth meth-correlation is NaN; meth dataset carries it.
    meth_fg = df[(df["sample"] == "sim-meth-vars") & (df["tool"] == "fg-labs")].iloc[0]
    nonmeth_fg = df[(df["sample"] == "sim-wgs-vars") & (df["tool"] == "fg-labs")].iloc[0]
    assert meth_fg["meth_pearson_r"] == 0.97  # noqa: PLR2004
    assert math.isnan(nonmeth_fg["meth_pearson_r"])


def test_build_accuracy_class_table_flags_confounded(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    classes = build_accuracy_class_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)
    conversion = classes[
        (classes["sample"] == "sim-meth-vars")
        & (classes["tool"] == "fg-labs")
        & (classes["subclass"] == "conversion")
    ].iloc[0]
    assert bool(conversion["confounded"]) is True
    mirror = classes[
        (classes["sample"] == "sim-meth-vars")
        & (classes["tool"] == "fg-labs")
        & (classes["subclass"] == "mirror")
    ].iloc[0]
    assert bool(mirror["confounded"]) is False
    assert mirror["mean_as"] == 138.0  # noqa: PLR2004


def test_render_accuracy_markdown_has_three_sections(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    _seed_db(db_path)
    md = render_accuracy_markdown(db_path=db_path, fg_labs_sha=FG_LABS_SHA)
    assert "# Truth-based alignment accuracy: `abc1234`" in md
    assert "## Placement + MAPQ calibration" in md
    assert "## Variant representation + methylation correlation" in md
    assert "## Per-class AS/MAPQ honesty" in md
    # The conversion class renders as confounded (arch column present).
    assert "| sim-meth-vars | fg-labs | m7i | conversion | yes |" in md
    # Non-meth meth columns render as em-dashes on the sim-wgs-vars variant row
    # (MD/NM present from the golden, but meth r / rmse / n_cpg are NULL). Anchor
    # on the variant row specifically — the placement row shares the
    # sample/tool/arch prefix but has no meth cells.
    variant_row = next(
        line
        for line in md.splitlines()
        if line.startswith("| sim-wgs-vars | fg-labs | c6a |") and line.rstrip().endswith("| — |")
    )
    assert "| 99.00 | 99.00 | — | — | — |" in variant_row


def test_render_accuracy_markdown_empty_when_no_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "benchmark.db"
    connect(db_path).close()  # create empty schema
    md = render_accuracy_markdown(db_path=db_path, fg_labs_sha="nope")
    assert "_No accuracy rows ingested for `nope`._" in md


def test_build_accuracy_class_table_skips_empty_lowest_rep(tmp_path: Path) -> None:
    """When the lowest rep of a cell carries no `by_class_json`, a later
    populated rep is still surfaced (the empty rep must not claim the cell)."""
    db_path = tmp_path / "benchmark.db"
    conn = connect(db_path)
    upsert_run(conn, fg_labs_sha=FG_LABS_SHA, status="complete")
    common = dict(
        fg_labs_sha=FG_LABS_SHA,
        sample="sim-wgs-vars",
        arch="c6a",
        tool="fg-labs",
        placement_total=2000,
        placement_correct_pct=98.0,
        placement_mismapped_pct=1.5,
        placement_unmapped_pct=0.5,
        placement_json="{}",
        variant_bearing_reads=80,
        md_concordant_pct=99.0,
        nm_concordant_pct=99.0,
        meth_n_cpg=None,
        meth_pearson_r=None,
        meth_rmse=None,
    )
    # rep 1: no class payload yet; rep 2: populated.
    upsert_accuracy(conn, rep=1, by_class_json="", **common)
    upsert_accuracy(
        conn,
        rep=2,
        by_class_json=json.dumps(
            {
                "all": {
                    "confounded": False,
                    "n_expected": 80,
                    "n_represented": 79,
                    "represented_pct": 98.75,
                    "mean_mapq": 59.0,
                    "mean_as": 142.0,
                }
            }
        ),
        **common,
    )
    conn.close()

    classes = build_accuracy_class_table(db_path=db_path, fg_labs_sha=FG_LABS_SHA)
    # The populated rep-2 row is surfaced rather than dropped by the empty rep-1.
    assert list(classes["subclass"]) == ["all"]
    assert classes.iloc[0]["n_represented"] == 79  # noqa: PLR2004
