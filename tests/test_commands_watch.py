"""Tests for ``bwa_mem3_bench.commands.watch`` helpers."""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock, patch

watch_module = importlib.import_module("bwa_mem3_bench.commands.watch")

# 250 ``j*`` ids + one ``dup`` id = 251 unique ids; ceil(251 / 100) = 3 chunks.
_EXPECTED_CHUNK_SPAN_CALLS = 3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_summary(
    job_id: str, name: str = "snakejob-align-abc", status: str = "RUNNING"
) -> dict[str, Any]:
    """Build a minimal Batch job summary as ``list_jobs`` returns it."""
    return {"jobId": job_id, "jobName": name, "createdAt": 0, "status": status}


# ---------------------------------------------------------------------------
# _describe_jobs — must dedup before calling describe-jobs
# ---------------------------------------------------------------------------


def test_describe_jobs_dedups_input_ids() -> None:
    """
    ``describe_jobs`` rejects requests with duplicate jobIds (``ClientException:
    Jobs contains duplicates``). The wrapper must dedup before each Batch call.
    """
    mock_client = MagicMock()
    mock_client.describe_jobs.return_value = {"jobs": []}

    with patch.object(watch_module, "_batch_client", return_value=mock_client):
        watch_module._describe_jobs(["a", "b", "a", "c", "b", "a"])

    # Exactly one Batch call, with each id present at most once.
    assert mock_client.describe_jobs.call_count == 1
    sent = mock_client.describe_jobs.call_args.kwargs["jobs"]
    assert len(sent) == len(set(sent)), f"describe_jobs called with duplicates: {sent}"
    assert set(sent) == {"a", "b", "c"}


def test_describe_jobs_dedups_across_chunks() -> None:
    """
    Duplicates spanning the 100-id chunk boundary must still be deduped — a
    naive ``set()`` per chunk would let a duplicate slip into a second call.
    """
    mock_client = MagicMock()
    mock_client.describe_jobs.return_value = {"jobs": []}

    # 250 ids, with id "dup" repeated three times at positions that would land
    # in different chunks if not deduped first.
    ids: list[str] = []
    for n in range(250):
        ids.append(f"j{n}")
        if n in {5, 105, 205}:
            ids.append("dup")

    with patch.object(watch_module, "_batch_client", return_value=mock_client):
        watch_module._describe_jobs(ids)

    assert mock_client.describe_jobs.call_count == _EXPECTED_CHUNK_SPAN_CALLS
    for call in mock_client.describe_jobs.call_args_list:
        sent = call.kwargs["jobs"]
        assert len(sent) == len(set(sent)), f"chunk had duplicates: {sent}"


# ---------------------------------------------------------------------------
# _get_or_fetch_outputs — exercises the path that triggered the regression
# ---------------------------------------------------------------------------


def test_get_or_fetch_outputs_handles_duplicate_summaries() -> None:
    """
    When ``list_jobs`` returns the same job in two state queries (e.g. STARTING
    then RUNNING because the job transitioned mid-poll), the aggregated
    ``jobs`` list passed to ``_get_or_fetch_outputs`` contains duplicates.
    The describe-jobs call must still receive each id only once.
    """
    # Reset the module-level cache so the test is hermetic.
    watch_module._OUTPUT_CACHE.clear()

    mock_client = MagicMock()
    mock_client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-1",
                "container": {
                    "command": [
                        "/bin/bash",
                        "-c",
                        "python -m snakemake --target-jobs "
                        "'align_fg_labs:sha=abc,sample=smoke-1M,arch=c8g,rep=1' --cores 2",
                    ]
                },
            }
        ]
    }

    duplicated = [_make_summary("job-1"), _make_summary("job-1", status="STARTING")]

    with patch.object(watch_module, "_batch_client", return_value=mock_client):
        outputs = watch_module._get_or_fetch_outputs(duplicated)

    sent = mock_client.describe_jobs.call_args.kwargs["jobs"]
    assert sent == ["job-1"], f"expected single id, got {sent}"
    assert outputs == {
        "job-1": "align_fg_labs:sha=abc,sample=smoke-1M,arch=c8g,rep=1",
    }

    # Cleanup — don't leak cache state into other tests.
    watch_module._OUTPUT_CACHE.clear()


# ---------------------------------------------------------------------------
# Retry-chain classification — the actual goal of the identity extraction
# ---------------------------------------------------------------------------


def test_classify_marks_spot_terminated_failed_as_retried_when_successor_exists() -> None:
    """
    Spot interruption regression. A FAILED job whose snakemake target-jobs
    string matches a later-created RUNNING/RUNNABLE/SUCCEEDED job has been
    retried — should display as RETRIED, not FAILED. This used to fail
    because the identity extractor looked for S3 output paths, which don't
    appear in the snakemake-aws-batch worker command (only `--target-jobs
    rule:wildcards`).
    """
    watch_module._OUTPUT_CACHE.clear()
    target_jobs = "align_fg_labs:sha=abc,sample=panel-twist-5M,arch=c7i,rep=2"
    cmd = ["/bin/bash", "-c", f"python -m snakemake --target-jobs '{target_jobs}' --cores 2"]

    mock_client = MagicMock()
    mock_client.describe_jobs.return_value = {
        "jobs": [
            {"jobId": "spot-killed", "container": {"command": cmd}},
            {"jobId": "retry", "container": {"command": cmd}},
        ]
    }

    # JobNames must use real UUID format — `_extract_rule_name` strips a
    # canonical UUID suffix; otherwise it falls back to the full jobName,
    # which would put the two jobs in different identity buckets.
    failed = {
        "jobId": "spot-killed",
        "jobName": "snakejob-align_fg_labs-11111111-2222-3333-4444-555555555555",
        "status": "FAILED",
        "createdAt": 1000,
    }
    retry = {
        "jobId": "retry",
        "jobName": "snakejob-align_fg_labs-66666666-7777-8888-9999-aaaaaaaaaaaa",
        "status": "RUNNING",
        "createdAt": 2000,
    }

    with patch.object(watch_module, "_batch_client", return_value=mock_client):
        display = watch_module._classify_display_states([failed, retry])

    actual = display["spot-killed"]
    assert actual == "RETRIED", f"FAILED with later successor should be RETRIED, got {actual}"
    assert display["retry"] == "RUNNING"
    watch_module._OUTPUT_CACHE.clear()


def test_classify_keeps_terminal_failed_when_no_successor() -> None:
    """A FAILED job with no successor stays FAILED (real failure, not transient)."""
    watch_module._OUTPUT_CACHE.clear()
    target_jobs = "align_fg_labs:sha=abc,sample=wgs-5M,arch=c6a,rep=1"
    cmd = ["/bin/bash", "-c", f"python -m snakemake --target-jobs '{target_jobs}'"]

    mock_client = MagicMock()
    mock_client.describe_jobs.return_value = {
        "jobs": [{"jobId": "only-failure", "container": {"command": cmd}}]
    }
    only = {
        "jobId": "only-failure",
        "jobName": "snakejob-align_fg_labs-11111111-2222-3333-4444-555555555555",
        "status": "FAILED",
        "createdAt": 1000,
    }

    with patch.object(watch_module, "_batch_client", return_value=mock_client):
        display = watch_module._classify_display_states([only])

    assert display["only-failure"] == "FAILED"
    watch_module._OUTPUT_CACHE.clear()
