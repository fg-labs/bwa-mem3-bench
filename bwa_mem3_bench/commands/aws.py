"""`aws` — job-inspection subcommands (list, describe, kill, logs)."""

from __future__ import annotations

import datetime as dt
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd

_cfg = aws_config.load()
_REGION = _cfg.region
_COORDINATOR_QUEUE = _cfg.coordinator_queue
_WORKER_QUEUES = _cfg.worker_queues
_ARENA_QUEUES = _cfg.arena_queues
_ALL_QUEUES = (_COORDINATOR_QUEUE, *_WORKER_QUEUES, *_ARENA_QUEUES)
_ACTIVE_STATES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")
_TERMINAL_STATES = ("SUCCEEDED", "FAILED")
_ALL_STATES = (*_ACTIVE_STATES, *_TERMINAL_STATES)

_console = Console()


def _batch() -> Any:
    return boto3.client("batch", region_name=_REGION)


def _logs() -> Any:
    return boto3.client("logs", region_name=_REGION)


def _s3() -> Any:
    return boto3.client("s3", region_name=_REGION)


def _list_jobs(queue: str, status: str) -> list[dict[str, Any]]:
    client = _batch()
    out: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_jobs")
    for page in paginator.paginate(jobQueue=queue, jobStatus=status):
        out.extend(page.get("jobSummaryList", []))
    return out


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%H:%M:%S")


def jobs(*, queue: str = "", status: str = "") -> None:
    """List Batch jobs across the project's queues.

    :param queue: Restrict to one queue (e.g. `bwa-mem3-bench-c8g`). Empty = all.
    :param status: Restrict to one status (e.g. `RUNNING`). Empty = all active.
    """
    queues = (queue,) if queue else _ALL_QUEUES
    statuses = (status,) if status else _ACTIVE_STATES

    rows: list[tuple[str, str, str, str, str]] = []
    for q in queues:
        for s in statuses:
            for j in _list_jobs(q, s):
                rows.append(
                    (
                        j["jobId"],
                        j["jobName"],
                        q.replace("bwa-mem3-bench-", ""),
                        s,
                        _fmt_ts(j.get("createdAt")),
                    )
                )

    if not rows:
        _console.print("[dim]no matching jobs[/dim]")
        return

    table = Table(title=f"Batch jobs ({len(rows)})")
    table.add_column("jobId")
    table.add_column("jobName")
    table.add_column("queue")
    table.add_column("status")
    table.add_column("createdAt", justify="right")
    for r in sorted(rows, key=lambda row: row[4]):
        table.add_row(*r)
    _console.print(table)


def describe(job_id: str) -> None:
    """Show full details for a single Batch job.

    :param job_id: Batch jobId.
    """
    resp = _batch().describe_jobs(jobs=[job_id])
    jobs_resp = resp.get("jobs") or []
    if not jobs_resp:
        _console.print(f"[red]no such job: {job_id}[/red]")
        sys.exit(1)
    j = jobs_resp[0]

    container = j.get("container") or {}
    attempts = j.get("attempts") or []

    table = Table(title=j.get("jobName", job_id), show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("jobId", j.get("jobId", ""))
    table.add_row("queue", (j.get("jobQueue") or "").split("/")[-1])
    table.add_row("jobDefinition", (j.get("jobDefinition") or "").split("/")[-1])
    table.add_row("status", j.get("status", ""))
    table.add_row("statusReason", j.get("statusReason", "") or "")
    table.add_row("created", _fmt_ts(j.get("createdAt")))
    table.add_row("started", _fmt_ts(j.get("startedAt")))
    table.add_row("stopped", _fmt_ts(j.get("stoppedAt")))
    table.add_row("vcpus", str(container.get("vcpus", "")))
    table.add_row("memoryMB", str(container.get("memory", "")))
    table.add_row("image", container.get("image", "") or "")
    table.add_row("exitCode", str(container.get("exitCode", "") or ""))
    table.add_row("exitReason", container.get("reason", "") or "")
    table.add_row("logStream", container.get("logStreamName", "") or "")
    table.add_row("attempts", str(len(attempts)))
    _console.print(table)

    env = container.get("environment") or []
    if env:
        t = Table(title="environment")
        t.add_column("name", style="bold")
        t.add_column("value")
        for e in env:
            t.add_row(e.get("name", ""), e.get("value", ""))
        _console.print(t)


def kill(*job_ids: str, reason: str = "user-terminated via bwa-mem3-bench aws kill") -> None:
    """Terminate (cancel or stop) one or more Batch jobs.

    :param job_ids: Batch jobIds to terminate.
    :param reason: Termination reason recorded on the job.
    """
    if not job_ids:
        _console.print("[red]no job IDs given[/red]")
        sys.exit(2)

    client = _batch()
    for jid in job_ids:
        try:
            client.terminate_job(jobId=jid, reason=reason)
            _console.print(f"[green]terminated[/green] {jid}")
        except ClientError as e:
            _console.print(f"[red]failed[/red] {jid}: {e}")


def kill_all(
    *, queue: str = "", reason: str = "bulk terminate via bwa-mem3-bench aws kill-all"
) -> None:
    """Terminate every active Batch job in the project's queues.

    :param queue: Restrict to one queue. Empty = all project queues.
    :param reason: Termination reason recorded on each job.
    """
    queues = (queue,) if queue else _ALL_QUEUES
    total = 0
    for q in queues:
        for s in _ACTIVE_STATES:
            for j in _list_jobs(q, s):
                kill(j["jobId"], reason=reason)
                total += 1
    _console.print(f"[bold]terminated {total} job(s)[/bold]")


def logs(job_id: str, *, tail: int = 100) -> None:
    """Fetch the last N lines of a job's CloudWatch log stream.

    :param job_id: Batch jobId.
    :param tail: Number of most-recent log lines to show. Default 100.
    """
    resp = _batch().describe_jobs(jobs=[job_id])
    jobs_resp = resp.get("jobs") or []
    if not jobs_resp:
        _console.print(f"[red]no such job: {job_id}[/red]")
        sys.exit(1)

    container = jobs_resp[0].get("container") or {}
    stream = container.get("logStreamName")
    if not stream:
        _console.print("[yellow]job has no log stream yet (not started?)[/yellow]")
        return

    resp = _logs().get_log_events(
        logGroupName="/aws/batch/job",
        logStreamName=stream,
        limit=tail,
        startFromHead=False,
    )
    events = resp.get("events") or []
    for e in events:
        ts = _fmt_ts(e.get("timestamp"))
        msg = e.get("message", "")
        print(f"[{ts}] {msg}")


def _account() -> str:
    return str(boto3.client("sts", region_name=_REGION).get_caller_identity()["Account"])


def setup(*, skip_bootstrap: bool = False, dry_run: bool = False) -> None:
    """One-shot CDK deploy: bootstrap + deploy --all. Run once per account.

    :param skip_bootstrap: skip `cdk bootstrap` (e.g. already bootstrapped).
    :param dry_run: print commands without executing.
    """
    if not skip_bootstrap:
        run_cmd(
            ["cdk", "bootstrap", f"aws://{_account()}/{_REGION}"],
            dry_run=dry_run,
            cwd=REPO_ROOT / "cdk",
        )
    run_cmd(
        ["cdk", "deploy", "--all", "--require-approval", "never"],
        dry_run=dry_run,
        cwd=REPO_ROOT / "cdk",
    )


def teardown(*, force: bool = False, dry_run: bool = False) -> None:
    """Destroy all CDK stacks. Destructive. Requires --force.

    :param force: must be set to True to allow destruction.
    :param dry_run: print commands without executing.
    """
    if not force:
        _console.print("[red]refusing without --force; this deletes all infrastructure[/red]")
        sys.exit(2)
    run_cmd(
        ["cdk", "destroy", "--all", "--force"],
        dry_run=dry_run,
        cwd=REPO_ROOT / "cdk",
    )


def cost(*, days: int = 7) -> None:
    """Show Cost Explorer spend for Project=bwa-mem3-bench over the last N days.

    :param days: number of days to look back. Default 7.
    """
    ce = boto3.client("ce", region_name="us-east-1")
    end = dt.datetime.now(dt.UTC).date()
    start = end - dt.timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Tags": {"Key": "Project", "Values": ["bwa-mem3-bench"]}},
    )
    table = Table(title=f"Cost (last {days} days, Project=bwa-mem3-bench)")
    table.add_column("date")
    table.add_column("amount (USD)", justify="right")
    total = 0.0
    for row in resp.get("ResultsByTime", []):
        amt = float(row["Total"]["UnblendedCost"]["Amount"])
        total += amt
        table.add_row(row["TimePeriod"]["Start"], f"{amt:.2f}")
    table.add_row("[bold]total[/bold]", f"[bold]{total:.2f}[/bold]")
    _console.print(table)


def cleanup(*, keep_latest: int = 10, dry_run: bool = False) -> None:
    """Deregister old Batch job definitions, keeping only the most recent N.

    :param keep_latest: number of active revisions to keep. Default 10.
    :param dry_run: print what would be deregistered without doing it.
    """
    client = _batch()
    resp = client.describe_job_definitions(
        jobDefinitionName="bwa-mem3-bench-coordinator",
        status="ACTIVE",
        maxResults=100,
    )
    defs = sorted(
        resp.get("jobDefinitions", []),
        key=lambda d: d["revision"],
        reverse=True,
    )
    to_remove = defs[keep_latest:]
    for d in to_remove:
        arn = d["jobDefinitionArn"]
        if dry_run:
            _console.print(f"[dry-run] deregister {arn}")
        else:
            client.deregister_job_definition(jobDefinition=arn)
            _console.print(f"[green]deregistered[/green] {arn}")
    _console.print(f"[bold]kept {min(len(defs), keep_latest)} / {len(defs)}[/bold]")


# Suffixes of the large per-run alignment artifacts that `cleanup_s3` removes.
# Everything else under runs/<sha>/ (timing.tsv, *.json compares, eval TSVs,
# meta.json — the small things `collect` ingests) is preserved.
_BAM_SUFFIXES = (".bam", ".bam.bai", ".bam.csi")
# S3 DeleteObjects caps a single request at 1000 keys.
_DELETE_BATCH = 1000
# How many failed-delete keys to name in the error summary before truncating.
_DELETE_ERROR_SAMPLE = 5


def _active_job_count() -> int:
    """Total Batch jobs in a non-terminal state across every project queue.

    A nonzero count means a worker may still be uploading into ``runs/``, so
    deleting run outputs then could race an in-flight upload (see the
    kill-then-wipe gotcha in CLAUDE.md).
    """
    return sum(len(_list_jobs(q, s)) for q in _ALL_QUEUES for s in _ACTIVE_STATES)


def _golden_shas(s3: Any, bucket: str) -> set[str]:
    """fg-labs SHAs that have a blessed golden under ``golden/fg-labs-<sha>/``.

    These are protected from run-BAM deletion so a blessed release's source run
    tree stays intact even when it is no longer among the most recent runs.
    """
    paginator = s3.get_paginator("list_objects_v2")
    shas: set[str] = set()
    # Paginate: a bucket with >1000 golden prefixes would otherwise drop some,
    # leaving a blessed release's run tree unprotected from deletion.
    for page in paginator.paginate(Bucket=bucket, Prefix="golden/fg-labs-", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # "golden/fg-labs-<sha>/" -> "<sha>"
            name = cp["Prefix"].removeprefix("golden/fg-labs-").rstrip("/")
            if name:
                shas.add(name)
    return shas


def _run_bams_by_sha(s3: Any, bucket: str) -> dict[str, tuple[float, list[tuple[str, int]]]]:
    """Map each ``runs/<sha>/`` to its (newest-object-epoch, [(bam_key, size)]).

    The timestamp is the max ``LastModified`` over the run's objects, used to
    rank runs by recency; the key list is restricted to the BAM artifacts.
    """
    paginator = s3.get_paginator("list_objects_v2")
    # Enumerate the immediate run-SHA subprefixes, paginating so a bucket with
    # >1000 run SHAs doesn't silently omit the older ones from cleanup selection.
    shas: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix="runs/", Delimiter="/"):
        shas.extend(
            cp["Prefix"].removeprefix("runs/").rstrip("/") for cp in page.get("CommonPrefixes", [])
        )
    out: dict[str, tuple[float, list[tuple[str, int]]]] = {}
    for sha in shas:
        newest = 0.0
        bams: list[tuple[str, int]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"runs/{sha}/"):
            for obj in page.get("Contents", []):
                newest = max(newest, obj["LastModified"].timestamp())
                if obj["Key"].endswith(_BAM_SUFFIXES):
                    bams.append((obj["Key"], int(obj["Size"])))
        out[sha] = (newest, bams)
    return out


def _select_run_bams_to_delete(
    runs: dict[str, tuple[float, list[tuple[str, int]]]],
    golden_shas: set[str],
    keep_latest: int,
) -> list[tuple[str, int]]:
    """The (bam_key, size) pairs to delete: BAMs of every run SHA that is neither
    among the ``keep_latest`` most-recent runs nor a blessed golden.

    Pure selection logic (no I/O) so the keep/protect policy is unit-testable.
    """
    ordered = sorted(runs, key=lambda sha: runs[sha][0], reverse=True)
    protected = set(ordered[: max(keep_latest, 0)]) | golden_shas
    return [pair for sha in ordered if sha not in protected for pair in runs[sha][1]]


def _workflow_source_keys(s3: Any, bucket: str) -> list[str]:
    """Root ``snakemake-workflow-sources.*.tar.xz`` bundles (per-run staging
    archives the executor uploads; orphaned once a run is done)."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix="snakemake-workflow-sources."):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".tar.xz"):
                keys.append(obj["Key"])
    return keys


def cleanup_s3(
    *,
    keep_latest: int = 3,
    workflow_sources: bool = False,
    force: bool = False,
) -> None:
    """Delete large aligned BAMs from old ``runs/<sha>/`` trees, keeping recent runs.

    Removes only ``*.bam`` / ``*.bam.bai`` / ``*.bam.csi`` under
    ``runs/<fg-labs-sha>/`` for the oldest run SHAs. Preserved: the
    ``keep_latest`` most-recent runs, every SHA with a blessed golden under
    ``golden/fg-labs-<sha>/``, all the small per-run artifacts ``collect``
    ingests (timing/compare/eval/meta), and the ``baseline/`` & ``minibwa/``
    caches (expensive, reused across runs). Optionally also removes the orphaned
    root ``snakemake-workflow-sources.*.tar.xz`` bundles.

    Safe by default: without ``force`` this only prints the deletion plan.
    Refuses to delete while any project Batch job is active, since a running
    worker may still be uploading into ``runs/``.

    :param keep_latest: number of most-recent run SHAs whose BAMs are kept.
    :param workflow_sources: also delete root ``snakemake-workflow-sources.*.tar.xz``.
    :param force: actually delete; without it this is a no-op preview.
    """
    bucket = _cfg.bucket
    s3 = _s3()
    golden = _golden_shas(s3, bucket)
    runs = _run_bams_by_sha(s3, bucket)
    victims = _select_run_bams_to_delete(runs, golden, keep_latest)
    ws_keys = _workflow_source_keys(s3, bucket) if workflow_sources else []

    n_keep = min(max(keep_latest, 0), len(runs))
    total_gb = sum(size for _, size in victims) / 1e9
    _console.print(
        f"[bold]cleanup-s3[/bold] bucket=s3://{bucket}\n"
        f"  run SHAs: {len(runs)} total — keeping {n_keep} most-recent "
        f"+ {len(golden)} golden-protected\n"
        f"  BAMs to delete: {len(victims)} ({total_gb:.1f} GB)\n"
        f"  workflow-source archives to delete: {len(ws_keys)}"
    )

    keys = [k for k, _ in victims] + ws_keys
    if not keys:
        _console.print("[green]nothing to clean[/green]")
        return
    if not force:
        _console.print("[yellow]preview only — re-run with --force to delete[/yellow]")
        return

    active = _active_job_count()
    if active:
        _console.print(
            f"[red]refusing to delete: {active} active Batch job(s) — a worker may "
            f"still be uploading into runs/. Wait for all jobs to reach a terminal "
            f"state (cli aws jobs) and retry.[/red]"
        )
        return

    deleted = 0
    errors: list[dict[str, Any]] = []
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = keys[i : i + _DELETE_BATCH]
        resp = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        # delete_objects can return HTTP 200 with per-key Errors; Quiet=True
        # omits the successful keys, so count successes as batch minus failures.
        batch_errors = resp.get("Errors", [])
        errors.extend(batch_errors)
        deleted += len(batch) - len(batch_errors)
    if errors:
        sample = ", ".join(
            f"{e.get('Key')} ({e.get('Code')})" for e in errors[:_DELETE_ERROR_SAMPLE]
        )
        suffix = " …" if len(errors) > _DELETE_ERROR_SAMPLE else ""
        _console.print(
            f"[red]deleted {deleted} objects, but {len(errors)} failed: {sample}{suffix}[/red]"
        )
        sys.exit(1)
    _console.print(f"[green]deleted {deleted} objects ({total_gb:.1f} GB freed)[/green]")
