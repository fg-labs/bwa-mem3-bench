"""Tests for aws.py job-inspection helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from bwa_mem3_bench.commands import aws as aws_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUEUE = "bwa-mem3-bench-c8g"
_STATUS = "RUNNING"


def _make_job(job_id: str, name: str = "snakejob-align-abc", created_at: int = 0) -> dict[str, Any]:
    return {"jobId": job_id, "jobName": name, "createdAt": created_at}


def _make_paginator(pages: list[list[dict[str, Any]]]) -> MagicMock:
    """Return a mock paginator that yields the given pages."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"jobSummaryList": page} for page in pages]
    return mock_paginator


# ---------------------------------------------------------------------------
# _list_jobs — pagination
# ---------------------------------------------------------------------------


def test_list_jobs_single_page() -> None:
    jobs = [_make_job("j1"), _make_job("j2")]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _make_paginator([jobs])

    with patch.object(aws_module, "_batch", return_value=mock_client):
        result = aws_module._list_jobs(_QUEUE, _STATUS)

    assert [j["jobId"] for j in result] == ["j1", "j2"]


def test_list_jobs_multiple_pages() -> None:
    page1 = [_make_job("j1"), _make_job("j2")]
    page2 = [_make_job("j3")]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _make_paginator([page1, page2])

    with patch.object(aws_module, "_batch", return_value=mock_client):
        result = aws_module._list_jobs(_QUEUE, _STATUS)

    assert [j["jobId"] for j in result] == ["j1", "j2", "j3"]


def test_list_jobs_empty_page() -> None:
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _make_paginator([[]])

    with patch.object(aws_module, "_batch", return_value=mock_client):
        result = aws_module._list_jobs(_QUEUE, _STATUS)

    assert result == []


# ---------------------------------------------------------------------------
# jobs() — returns empty when nothing matches
# ---------------------------------------------------------------------------


def test_jobs_prints_nothing_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _make_paginator([[]])

    with patch.object(aws_module, "_batch", return_value=mock_client):
        aws_module.jobs(queue=_QUEUE, status=_STATUS)

    captured = capsys.readouterr()
    assert "no matching jobs" in captured.out


# ---------------------------------------------------------------------------
# kill() — handles ClientError gracefully
# ---------------------------------------------------------------------------


def test_kill_handles_client_error(capsys: pytest.CaptureFixture[str]) -> None:
    error_response = {"Error": {"Code": "ClientException", "Message": "not found"}}
    mock_client = MagicMock()
    mock_client.terminate_job.side_effect = ClientError(error_response, "TerminateJob")

    with patch.object(aws_module, "_batch", return_value=mock_client):
        aws_module.kill("nonexistent-job-id")

    captured = capsys.readouterr()
    assert "failed" in captured.out


def test_kill_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.terminate_job.return_value = {}

    with patch.object(aws_module, "_batch", return_value=mock_client):
        aws_module.kill("j1", "j2")

    mock_client.terminate_job.assert_any_call(
        jobId="j1", reason="user-terminated via bwa-mem3-bench aws kill"
    )
    mock_client.terminate_job.assert_any_call(
        jobId="j2", reason="user-terminated via bwa-mem3-bench aws kill"
    )


# ---------------------------------------------------------------------------
# kill_all() — enumerates across queues and states
# ---------------------------------------------------------------------------


def test_kill_all_enumerates_all_queues_and_active_states(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """kill_all should attempt termination across all queues and all active states."""
    mock_client = MagicMock()

    # Return one job for RUNNING in c8g queue; empty for everything else
    def _paginate(jobQueue: str, jobStatus: str, **_: Any) -> Any:  # noqa: N803
        if jobQueue.endswith("-c8g") and jobStatus == "RUNNING":
            return [{"jobSummaryList": [_make_job("j-run-1")]}]
        return [{"jobSummaryList": []}]

    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = _paginate
    mock_client.get_paginator.return_value = mock_paginator
    mock_client.terminate_job.return_value = {}

    with patch.object(aws_module, "_batch", return_value=mock_client):
        aws_module.kill_all()

    captured = capsys.readouterr()
    assert "terminated 1 job(s)" in captured.out
    mock_client.terminate_job.assert_called_once_with(
        jobId="j-run-1",
        reason="bulk terminate via bwa-mem3-bench aws kill-all",
    )


def test_all_queues_covers_the_arena_queues() -> None:
    """The arena's on-demand queues (`bwa-mem3-bench-c8a-arena`,
    `bwa-mem3-bench-c8g-arena`) are SEPARATE from each arch's regular spot
    queue (e.g. `bwa-mem3-bench-c8a`) -- a queue missing from `_ALL_QUEUES` is
    invisible to `kill_all`/`jobs`/`cost`, leaving a stuck on-demand arena job
    running (and billing) past a supposed bulk-terminate. Regression test for
    a real CodeRabbit finding: this coverage was missing entirely before
    `aws_config.AwsConfig.arena_queues` existed."""
    assert "bwa-mem3-bench-c8a-arena" in aws_module._ALL_QUEUES
    assert "bwa-mem3-bench-c8g-arena" in aws_module._ALL_QUEUES
