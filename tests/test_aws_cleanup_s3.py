"""Tests for `cli aws cleanup-s3` — old-run BAM cleanup with golden + active-job guards."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import patch

import pytest

from bwa_mem3_bench.commands import aws as aws_module
from bwa_mem3_bench.commands.aws import _select_run_bams_to_delete


def _ts(day: int) -> dt.datetime:
    return dt.datetime(2026, 6, day, tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# Pure selection policy
# --------------------------------------------------------------------------
def _runs() -> dict[str, tuple[float, list[tuple[str, int]]]]:
    # newest -> oldest: shaC (day 5), shaB (day 3), shaA (day 1)
    return {
        "shaA": (_ts(1).timestamp(), [("runs/shaA/s/a/rep-1/aligned.bam", 100)]),
        "shaB": (_ts(3).timestamp(), [("runs/shaB/s/a/rep-1/aligned.bam", 200)]),
        "shaC": (_ts(5).timestamp(), [("runs/shaC/s/a/rep-1/aligned.bam", 400)]),
    }


def test_select_keeps_latest_n_runs() -> None:
    """keep_latest=1 keeps the newest run (shaC); deletes the older two."""
    victims = _select_run_bams_to_delete(_runs(), golden_shas=set(), keep_latest=1)
    keys = {k for k, _ in victims}
    assert keys == {"runs/shaA/s/a/rep-1/aligned.bam", "runs/shaB/s/a/rep-1/aligned.bam"}


def test_select_protects_golden_shas_even_when_old() -> None:
    """An old run SHA that is a blessed golden is never selected for deletion."""
    victims = _select_run_bams_to_delete(_runs(), golden_shas={"shaA"}, keep_latest=1)
    keys = {k for k, _ in victims}
    # shaA is golden-protected (kept) despite being the oldest; only shaB remains.
    assert keys == {"runs/shaB/s/a/rep-1/aligned.bam"}


def test_select_keep_latest_zero_still_protects_golden() -> None:
    victims = _select_run_bams_to_delete(_runs(), golden_shas={"shaC"}, keep_latest=0)
    keys = {k for k, _ in victims}
    assert keys == {
        "runs/shaA/s/a/rep-1/aligned.bam",
        "runs/shaB/s/a/rep-1/aligned.bam",
    }


def test_select_keep_all_when_keep_latest_exceeds_count() -> None:
    assert _select_run_bams_to_delete(_runs(), golden_shas=set(), keep_latest=10) == []


# --------------------------------------------------------------------------
# In-memory fake S3 for the end-to-end command behavior
# --------------------------------------------------------------------------
class _FakePaginator:
    def __init__(self, store: dict[str, tuple[int, dt.datetime]], *, page_size: int = 1000) -> None:
        self._store = store
        self._page_size = page_size

    def paginate(  # noqa: N803
        self,
        *,
        Bucket: str,  # noqa: N803
        Prefix: str = "",  # noqa: N803
        Delimiter: str | None = None,  # noqa: N803
        **_: Any,
    ) -> list[dict[str, Any]]:
        if Delimiter == "/":
            # Mirror list_objects_v2's CommonPrefixes grouping, split across pages
            # so the paginated delimiter scan is exercised over >1 page.
            prefixes: list[str] = []
            seen: set[str] = set()
            for key in sorted(self._store):
                if not key.startswith(Prefix):
                    continue
                head, sep, _ = key[len(Prefix) :].partition("/")
                if sep and head not in seen:
                    seen.add(head)
                    prefixes.append(f"{Prefix}{head}/")
            return [
                {"CommonPrefixes": [{"Prefix": p} for p in prefixes[i : i + self._page_size]]}
                for i in range(0, max(len(prefixes), 1), self._page_size)
            ]
        contents = [
            {"Key": k, "Size": sz, "LastModified": mt}
            for k, (sz, mt) in sorted(self._store.items())
            if k.startswith(Prefix)
        ]
        return [
            {"Contents": contents[i : i + self._page_size]}
            for i in range(0, max(len(contents), 1), self._page_size)
        ]


class _FakeS3:
    """Minimal S3 backed by {key: (size, last_modified)} supporting the calls
    cleanup_s3 makes: list_objects_v2 (with Delimiter for subprefixes),
    a list_objects_v2 paginator, and delete_objects."""

    def __init__(
        self,
        store: dict[str, tuple[int, dt.datetime]],
        *,
        page_size: int = 1000,
        delete_errors: set[str] | None = None,
    ) -> None:
        self.store = dict(store)
        self.deleted: list[str] = []
        self._page_size = page_size
        # Keys delete_objects should report as failures (HTTP 200 + per-key Errors).
        self._delete_errors = delete_errors or set()

    def list_objects_v2(  # noqa: N802
        self,
        *,
        Bucket: str,
        Prefix: str = "",
        Delimiter: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        if Delimiter == "/":
            prefixes: set[str] = set()
            for key in self.store:
                if not key.startswith(Prefix):
                    continue
                rest = key[len(Prefix) :]
                head, sep, _ = rest.partition("/")
                if sep:
                    prefixes.add(f"{Prefix}{head}/")
            return {"CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)]}
        contents = [{"Key": k} for k in sorted(self.store) if k.startswith(Prefix)]
        return {"Contents": contents}

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self.store, page_size=self._page_size)

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        keys = [o["Key"] for o in Delete["Objects"]]
        errors = [
            {"Key": k, "Code": "AccessDenied", "Message": "fake failure"}
            for k in keys
            if k in self._delete_errors
        ]
        # Only the successfully-deleted keys are removed from the store / recorded.
        for k in keys:
            if k not in self._delete_errors:
                self.deleted.append(k)
                self.store.pop(k, None)
        # Mirror Quiet=True: successes omitted, per-key Errors still returned on 200.
        return {"Errors": errors} if errors else {}


def _store() -> dict[str, tuple[int, dt.datetime]]:
    return {
        # old run (deletable): every BAM artifact suffix + a small artifact preserved
        "runs/old/wgs/c6a/rep-1/aligned.bam": (1_000, _ts(1)),
        "runs/old/wgs/c6a/rep-1/aligned.bam.bai": (10, _ts(1)),
        "runs/old/wgs/c6a/rep-1/aligned.bam.csi": (10, _ts(1)),
        "runs/old/wgs/c6a/rep-1/benchmarks/timing.tsv": (5, _ts(1)),
        # newest run (kept by keep_latest=1)
        "runs/new/wgs/c6a/rep-1/aligned.bam": (2_000, _ts(9)),
        # a golden SHA's run (protected) — older than `old`
        "runs/gold/wgs/c6a/rep-1/aligned.bam": (3_000, _ts(2)),
        "golden/fg-labs-gold/wgs/c6a/aligned.bam": (3_000, _ts(2)),
        # root workflow-source bundles
        "snakemake-workflow-sources.aaa.tar.xz": (9, _ts(1)),
        "snakemake-workflow-sources.bbb.tar.xz": (9, _ts(2)),
    }


def test_preview_without_force_deletes_nothing() -> None:
    fake = _FakeS3(_store())
    with patch.object(aws_module, "_s3", return_value=fake):
        aws_module.cleanup_s3(keep_latest=1, force=False)
    assert fake.deleted == []


def test_force_deletes_only_old_nongolden_bams() -> None:
    fake = _FakeS3(_store())
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=0),
    ):
        aws_module.cleanup_s3(keep_latest=1, force=True)
    deleted = set(fake.deleted)
    # old run's BAM + every index sidecar (.bai/.csi) go; the newest run, the
    # golden-protected run, and the small timing.tsv artifact are all preserved.
    assert deleted == {
        "runs/old/wgs/c6a/rep-1/aligned.bam",
        "runs/old/wgs/c6a/rep-1/aligned.bam.bai",
        "runs/old/wgs/c6a/rep-1/aligned.bam.csi",
    }
    assert "runs/new/wgs/c6a/rep-1/aligned.bam" not in deleted
    assert "runs/gold/wgs/c6a/rep-1/aligned.bam" not in deleted
    assert "runs/old/wgs/c6a/rep-1/benchmarks/timing.tsv" not in deleted


def test_force_refuses_when_jobs_active() -> None:
    fake = _FakeS3(_store())
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=2),
    ):
        aws_module.cleanup_s3(keep_latest=1, force=True)
    assert fake.deleted == []  # refused — a worker may still be uploading


def test_workflow_sources_flag_includes_tarballs() -> None:
    fake = _FakeS3(_store())
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=0),
    ):
        aws_module.cleanup_s3(keep_latest=1, workflow_sources=True, force=True)
    assert "snakemake-workflow-sources.aaa.tar.xz" in fake.deleted
    assert "snakemake-workflow-sources.bbb.tar.xz" in fake.deleted


def test_workflow_sources_not_touched_by_default() -> None:
    fake = _FakeS3(_store())
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=0),
    ):
        aws_module.cleanup_s3(keep_latest=1, force=True)
    assert not any(k.startswith("snakemake-workflow-sources.") for k in fake.deleted)


def test_paginated_golden_and_run_scans_see_all_prefixes() -> None:
    """With a page size of 1, the paginated delimiter scans must still discover
    every run SHA and every golden SHA (regression: first-page-only scans)."""
    fake = _FakeS3(_store(), page_size=1)
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=0),
    ):
        aws_module.cleanup_s3(keep_latest=1, force=True)
    deleted = set(fake.deleted)
    # The golden-protected run (discovered only via a later golden page) survives,
    # and the old run (a later runs/ page) is still selected for deletion.
    assert "runs/gold/wgs/c6a/rep-1/aligned.bam" not in deleted
    assert deleted == {
        "runs/old/wgs/c6a/rep-1/aligned.bam",
        "runs/old/wgs/c6a/rep-1/aligned.bam.bai",
        "runs/old/wgs/c6a/rep-1/aligned.bam.csi",
    }


def test_delete_errors_raise_and_count_only_successes() -> None:
    """A per-key Error on an HTTP-200 delete_objects must surface as a failure
    (nonzero exit), not be silently counted as a successful deletion."""
    failing = "runs/old/wgs/c6a/rep-1/aligned.bam.bai"
    fake = _FakeS3(_store(), delete_errors={failing})
    with (
        patch.object(aws_module, "_s3", return_value=fake),
        patch.object(aws_module, "_active_job_count", return_value=0),
        pytest.raises(SystemExit) as exc_info,
    ):
        aws_module.cleanup_s3(keep_latest=1, force=True)
    assert exc_info.value.code == 1
    # The failing key was not removed; the other was.
    assert failing not in fake.deleted
    assert "runs/old/wgs/c6a/rep-1/aligned.bam" in fake.deleted
