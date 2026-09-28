"""Tests for the golden-sample discovery helpers (Gate #2 vs-golden scoping)."""

import dataclasses
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import botocore.exceptions
import pytest

from bwa_mem3_bench import golden

_SHA = "d" * 40

# How soon a wall-clock-capped listing must fail once its (test) cap is hit.
_PROMPT_FAILURE_SECONDS = 2


class _FakePaginator:
    """A `list_objects_v2` paginator yielding canned pages, recording its kwargs.

    Pages are yielded lazily, and an exception is raised mid-iteration, the way a
    real botocore PageIterator fails: it only calls S3 as it is iterated.
    """

    def __init__(self, pages: list[dict[str, object] | BaseException]) -> None:
        self.pages = pages
        self.kwargs: dict[str, object] = {}

    def paginate(self, **kwargs: object) -> Iterator[dict[str, object]]:
        self.kwargs = kwargs
        for page in self.pages:
            if isinstance(page, BaseException):
                raise page
            yield page


def _patch_pages(pages: list[dict[str, object] | BaseException]) -> tuple[_FakePaginator, Any]:
    paginator = _FakePaginator(pages)
    client = type("C", (), {"get_paginator": lambda self, name: paginator})()
    return paginator, patch.object(golden, "_s3_client", return_value=client)


def test_golden_backed_samples_lists_one_level_of_sample_prefixes() -> None:
    """Each immediate sub-prefix of the golden is a sample, across pages; objects are not."""
    root = f"golden/fg-labs-{_SHA}/"
    paginator, patched = _patch_pages(
        [
            {
                "CommonPrefixes": [
                    {"Prefix": f"{root}wgs-5M/"},
                    {"Prefix": f"{root}meth-twist-emseq-5M/"},
                ],
                "Contents": [{"Key": f"{root}stray.txt"}],
            },
            {"CommonPrefixes": [{"Prefix": f"{root}wes-5M/"}]},
        ]
    )
    with patched:
        result = golden.golden_backed_samples("my-bucket", _SHA)
    assert result == frozenset({"wgs-5M", "meth-twist-emseq-5M", "wes-5M"})
    assert paginator.kwargs == {"Bucket": "my-bucket", "Prefix": root, "Delimiter": "/"}


def test_golden_backed_samples_empty_prefix_is_not_an_error() -> None:
    """An unblessed SHA (no keys under the prefix) is "nothing to compare", not a failure."""
    _, patched = _patch_pages([{"KeyCount": 0}])
    with patched:
        assert golden.golden_backed_samples("b", _SHA) == frozenset()


@pytest.mark.parametrize(
    "error",
    [
        botocore.exceptions.NoCredentialsError(),
        botocore.exceptions.ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "no bucket"}}, "ListObjectsV2"
        ),
        botocore.exceptions.ReadTimeoutError(endpoint_url="https://s3"),
        botocore.exceptions.ConnectTimeoutError(endpoint_url="https://s3"),
    ],
    ids=["credentials", "missing-bucket", "read-timeout", "connect-timeout"],
)
def test_golden_backed_samples_raises_on_s3_failure_mid_iteration(error: Exception) -> None:
    """A failure on a later page is surfaced, never read as "no samples"."""
    root = f"golden/fg-labs-{_SHA}/"
    _, patched = _patch_pages([{"CommonPrefixes": [{"Prefix": f"{root}wgs-5M/"}]}, error])
    with patched, pytest.raises(RuntimeError, match=f"listing s3://b/{root} failed"):
        golden.golden_backed_samples("b", _SHA)


def test_golden_backed_samples_is_capped_by_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing that never returns fails the parse on time, whatever is stalled."""
    monkeypatch.setattr(golden, "_LS_WALL_CLOCK_SECONDS", 0.2)
    release = threading.Event()

    def _stall(*_args: object, **_kwargs: object) -> frozenset[str]:
        release.wait(5)
        return frozenset()

    monkeypatch.setattr(golden, "_list_sample_prefixes", _stall)
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="did not finish within 0.2s"):
            golden.golden_backed_samples("b", _SHA)
    finally:
        release.set()
    # Well under the 5 s the stalled listing would take to return on its own.
    assert time.monotonic() - start < _PROMPT_FAILURE_SECONDS


@pytest.mark.parametrize("sha", ["deadbee", "v0.13.0", "D" * 40, "", "d" * 39])
def test_golden_backed_samples_rejects_a_sha_that_can_match_no_golden(sha: str) -> None:
    """A short SHA or tag would list nothing and silently skip Gate #2."""
    with (
        patch.object(golden, "_s3_client", side_effect=AssertionError("must not list")),
        pytest.raises(RuntimeError, match="not a full 40-hex SHA"),
    ):
        golden.golden_backed_samples("b", sha)


def test_golden_backed_samples_accepts_a_build_variant_suffix() -> None:
    _, patched = _patch_pages([{"KeyCount": 0}])
    with patched:
        assert golden.golden_backed_samples("b", f"{_SHA}-lto-build") == frozenset()


def test_golden_listing_needs_no_aws_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing runs while the Snakefile parses, where no `aws` binary may exist."""
    monkeypatch.setenv("PATH", "/nonexistent")
    root = f"golden/fg-labs-{_SHA}/"
    _, patched = _patch_pages([{"CommonPrefixes": [{"Prefix": f"{root}wgs-5M/"}]}])
    with patched:
        assert golden.golden_backed_samples("b", _SHA) == frozenset({"wgs-5M"})


def test_s3_client_pins_per_attempt_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-attempt limits the wall-clock cap is sized around, not botocore's defaults."""
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config = golden._s3_client().meta.config
    assert config.connect_timeout == golden._LS_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == golden._LS_READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == golden._LS_TOTAL_ATTEMPTS
    worst_case = golden._LS_TOTAL_ATTEMPTS * (
        golden._LS_CONNECT_TIMEOUT_SECONDS + golden._LS_READ_TIMEOUT_SECONDS
    )
    assert worst_case <= golden._LS_WALL_CLOCK_SECONDS


def test_s3_client_follows_the_ambient_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like the `aws` CLI it replaced: the environment's region wins."""
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert golden._s3_client().meta.region_name == "eu-west-1"


def test_s3_client_falls_back_to_the_deploy_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty region in the environment falls back instead of failing obscurely."""
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.setenv(var, "") if var != "AWS_PROFILE" else monkeypatch.delenv(var, False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    real = golden.aws_config.load()
    monkeypatch.setattr(
        golden.aws_config, "load", lambda: dataclasses.replace(real, region="ap-south-1")
    )
    assert golden._s3_client().meta.region_name == "ap-south-1"


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
