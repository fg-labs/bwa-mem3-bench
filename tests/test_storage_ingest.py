"""Tests for ingest.walk_run → SQLite."""

import json
from pathlib import Path

import pytest

from bwa_mem3_bench.storage.ingest import (
    _parse_bwa_stderr,
    _parse_eval_txt,
    _parse_meth_tsv,
    _parse_variants_tsv,
    _supp_json,
    baseline_sha_for,
    ingest_accuracy,
    ingest_baseline,
    ingest_minibwa,
    ingest_run,
    minibwa_sha_for,
)
from bwa_mem3_bench.storage.sqlite import connect

_TIMING_HEADER = "s\th:m:s\tmax_rss\tmax_vms\tmax_uss\tmax_pss\tio_in\tio_out\tmean_load\tcpu_time"


def _write_minibwa_trial(root: Path, sha: str, cell: tuple[str, str, int], wall: float) -> None:
    """Create a synthetic `minibwa/<sha>/<sample>/<arch>/rep-<n>/` timing tree.

    ``cell`` is the ``(sample, arch, rep)`` triple.
    """
    sample, arch, rep = cell
    bench = root / sha / sample / arch / f"rep-{rep}" / "benchmarks"
    bench.mkdir(parents=True, exist_ok=True)
    (bench / "timing.minibwa.tsv").write_text(
        f"{_TIMING_HEADER}\n"
        f"{wall:.3f}\t0:00:{int(wall):02d}\t1024.50\t2048.00\t900.00\t950.00\t"
        "200.75\t50.25\t380.00\t75.00\n"
    )


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_run"
BASELINE_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_baseline"

# Values matched against the synthetic bwa.stderr.log fixture under
# tests/fixtures/synthetic_run/abc1234/smoke-1M/c7g/rep-1/benchmarks/.
_FIXTURE_PROCESS_SECONDS = 8.76
_FIXTURE_INDEX_READ_SECONDS = 2.44


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "benchmark.db"


def test_ingest_creates_run_and_trial_and_comparison(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    assert n == 1

    runs = conn.execute("SELECT fg_labs_sha FROM runs").fetchall()
    assert runs == [("abc1234",)]

    trial = conn.execute(
        "SELECT sample, arch, rep, wall_seconds, instance_type, reads_processed,"
        " process_seconds, index_read_seconds FROM trials"
    ).fetchone()
    assert trial == (
        "smoke-1M",
        "c7g",
        1,
        12.345,
        "c7g.4xlarge",
        2003,
        _FIXTURE_PROCESS_SECONDS,
        _FIXTURE_INDEX_READ_SECONDS,
    )

    comp = conn.execute(
        "SELECT kind, concordance_pct, total, concordant FROM comparisons"
    ).fetchone()
    assert comp == ("vs-baseline", 100.0, 2003, 2003)

    conn.close()


def test_ingest_vs_default_comparison(db_path: Path, tmp_path: Path) -> None:
    """A `--fast` arm's compare/vs-default.json (PR #189) ingests as a
    `comparisons` row with kind='vs-default', alongside the trial's timing."""
    sha = "def5678"
    rep_dir = tmp_path / sha / "wgs-5M-fast" / "c8g" / "rep-1"
    (rep_dir / "benchmarks").mkdir(parents=True)
    (rep_dir / "compare").mkdir(parents=True)
    (rep_dir / "benchmarks" / "timing.tsv").write_text(
        _TIMING_HEADER + "\n"
        "9.100\t0:00:09\t1024.50\t2048.00\t900.00\t950.00\t200.75\t50.25\t380.00\t40.10\n"
    )
    (rep_dir / "compare" / "vs-default.json").write_text(
        json.dumps(
            {
                "total_reads": 2003,
                "concordant": 1990,
                "concordance_pct": 99.35,
                "by_class": {"mapq_diff": {"count": 13}},
            }
        )
    )

    conn = connect(db_path)
    assert ingest_run(conn, runs_root=tmp_path, fg_labs_sha=sha) == 1
    comp = conn.execute(
        "SELECT kind, concordance_pct, total, concordant FROM comparisons"
    ).fetchone()
    assert comp == ("vs-default", 99.35, 2003, 1990)
    conn.close()


def test_supp_json_extracts_only_supp_keys() -> None:
    comp = {
        "concordance_pct": 99.9996,
        "by_class": {"pos_diff": {"count": 19}},
        "supp_query_total": 5123,
        "supp_baseline_total": 5118,
        "supp_count_mismatch_templates": 5,
        "supp_unmatched_pct": 0.0879,
    }
    out = _supp_json(comp)
    assert out is not None
    assert json.loads(out) == {
        "supp_query_total": 5123,
        "supp_baseline_total": 5118,
        "supp_count_mismatch_templates": 5,
        "supp_unmatched_pct": 0.0879,
    }


def test_supp_json_none_when_absent() -> None:
    assert _supp_json({"concordance_pct": 100.0, "by_class": {}}) is None


def test_parse_bwa_stderr_extracts_process_and_index_read(tmp_path: Path) -> None:
    log = tmp_path / "bwa.stderr.log"
    log.write_text(
        'Looking to launch executable "/usr/local/bin/bwa-mem2.fg-labs.avx2"\n'
        "* Done reading Index!!\n"
        "\tIndex read time avg: 2.44, (2.44, 2.44)\n"
        "\n\tOverall time (sec) (Excluding Index reading time):\n"
        "\tPROCESS() (Total compute time + (read + SAM) IO time) : 8.76\n"
        "\tMEM_PROCESS_SEQ() (Total compute time (Kernel + SAM)), avg: 7.88, (7.88, 7.88)\n"
    )
    process, index_read = _parse_bwa_stderr(log)
    assert process == _FIXTURE_PROCESS_SECONDS
    assert index_read == _FIXTURE_INDEX_READ_SECONDS


def test_parse_bwa_stderr_missing_file_returns_none(tmp_path: Path) -> None:
    process, index_read = _parse_bwa_stderr(tmp_path / "does-not-exist.log")
    assert process is None
    assert index_read is None


def test_parse_bwa_stderr_truncated_log_returns_partial(tmp_path: Path) -> None:
    """Log truncated mid-init (e.g., bwa OOM-killed) — Index read present, PROCESS missing."""
    log = tmp_path / "truncated.log"
    log.write_text(
        "* Done reading Index!!\n"
        "\tIndex read time avg: 2.44, (2.44, 2.44)\n"
        "\n[std::bad_alloc... bwa crashed]\n"
    )
    process, index_read = _parse_bwa_stderr(log)
    assert process is None
    assert index_read == _FIXTURE_INDEX_READ_SECONDS


def test_ingest_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    n = ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="abc1234")
    assert n == 1

    trials = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
    assert trials == (1,)
    comparisons = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()
    assert comparisons == (1,)
    conn.close()


def test_ingest_unknown_sha_raises(db_path: Path) -> None:
    conn = connect(db_path)
    with pytest.raises(FileNotFoundError):
        ingest_run(conn, runs_root=FIXTURE, fg_labs_sha="does-not-exist")
    conn.close()


EXPECTED_BASELINE_TRIALS = 3  # 2 reps for c7g + 1 rep for c6a in the fixture


def test_ingest_baseline_inserts_synthetic_sha_trials(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    assert n == EXPECTED_BASELINE_TRIALS

    synthetic = baseline_sha_for("v2.2.1")
    runs = conn.execute(
        "SELECT fg_labs_sha, upstream_tag, status FROM runs WHERE fg_labs_sha = ?",
        (synthetic,),
    ).fetchone()
    assert runs == (synthetic, "v2.2.1", "baseline")

    rows = conn.execute(
        "SELECT sample, arch, rep, wall_seconds FROM trials "
        "WHERE fg_labs_sha = ? ORDER BY arch, rep",
        (synthetic,),
    ).fetchall()
    assert rows == [
        ("smoke-1M", "c6a", 1, 14.2),
        ("smoke-1M", "c7g", 1, 20.0),
        ("smoke-1M", "c7g", 2, 21.0),
    ]

    # No comparisons should be created for baseline-only ingestion.
    comp_count = conn.execute(
        """
        SELECT COUNT(*) FROM comparisons c
        JOIN trials t ON t.id = c.trial_id
        WHERE t.fg_labs_sha = ?
        """,
        (synthetic,),
    ).fetchone()
    assert comp_count == (0,)
    conn.close()


def test_ingest_baseline_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v2.2.1")
    assert n == EXPECTED_BASELINE_TRIALS
    total = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE fg_labs_sha = ?",
        (baseline_sha_for("v2.2.1"),),
    ).fetchone()
    assert total == (EXPECTED_BASELINE_TRIALS,)
    conn.close()


def test_ingest_baseline_missing_tool_returns_zero(db_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_baseline(conn, baseline_root=BASELINE_FIXTURE, tool_version="v999.9.9")
    assert n == 0
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert runs == (0,)
    conn.close()


_MINIBWA_SHA = "a8cf4d336613672213dd2df89e9fe9cbc041c31e"
EXPECTED_MINIBWA_TRIALS = 3  # 2 reps c8g + 1 rep c7i


def test_minibwa_sha_for_prefixes() -> None:
    assert minibwa_sha_for("abc123") == "minibwa-abc123"


def test_ingest_minibwa_inserts_synthetic_sha_trials(db_path: Path, tmp_path: Path) -> None:
    minibwa_root = tmp_path / "minibwa"
    _write_minibwa_trial(minibwa_root, _MINIBWA_SHA, ("wes-5M", "c8g", 1), 30.0)
    _write_minibwa_trial(minibwa_root, _MINIBWA_SHA, ("wes-5M", "c8g", 2), 31.0)
    _write_minibwa_trial(minibwa_root, _MINIBWA_SHA, ("wes-5M", "c7i", 1), 40.0)

    conn = connect(db_path)
    n = ingest_minibwa(conn, minibwa_root=minibwa_root, minibwa_sha=_MINIBWA_SHA)
    assert n == EXPECTED_MINIBWA_TRIALS

    synthetic = minibwa_sha_for(_MINIBWA_SHA)
    run = conn.execute(
        "SELECT fg_labs_sha, status FROM runs WHERE fg_labs_sha = ?",
        (synthetic,),
    ).fetchone()
    assert run == (synthetic, "minibwa")

    rows = conn.execute(
        "SELECT sample, arch, rep, wall_seconds, process_seconds FROM trials "
        "WHERE fg_labs_sha = ? ORDER BY arch, rep",
        (synthetic,),
    ).fetchall()
    # minibwa has no PROCESS() line → process_seconds is NULL.
    assert rows == [
        ("wes-5M", "c7i", 1, 40.0, None),
        ("wes-5M", "c8g", 1, 30.0, None),
        ("wes-5M", "c8g", 2, 31.0, None),
    ]

    # Wall-time-only probe: no comparisons created.
    comp_count = conn.execute(
        """
        SELECT COUNT(*) FROM comparisons c
        JOIN trials t ON t.id = c.trial_id
        WHERE t.fg_labs_sha = ?
        """,
        (synthetic,),
    ).fetchone()
    assert comp_count == (0,)
    conn.close()


def test_ingest_minibwa_idempotent(db_path: Path, tmp_path: Path) -> None:
    minibwa_root = tmp_path / "minibwa"
    _write_minibwa_trial(minibwa_root, _MINIBWA_SHA, ("smoke-1M", "c8g", 1), 5.0)

    conn = connect(db_path)
    ingest_minibwa(conn, minibwa_root=minibwa_root, minibwa_sha=_MINIBWA_SHA)
    ingest_minibwa(conn, minibwa_root=minibwa_root, minibwa_sha=_MINIBWA_SHA)
    total = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE fg_labs_sha = ?",
        (minibwa_sha_for(_MINIBWA_SHA),),
    ).fetchone()
    assert total == (1,)
    conn.close()


def test_ingest_minibwa_missing_sha_returns_zero(db_path: Path, tmp_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_minibwa(conn, minibwa_root=tmp_path / "minibwa", minibwa_sha=_MINIBWA_SHA)
    assert n == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    conn.close()


# --- accuracy (holodeck eval) ingest -----------------------------------------

# Real holodeck eval TSV formats (src/commands/eval/{placement,variants,meth}.rs).
_EVAL_TXT = (
    "mapq_bin\ttotal\tcorrect\tmismapped\tunmapped\tpct_correct\tpct_mismapped\tpct_unmapped\n"
    "0\t10\t5\t5\t0\t50.00\t50.00\t0.00\n"
    "60+\t990\t985\t3\t2\t99.49\t0.30\t0.20\n"
    "ALL\t1000\t990\t8\t2\t99.00\t0.80\t0.20\n"
)
_VARIANTS_TSV = (
    "class\tconfounded\tn_expected\tn_represented\trepresented_pct\tmean_mapq\tmean_as\n"
    "mirror\tfalse\t20\t18\t90.00\t58.00\t140.50\n"
    "conversion\ttrue\t30\t30\t100.00\t60.00\tNA\n"
    "#variant_bearing_reads\t50\n"
    "#md_concordant_pct\t97.50\n"
    "#nm_concordant_pct\tNA\n"
)
_METH_TSV = "n_cpg\tpearson_r\trmse\n500\t0.9700\t0.0400\n"


def _write_eval_cell(
    runs_root: Path,
    sha: str,
    cell: tuple[str, str, int],
    tool: str,
    *,
    meth: bool,
) -> None:
    """Create a synthetic `runs/<sha>/<sample>/<arch>/rep-<n>/eval/<tool>.*` tree.

    Writes the three holodeck eval outputs; `meth=False` writes the empty
    `.meth.tsv` placeholder the eval rule creates for non-meth samples.
    """
    sample, arch, rep = cell
    eval_dir = runs_root / sha / sample / arch / f"rep-{rep}" / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / f"{tool}.eval.txt").write_text(_EVAL_TXT)
    (eval_dir / f"{tool}.variants.tsv").write_text(_VARIANTS_TSV)
    (eval_dir / f"{tool}.meth.tsv").write_text(_METH_TSV if meth else "")


def test_parse_eval_txt_surfaces_all_row(tmp_path: Path) -> None:
    tmp = tmp_path / "x.eval.txt"
    tmp.write_text(_EVAL_TXT)
    parsed = _parse_eval_txt(tmp)
    assert parsed["all"]["total"] == 1000  # noqa: PLR2004
    assert parsed["all"]["pct_correct"] == 99.00  # noqa: PLR2004
    assert set(parsed["bins"]) == {"0", "60+"}


def test_parse_variants_tsv_classes_and_footer_na(tmp_path: Path) -> None:
    tmp = tmp_path / "x.variants.tsv"
    tmp.write_text(_VARIANTS_TSV)
    parsed = _parse_variants_tsv(tmp)
    assert parsed["variant_bearing_reads"] == 50  # noqa: PLR2004
    assert parsed["md_concordant_pct"] == 97.5  # noqa: PLR2004
    assert parsed["nm_concordant_pct"] is None  # "NA" → None
    assert parsed["by_class"]["conversion"]["confounded"] is True
    assert parsed["by_class"]["conversion"]["mean_as"] is None  # "NA" → None


def test_parse_eval_txt_missing_all_row_raises(tmp_path: Path) -> None:
    """An eval.txt with bins but no ALL row must fail rather than yield empty
    headline placement metrics."""
    no_all = tmp_path / "x.eval.txt"
    no_all.write_text(
        "mapq_bin\ttotal\tcorrect\tmismapped\tunmapped\tpct_correct\tpct_mismapped\tpct_unmapped\n"
        "60+\t990\t985\t3\t2\t99.49\t0.30\t0.20\n"
    )
    with pytest.raises(ValueError, match="ALL row"):
        _parse_eval_txt(no_all)


def test_parse_eval_txt_header_only_raises(tmp_path: Path) -> None:
    header_only = tmp_path / "x.eval.txt"
    header_only.write_text(
        "mapq_bin\ttotal\tcorrect\tmismapped\tunmapped\tpct_correct\tpct_mismapped\tpct_unmapped\n"
    )
    with pytest.raises(ValueError, match=r"malformed eval\.txt"):
        _parse_eval_txt(header_only)


def test_parse_meth_tsv_empty_placeholder_is_none(tmp_path: Path) -> None:
    empty = tmp_path / "x.meth.tsv"
    empty.write_text("")
    assert _parse_meth_tsv(empty) is None


def test_parse_meth_tsv_header_only_raises(tmp_path: Path) -> None:
    """A header-only/truncated meth.tsv is a corrupt artifact, not the non-meth
    placeholder (which is truly empty)."""
    header_only = tmp_path / "x.meth.tsv"
    header_only.write_text("n_cpg\tpearson_r\trmse\n")
    with pytest.raises(ValueError, match=r"malformed meth\.tsv"):
        _parse_meth_tsv(header_only)


def test_parse_variants_tsv_empty_raises(tmp_path: Path) -> None:
    empty = tmp_path / "x.variants.tsv"
    empty.write_text("")
    with pytest.raises(ValueError, match=r"empty variants\.tsv"):
        _parse_variants_tsv(empty)


def test_parse_variants_tsv_missing_footer_key_raises(tmp_path: Path) -> None:
    """A truncated/format-drifted variants.tsv must fail loudly rather than
    default the headline footer metrics to placeholders."""
    truncated = tmp_path / "x.variants.tsv"
    # Drop the `#variant_bearing_reads` footer line.
    truncated.write_text(
        "class\tconfounded\tn_expected\tn_represented\trepresented_pct\tmean_mapq\tmean_as\n"
        "mirror\tfalse\t20\t18\t90.00\t58.00\t140.50\n"
        "#md_concordant_pct\t97.50\n"
        "#nm_concordant_pct\tNA\n"
    )
    with pytest.raises(ValueError, match="variant_bearing_reads"):
        _parse_variants_tsv(truncated)


def test_ingest_accuracy_inserts_arms(db_path: Path, tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    sha = "abc1234"
    # A meth dataset with two arms (fg-labs + baseline) and a non-meth arm.
    _write_eval_cell(runs_root, sha, ("sim-meth-vars", "m7i", 1), "fg-labs", meth=True)
    _write_eval_cell(runs_root, sha, ("sim-meth-vars", "m7i", 1), "baseline", meth=True)
    _write_eval_cell(runs_root, sha, ("sim-wgs-vars", "c6a", 1), "minibwa", meth=False)

    conn = connect(db_path)
    n = ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=sha)
    assert n == 3  # noqa: PLR2004

    rows = conn.execute(
        "SELECT sample, tool, placement_correct_pct, variant_bearing_reads, "
        "nm_concordant_pct, meth_pearson_r FROM accuracy "
        "WHERE fg_labs_sha = ? ORDER BY sample, tool",
        (sha,),
    ).fetchall()
    assert rows == [
        # meth arms carry the methylation correlation; non-meth's is NULL.
        ("sim-meth-vars", "baseline", 99.0, 50, None, 0.97),
        ("sim-meth-vars", "fg-labs", 99.0, 50, None, 0.97),
        ("sim-wgs-vars", "minibwa", 99.0, 50, None, None),
    ]
    conn.close()


def test_ingest_accuracy_idempotent(db_path: Path, tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    sha = "abc1234"
    _write_eval_cell(runs_root, sha, ("sim-wgs-vars", "c6a", 1), "fg-labs", meth=False)

    conn = connect(db_path)
    ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=sha)
    ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=sha)
    assert conn.execute("SELECT COUNT(*) FROM accuracy").fetchone() == (1,)
    conn.close()


def test_ingest_accuracy_missing_run_returns_zero(db_path: Path, tmp_path: Path) -> None:
    conn = connect(db_path)
    n = ingest_accuracy(conn, runs_root=tmp_path / "runs", fg_labs_sha="nope")
    assert n == 0
    conn.close()


def test_ingest_accuracy_missing_eval_txt_raises(db_path: Path, tmp_path: Path) -> None:
    """A partial sync (variants.tsv present, sibling .eval.txt missing) must
    fail rather than upsert a row with NULL placement metrics."""
    runs_root = tmp_path / "runs"
    sha = "abc1234"
    _write_eval_cell(runs_root, sha, ("sim-wgs-vars", "c6a", 1), "fg-labs", meth=False)
    (runs_root / sha / "sim-wgs-vars" / "c6a" / "rep-1" / "eval" / "fg-labs.eval.txt").unlink()

    conn = connect(db_path)
    with pytest.raises(FileNotFoundError, match="eval output"):
        ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=sha)
    conn.close()


def test_ingest_accuracy_missing_meth_tsv_raises(db_path: Path, tmp_path: Path) -> None:
    """The eval rule writes a `.meth.tsv` (empty for non-meth) for every arm, so
    a missing one is a partial sync — fail rather than clear meth metrics."""
    runs_root = tmp_path / "runs"
    sha = "abc1234"
    _write_eval_cell(runs_root, sha, ("sim-wgs-vars", "c6a", 1), "fg-labs", meth=False)
    (runs_root / sha / "sim-wgs-vars" / "c6a" / "rep-1" / "eval" / "fg-labs.meth.tsv").unlink()

    conn = connect(db_path)
    with pytest.raises(FileNotFoundError, match="meth output"):
        ingest_accuracy(conn, runs_root=runs_root, fg_labs_sha=sha)
    conn.close()
