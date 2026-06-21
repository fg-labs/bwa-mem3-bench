"""Tests for the golden-sample discovery helpers (Gate #2 vs-golden scoping)."""

import subprocess
from unittest.mock import patch

import pytest

from bwa_mem3_bench import golden


def test_parse_golden_samples_extracts_pre_prefixes() -> None:
    """`PRE <name>/` rows become bare sample names; trailing slash stripped."""
    ls_output = (
        "                           PRE meth-twist-emseq-5M/\n"
        "                           PRE panel-twist-5M/\n"
        "                           PRE wgs-5M/\n"
    )
    assert golden.parse_golden_samples(ls_output) == frozenset(
        {"meth-twist-emseq-5M", "panel-twist-5M", "wgs-5M"}
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
