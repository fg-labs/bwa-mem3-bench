"""Tests for the golden-sample discovery helpers (Gate #2 vs-golden scoping)."""

import subprocess
from unittest.mock import patch

import pytest

from bwa_mem3_bench import golden


def test_parse_golden_samples_extracts_pre_prefixes() -> None:
    """`PRE <name>/` rows become bare sample names; trailing slash stripped."""
    ls_output = (
        "                           PRE meth-twist-emseq-5M/\n"
        "                           PRE panel-agilent-qxt-5M/\n"
        "                           PRE wgs-5M/\n"
    )
    assert golden.parse_golden_samples(ls_output) == frozenset(
        {"meth-twist-emseq-5M", "panel-agilent-qxt-5M", "wgs-5M"}
    )


def test_parse_golden_samples_ignores_object_rows_and_blanks() -> None:
    """Only `PRE` directory rows count; object rows and blank lines are ignored."""
    ls_output = (
        "2026-06-08 12:00:00       1234 some-stray-object.txt\n"
        "\n"
        "                           PRE wes-5M/\n"
    )
    assert golden.parse_golden_samples(ls_output) == frozenset({"wes-5M"})


def test_parse_golden_samples_empty() -> None:
    assert golden.parse_golden_samples("") == frozenset()


def test_golden_backed_samples_parses_ls() -> None:
    """A successful `aws s3 ls` is parsed into the sample set."""
    completed = type(
        "P",
        (),
        {"returncode": 0, "stdout": "                           PRE wgs-5M/\n", "stderr": ""},
    )()
    with patch.object(golden.subprocess, "run", return_value=completed) as run:
        result = golden.golden_backed_samples("my-bucket", "deadbeef")
    assert result == frozenset({"wgs-5M"})
    # Lists the per-sample golden prefix for the pinned SHA.
    assert run.call_args.args[0] == ["aws", "s3", "ls", "s3://my-bucket/golden/fg-labs-deadbeef/"]
    # The listing is bounded so a network/DNS stall can't hang workflow init.
    assert run.call_args.kwargs["timeout"] is not None


def test_golden_backed_samples_raises_on_timeout() -> None:
    """A stalled `aws s3 ls` surfaces as a RuntimeError, not an indefinite hang."""
    timeout_exc = subprocess.TimeoutExpired(cmd=["aws", "s3", "ls"], timeout=30)
    with (
        patch.object(golden.subprocess, "run", side_effect=timeout_exc),
        pytest.raises(RuntimeError, match="aws s3 ls timed out"),
    ):
        golden.golden_backed_samples("b", "sha")


def test_golden_backed_samples_empty_prefix_is_not_an_error() -> None:
    """Exit 1 with no stderr (prefix simply has no entries) yields an empty set."""
    completed = type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()
    with patch.object(golden.subprocess, "run", return_value=completed):
        assert golden.golden_backed_samples("b", "nosuchsha") == frozenset()


def test_golden_backed_samples_raises_on_real_s3_error() -> None:
    """A non-zero exit with stderr (bad creds, region) is surfaced, not swallowed."""
    completed = type(
        "P", (), {"returncode": 255, "stdout": "", "stderr": "Unable to locate credentials"}
    )()
    with (
        patch.object(golden.subprocess, "run", return_value=completed),
        pytest.raises(RuntimeError, match="aws s3 ls failed"),
    ):
        golden.golden_backed_samples("b", "sha")


def test_parse_run_reps_counts_only_aligned_bams() -> None:
    sha = "abc"
    ls = "\n".join(
        [
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-1/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-2/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-1/compare/vs-golden.json",
            f"2026 1 runs/{sha}/wes-5M/c8g/rep-1/aligned.bam",
            "2026 1 runs/other/wes-5M/c8g/rep-1/aligned.bam",
        ]
    )
    assert golden.parse_run_reps(ls, sha) == {("wgs-5M", "c6a"): 2, ("wes-5M", "c8g"): 1}


def test_parse_run_reps_counts_only_numeric_rep_dirs() -> None:
    """A stray ``rep-backup`` (or ``rep-0``) must not inflate a cell's rep count."""
    sha = "abc"
    ls = "\n".join(
        [
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-1/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-12/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-backup/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-0/aligned.bam",
            f"2026 1 runs/{sha}/wgs-5M/c6a/rep-/aligned.bam",
        ]
    )
    assert golden.parse_run_reps(ls, sha) == {("wgs-5M", "c6a"): 2}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rep-1", True),
        ("rep-10", True),
        ("rep-0", False),
        ("rep-01", False),
        ("rep-", False),
        ("rep-backup", False),
        ("rep-1x", False),
        ("rep1", False),
    ],
)
def test_is_rep_dir(name: str, expected: bool) -> None:
    assert golden.is_rep_dir(name) is expected


def test_parse_golden_cells() -> None:
    sha = "abc"
    ls = "\n".join(
        [
            f"2026 1 golden/fg-labs-{sha}/wgs-5M/c6a/aligned.bam",
            f"2026 1 golden/fg-labs-{sha}/wgs-5M/c6a/aligned.bam.bai",
            f"2026 1 golden/fg-labs-{sha}/hic-1M/c8g/aligned.bam",
        ]
    )
    assert golden.parse_golden_cells(ls, sha) == frozenset({("wgs-5M", "c6a"), ("hic-1M", "c8g")})


def test_missing_golden_cells_reports_run_cells_absent_from_golden() -> None:
    sha = "abc"
    run_ls = "\n".join(
        f"2026 1 runs/{sha}/{s}/c6a/rep-1/aligned.bam" for s in ("hic-1M", "wes-5M", "wgs-5M")
    )
    golden_ls = f"2026 1 golden/fg-labs-{sha}/hic-1M/c6a/aligned.bam"

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out = golden_ls if "/golden/" in argv[-1] else run_ls
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")

    with patch.object(golden.subprocess, "run", side_effect=fake_run):
        assert golden.missing_golden_cells("B", sha) == [("wes-5M", "c6a"), ("wgs-5M", "c6a")]


def test_list_recursive_raises_on_s3_error() -> None:
    failed = subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="AccessDenied")
    with (
        patch.object(golden.subprocess, "run", return_value=failed),
        pytest.raises(RuntimeError, match="AccessDenied"),
    ):
        golden.list_recursive("s3://B/runs/abc/")


def test_missing_golden_cells_rejects_an_empty_run_tree() -> None:
    """With no run BAMs there is nothing to check completeness against: fail, don't pass."""
    empty = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch.object(golden.subprocess, "run", return_value=empty),
        pytest.raises(RuntimeError, match="no aligned.bam"),
    ):
        golden.missing_golden_cells("B", "abc")
