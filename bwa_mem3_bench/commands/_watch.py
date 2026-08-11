"""`watch` — live rich progress display for a running coordinator Batch job."""

from __future__ import annotations

import re
import select
import sys
import termios
import tty
from collections import defaultdict
from typing import Any

import boto3
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from bwa_mem3_bench import aws_config

_cfg = aws_config.load()
COORDINATOR_QUEUE = _cfg.coordinator_queue
WORKER_QUEUES = _cfg.worker_queues
_LOG_GROUP = "/aws/batch/job"
_REGION = _cfg.region

_STATE_STYLES: dict[str, str] = {
    "SUBMITTED": "dim",
    "PENDING": "dim",
    "RUNNABLE": "yellow",
    "STARTING": "yellow",
    "RUNNING": "blue",
    "SUCCEEDED": "green",
    "FAILED": "red",
    "RETRIED": "dim red",
}

_STATE_ABBREVS: dict[str, str] = {
    "SUBMITTED": "SUB",
    "PENDING": "PND",
    "RUNNABLE": "RDY",
    "STARTING": "STA",
    "RUNNING": "RUN",
    "SUCCEEDED": "SUC",
    "FAILED": "FAL",
    "RETRIED": "RTR",
}

_ACTIVE_STATES = ["SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"]
_TERMINAL_STATES = ["SUCCEEDED", "FAILED"]
# RETRIED is computed in Python from the job set (a FAILED job whose rule has a
# successor by createdAt). Batch itself never reports RETRIED — keep it strictly
# separate from the AWS-state taxonomy used by coordinator-done logic.
_DERIVED_STATES = ["RETRIED"]
_ALL_STATES = _ACTIVE_STATES + _TERMINAL_STATES
_DISPLAY_STATES = _ACTIVE_STATES + _TERMINAL_STATES + _DERIVED_STATES

# snakemake-executor-plugin-aws-batch passes the snakemake target as
# `--target-jobs 'rule_name:wildcard1=v1,wildcard2=v2,...'` on the worker's
# command line. The string after `--target-jobs` uniquely identifies a rule
# instance — and crucially is STABLE across retries (snakemake re-submits the
# same target-jobs value with a fresh Batch jobName UUID after a spot kill).
# That makes it the right identity for retry-chain detection.
_TARGET_JOBS_PATTERN = re.compile(r"--target-jobs\s+['\"]([^'\"]+)['\"]")

_SECS_PER_MINUTE = 60
_SECS_PER_HOUR = 3600
_MIN_RULE_PARTS = 2

console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# AWS helpers (boto3-based)
# ---------------------------------------------------------------------------

_SNAKEJOB_PREFIX = "snakejob-"
_UUID_PATTERN_SUFFIX = r"-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"


def _batch_client() -> Any:
    return boto3.client("batch", region_name=_REGION)


def _logs_client() -> Any:
    return boto3.client("logs", region_name=_REGION)


def _get_coordinator_job() -> dict[str, Any] | None:
    """Find the most recently created active coordinator job in the coordinator queue."""
    client = _batch_client()
    for state in _ACTIVE_STATES:
        resp = client.list_jobs(jobQueue=COORDINATOR_QUEUE, jobStatus=state)
        jobs: list[dict[str, Any]] = resp.get("jobSummaryList", [])
        if jobs:
            jobs.sort(key=lambda j: j.get("createdAt", 0), reverse=True)
            return dict(jobs[0])
    return None


def _get_coordinator_job_by_id(job_id: str) -> dict[str, Any] | None:
    """Describe a specific Batch job by ID."""
    client = _batch_client()
    resp = client.describe_jobs(jobs=[job_id])
    jobs: list[dict[str, Any]] = resp.get("jobs", [])
    return dict(jobs[0]) if jobs else None


def _get_log_stream(job_id: str) -> str | None:
    """Get the CloudWatch log stream name for a Batch job via describe-jobs."""
    client = _batch_client()
    resp = client.describe_jobs(jobs=[job_id])
    jobs: list[dict[str, Any]] = resp.get("jobs", [])
    if not jobs:
        return None
    stream: str | None = jobs[0].get("container", {}).get("logStreamName")
    return stream


def _get_log_events(
    log_stream: str, *, limit: int = 500, start_from_head: bool = False
) -> list[dict[str, Any]]:
    """Fetch CloudWatch log events for a log stream."""
    client = _logs_client()
    kwargs: dict[str, Any] = {
        "logGroupName": _LOG_GROUP,
        "logStreamName": log_stream,
        "limit": limit,
        "startFromHead": start_from_head,
    }
    try:
        resp = client.get_log_events(**kwargs)
        events: list[dict[str, Any]] = resp.get("events", [])
        return events
    except Exception:  # noqa: BLE001
        return []


def _list_jobs_in_queue(queue: str, state: str) -> list[dict[str, Any]]:
    """List all jobs in the given queue with the given status (handles pagination)."""
    client = _batch_client()
    jobs: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"jobQueue": queue, "jobStatus": state}
    while True:
        resp = client.list_jobs(**kwargs)
        jobs.extend(resp.get("jobSummaryList", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return jobs


_DESCRIBE_JOBS_CHUNK = 100


def _describe_jobs(job_ids: list[str]) -> list[dict[str, Any]]:
    """
    Describe Batch jobs in 100-id chunks (Batch API limit).

    Deduplicates ``job_ids`` before chunking — Batch's ``describe-jobs`` rejects
    a request whose ``jobs`` list contains duplicates with ``ClientException:
    Jobs contains duplicates``. Duplicates can sneak in here legitimately
    because callers aggregate ``list_jobs`` results across multiple state
    queries, and a job that transitions states (e.g. STARTING → RUNNING)
    between calls will be returned by both. Order is preserved for stable
    debugging via ``dict.fromkeys``.
    """
    client = _batch_client()
    unique_ids = list(dict.fromkeys(job_ids))
    out: list[dict[str, Any]] = []
    for i in range(0, len(unique_ids), _DESCRIBE_JOBS_CHUNK):
        chunk = unique_ids[i : i + _DESCRIBE_JOBS_CHUNK]
        resp = client.describe_jobs(jobs=chunk)
        out.extend(resp.get("jobs", []))
    return out


def _extract_primary_output(job: dict[str, Any]) -> str | None:
    """
    Extract the snakemake `--target-jobs` identifier from a described job's
    command. Format is ``rule_name:wildcard1=v1,wildcard2=v2,...`` (a stable
    identifier across retries — see ``_TARGET_JOBS_PATTERN``).

    Returns None if the command isn't present (e.g. the job summary form
    without describe-jobs lookup) or no `--target-jobs` arg is found
    (non-snakemake jobs in the queue, the coordinator job itself).
    """
    container = job.get("container") or job.get("containerProperties") or {}
    command = container.get("command")
    if not command:
        return None
    joined = " ".join(command) if isinstance(command, list) else str(command)
    match = _TARGET_JOBS_PATTERN.search(joined)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Snakemake log parsing
# ---------------------------------------------------------------------------


def _parse_snakemake_progress(log_stream: str) -> tuple[int, int] | None:
    """Parse latest 'X of Y steps (Z%) done' from coordinator CloudWatch logs."""
    events = _get_log_events(log_stream, limit=500)
    pattern = re.compile(r"(\d+) of (\d+) steps \(\d+%\) done")
    last_match: tuple[int, int] | None = None
    for event in events:
        m = pattern.search(event.get("message", ""))
        if m:
            last_match = (int(m.group(1)), int(m.group(2)))
    return last_match


def _parse_job_plan(log_stream: str) -> tuple[dict[str, int], int | None]:
    """
    Parse the Snakemake 'Job stats' table from early coordinator logs.

    Returns a (plan, total) tuple: ``plan`` maps command label to expected job
    count; ``total`` is the overall step count from the table's ``total`` row
    (None if the row wasn't seen). The table is printed once at coordinator
    startup before any jobs are submitted — using it to seed the progress bar
    avoids a ``0/0`` stretch during the bootstrap window before the first
    ``X of Y steps (Z%) done`` line appears.
    """
    events = _get_log_events(log_stream, limit=200, start_from_head=True)
    lines = [e.get("message", "").rstrip() for e in events]
    plan: dict[str, int] = {}
    total: int | None = None
    in_table = False
    row_pattern = re.compile(r"^(\w+)\s+(\d+)\s*$")
    for line in lines:
        if line.startswith("Job stats:"):
            in_table = True
            continue
        if in_table:
            if line.startswith("---") or line.startswith("job "):
                continue
            m = row_pattern.match(line.strip())
            if m:
                rule, count = m.group(1), int(m.group(2))
                if rule == "total":
                    total = count
                    break
                cmd = _rule_to_command(rule)
                plan[cmd] = plan.get(cmd, 0) + count
            elif not line.strip():
                break
    return plan, total


# ---------------------------------------------------------------------------
# Rule name -> command label mapping
# ---------------------------------------------------------------------------


def _extract_rule_name(job_name: str) -> str | None:
    """Extract the Snakemake rule name from a job name like ``snakejob-<rule>-<uuid>``."""
    if not job_name.startswith(_SNAKEJOB_PREFIX):
        return None
    without_prefix = job_name[len(_SNAKEJOB_PREFIX) :]
    rule = re.sub(_UUID_PATTERN_SUFFIX, "", without_prefix)
    return rule if rule != without_prefix else None


def _rule_to_command(rule_name: str) -> str:
    """
    Map a Snakemake rule name to a short command label for the watch table.

    bwa-mem3-bench rules are of the form ``align_fg_labs_c8g``,
    ``index_fg_labs_c8g``, ``sort_fg_labs_c8g``, etc.  We keep the first two
    underscore-separated tokens as the label so similar rules group together.

    Examples::

        align_fg_labs_c8g   -> align_fg_labs
        align_upstream_c8g  -> align_upstream
        index_fg_labs_c8g   -> index_fg_labs
    """
    parts = rule_name.split("_")
    if len(parts) >= _MIN_RULE_PARTS:
        return "_".join(parts[:_MIN_RULE_PARTS])
    return rule_name


# ---------------------------------------------------------------------------
# Job counting
# ---------------------------------------------------------------------------


# Cache of jobId -> primary output S3 path. The output path is immutable for
# the lifetime of a job, so once we've described a job we never need to again.
# Bounded only by the number of worker jobs in the run, which is small (<1k).
_OUTPUT_CACHE: dict[str, str | None] = {}


def _get_or_fetch_outputs(jobs: list[dict[str, Any]]) -> dict[str, str | None]:
    """
    Return jobId -> primary S3 output path for the given job summaries.

    Cache hits avoid the describe-jobs round-trip; misses are batched into a
    single describe-jobs call (chunked to the 100-id Batch limit). The cache
    persists across watch ticks.
    """
    # Dedup at this boundary as well: ``jobs`` aggregates list_jobs results
    # across multiple state queries and the same jobId can appear twice if it
    # transitioned state mid-poll. ``_describe_jobs`` dedups too, but doing it
    # here as well keeps ``missing`` honest and the ``setdefault`` loop below
    # from iterating duplicates.
    missing = list(dict.fromkeys(j["jobId"] for j in jobs if j["jobId"] not in _OUTPUT_CACHE))
    if missing:
        for described in _describe_jobs(missing):
            _OUTPUT_CACHE[described["jobId"]] = _extract_primary_output(described)
        # Defensive: any id we asked about but didn't get back is recorded as
        # None so we don't keep re-requesting it.
        for jid in missing:
            _OUTPUT_CACHE.setdefault(jid, None)
    return {j["jobId"]: _OUTPUT_CACHE.get(j["jobId"]) for j in jobs}


def _classify_display_states(jobs: list[dict[str, Any]]) -> dict[str, str]:
    """
    Compute the display state for each job, applying retry-chain detection.

    A FAILED job is reclassified as ``RETRIED`` when another job for the same
    ``(rule_name, primary_output_path)`` was created later (by ``createdAt``).
    All other jobs keep their Batch state.

    Jobs whose rule name or primary output cannot be determined fall back to
    ``(rule_name, jobId)`` as identity, which means they form a singleton group
    and are never marked retried — the conservative interpretation when we
    can't prove a successor exists.
    """
    outputs = _get_or_fetch_outputs(jobs)

    identity_to_jobs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        rule = _extract_rule_name(job["jobName"]) or job["jobName"]
        output = outputs.get(job["jobId"])
        identity = (rule, output) if output else (rule, job["jobId"])
        identity_to_jobs[identity].append(job)

    display_states: dict[str, str] = {}
    for chain in identity_to_jobs.values():
        chain.sort(key=lambda j: j.get("createdAt", 0))
        latest_id = chain[-1]["jobId"]
        for job in chain:
            state = job.get("status", "")
            if state == "FAILED" and job["jobId"] != latest_id:
                display_states[job["jobId"]] = "RETRIED"
            else:
                display_states[job["jobId"]] = state
    return display_states


def _count_jobs_by_command_and_state(
    *, since: int = 0
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """
    Fetch all worker-queue jobs and group counts by command label and state.

    Counts are summed across all WORKER_QUEUES.  Only jobs created at or after
    ``since`` (epoch-ms) are included when ``since > 0``.

    FAILED jobs that have a more recently-created successor for the same
    ``(rule, primary_output)`` are reclassified to the derived ``RETRIED``
    state at display time (the underlying Batch state remains FAILED).
    """
    cmd_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    state_totals: dict[str, int] = {s: 0 for s in _DISPLAY_STATES}

    all_jobs: list[dict[str, Any]] = []
    for state in _ALL_STATES:
        for queue in WORKER_QUEUES:
            jobs = _list_jobs_in_queue(queue, state)
            if since:
                jobs = [j for j in jobs if j.get("createdAt", 0) >= since]
            # Stamp the listed status onto each summary so the classifier can
            # see it (list_jobs already returns ``status`` but be explicit).
            for job in jobs:
                job.setdefault("status", state)
            all_jobs.extend(jobs)

    display_states = _classify_display_states(all_jobs)

    for job in all_jobs:
        rule = _extract_rule_name(job["jobName"])
        display = display_states.get(job["jobId"], job.get("status", ""))
        state_totals[display] = state_totals.get(display, 0) + 1
        if rule:
            cmd = _rule_to_command(rule)
            cmd_counts[cmd][display] += 1

    return dict(cmd_counts), state_totals


# ---------------------------------------------------------------------------
# ETA helpers
# ---------------------------------------------------------------------------


def _format_eta(seconds: float) -> str:
    """Format an ETA in seconds into a short human-readable string."""
    if seconds < _SECS_PER_MINUTE:
        return f"~{int(seconds)}s"
    if seconds < _SECS_PER_HOUR:
        minutes = int(seconds) // _SECS_PER_MINUTE
        secs = int(seconds) % _SECS_PER_MINUTE
        return f"~{minutes}m{secs:02d}s" if secs else f"~{minutes}m"
    hours = int(seconds) // _SECS_PER_HOUR
    minutes = (int(seconds) % _SECS_PER_HOUR) // _SECS_PER_MINUTE
    return f"~{hours}h{minutes:02d}m" if minutes else f"~{hours}h"


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def _fmt_delta(d: int) -> str:
    """Format a delta value: green for positive, red for negative, dim dot for zero."""
    if d > 0:
        return f"[green]+{d}[/green]"
    if d < 0:
        return f"[red]{d}[/red]"
    return "[dim]·[/dim]"


def _add_summary_rows(  # noqa: PLR0913
    table: Table,
    *,
    visible_states: list[str],
    total_by_state: dict[str, int],
    delta_by_state: dict[str, int],
    grand_total: int,
    grand_succeeded: int,
    grand_queued: int,
    delta_queued: int,
    delta_total: int,
    has_plan: bool,
    has_prev: bool,
) -> None:
    """Append totals and delta rows to the command status table."""
    totals_row: list[str] = ["[bold]Total[/bold]"]
    if has_plan:
        totals_row.append(f"[bold]{grand_queued}[/bold]" if grand_queued > 0 else "[dim]·[/dim]")
    for state in visible_states:
        c = total_by_state.get(state, 0)
        totals_row.append(f"[bold]{c}[/bold]" if c > 0 else "[dim]·[/dim]")
    totals_row.append(f"[bold]{grand_total}[/bold]")
    grand_pct = f"{grand_succeeded * 100 // grand_total}%" if grand_total > 0 else ""
    if grand_total > 0 and grand_succeeded == grand_total:
        totals_row.append(f"[bold green]{grand_pct}[/bold green]")
    else:
        totals_row.append(f"[bold]{grand_pct}[/bold]")
    table.add_row(*totals_row, end_section=True)

    if has_prev:
        delta_row: list[str] = ["[dim]Delta[/dim]"]
        if has_plan:
            delta_row.append(_fmt_delta(delta_queued))
        for state in visible_states:
            delta_row.append(_fmt_delta(delta_by_state.get(state, 0)))
        delta_row.append(_fmt_delta(delta_total))
        delta_row.append("")  # Done column
        table.add_row(*delta_row)


def _build_command_status_table(  # noqa: PLR0915
    cmd_counts: dict[str, dict[str, int]],
    job_plan: dict[str, int] | None = None,
    prev_cmd_counts: dict[str, dict[str, int]] | None = None,
) -> Table:
    """
    Build a Rich Table showing per-command job state breakdown.

    Args:
        cmd_counts: Mapping of command label to {state: count}.
        job_plan: Optional expected job counts per command from the coordinator's
                  job plan.  When provided an extra *QUE* column shows how many
                  jobs have not yet been submitted.
        prev_cmd_counts: Previous poll's cmd_counts for computing deltas.
    """
    if not cmd_counts and not job_plan:
        empty: Table = Table.grid()
        empty.add_row("[dim]No worker jobs yet[/dim]")
        return empty

    all_cmds = sorted(set(cmd_counts) | set(job_plan or {}))
    visible_states = list(_DISPLAY_STATES)

    table = Table(title=None, box=None, pad_edge=False, show_edge=False, padding=(0, 1))
    table.add_column("Command", style="bold", no_wrap=True)
    if job_plan:
        table.add_column("QUE", justify="right", style="dim")
    for state in visible_states:
        style = _STATE_STYLES.get(state, "")
        table.add_column(_STATE_ABBREVS.get(state, state[:3]), justify="right", style=style)
    table.add_column("Total", justify="right", style="bold")
    table.add_column("Done", justify="right")

    total_by_state: dict[str, int] = {}
    delta_by_state: dict[str, int] = {}
    grand_total = 0
    grand_succeeded = 0
    grand_queued = 0
    delta_queued = 0
    delta_total = 0

    for cmd in all_cmds:
        counts = cmd_counts.get(cmd, {})
        observed = sum(counts.values())
        expected = job_plan.get(cmd, 0) if job_plan else 0
        total = max(observed, expected)
        succeeded = counts.get("SUCCEEDED", 0)
        pct = f"{succeeded * 100 // total}%" if total > 0 else ""

        grand_total += total
        grand_succeeded += succeeded

        prev_counts = (prev_cmd_counts or {}).get(cmd, {})
        prev_observed = sum(prev_counts.values())
        prev_expected = job_plan.get(cmd, 0) if job_plan else 0
        prev_total = max(prev_observed, prev_expected)

        row: list[str] = [cmd]
        if job_plan:
            queued = expected - observed
            prev_queued = prev_expected - prev_observed
            grand_queued += max(queued, 0)
            delta_queued += queued - prev_queued
            row.append(str(queued) if queued > 0 else "[dim]·[/dim]")
        delta_total += total - prev_total
        for state in visible_states:
            c = counts.get(state, 0)
            total_by_state[state] = total_by_state.get(state, 0) + c
            d = c - prev_counts.get(state, 0)
            delta_by_state[state] = delta_by_state.get(state, 0) + d
            row.append(str(c) if c > 0 else "[dim]·[/dim]")
        row.append(str(total))
        if total > 0 and succeeded == total:
            row.append(f"[green]{pct}[/green]")
        else:
            row.append(pct)
        table.add_row(*row)

    _add_summary_rows(
        table,
        visible_states=visible_states,
        total_by_state=total_by_state,
        delta_by_state=delta_by_state,
        grand_total=grand_total,
        grand_succeeded=grand_succeeded,
        grand_queued=grand_queued,
        delta_queued=delta_queued,
        delta_total=delta_total,
        has_plan=bool(job_plan),
        has_prev=prev_cmd_counts is not None,
    )

    return table


def _format_job_status_line(counts: dict[str, int]) -> str:
    """Format job counts into a styled status string."""
    parts = []
    for state in _DISPLAY_STATES:
        c = counts.get(state, 0)
        if c > 0:
            style = _STATE_STYLES.get(state, "")
            parts.append(f"[{style}]{state}: {c}[/{style}]")
    return "Jobs: " + "  ".join(parts) if parts else "No jobs"


def _build_watch_layout(  # noqa: PLR0913
    cmd_counts: dict[str, dict[str, int]],
    job_plan: dict[str, int] | None,
    progress: Progress,
    counts: dict[str, int],
    *,
    countdown: int = 0,
    refreshing: bool = False,
    prev_cmd_counts: dict[str, dict[str, int]] | None = None,
) -> Table:
    """Build the full Rich layout for the watch command."""
    layout: Table = Table.grid()
    layout.add_row(
        _build_command_status_table(cmd_counts, job_plan=job_plan, prev_cmd_counts=prev_cmd_counts)
    )
    layout.add_row("")
    layout.add_row(progress)
    status_table: Table = Table.grid()
    status = _format_job_status_line(counts)
    if refreshing:
        status += "  [dim]Refreshing...[/dim]"
    elif countdown > 0:
        status += f"  [dim]Next refresh in {countdown}s (r=refresh, q=quit, ?=help)[/dim]"
    status_table.add_row(status)
    layout.add_row(status_table)
    return layout


def _build_help_panel() -> Panel:
    """Build a help panel explaining columns and key bindings."""
    grid: Table = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan")
    grid.add_column()

    grid.add_row("", "[bold underline]Columns[/bold underline]")
    grid.add_row("Command", "Snakemake rule name (first two tokens)")
    grid.add_row("QUE", "Planned jobs not yet submitted by coordinator")
    grid.add_row("RDY", "RUNNABLE — submitted, waiting for compute")
    grid.add_row("STA", "STARTING — container launching")
    grid.add_row("RUN", "RUNNING — actively executing")
    grid.add_row("SUC", "SUCCEEDED — completed successfully")
    grid.add_row("FAL", "FAILED — exited with error")
    grid.add_row("RTR", "RETRIED — failed but a successor job is in progress / succeeded")
    grid.add_row("Total", "Max of observed and planned job count")
    grid.add_row("Done", "Percentage of jobs succeeded")
    grid.add_row("", "")
    grid.add_row("", "[bold underline]Key Bindings[/bold underline]")
    grid.add_row("r", "Refresh immediately")
    grid.add_row("q", "Quit")
    grid.add_row("?", "Show this help")

    return Panel(grid, title="[bold]Help[/bold]", subtitle="[dim]Press any key to close[/dim]")


def _show_help(live: Live) -> None:
    """Display the help panel and wait for any keypress to dismiss."""
    live.update(_build_help_panel())
    while True:
        if select.select([sys.stdin], [], [], 0.5)[0]:
            sys.stdin.read(1)
            return


def _update_snakemake_progress(
    log_stream: str,
    progress: Progress,
    task_id: TaskID,
    total_steps: int,
) -> int:
    """
    Parse Snakemake progress from coordinator logs and update the progress bar.

    Returns the (possibly updated) total_steps value.
    """
    sm_progress = _parse_snakemake_progress(log_stream)
    if sm_progress:
        completed, total = sm_progress
        if total != total_steps:
            total_steps = total
            progress.update(task_id, total=total_steps)
        pct = f"{completed * 100 // total_steps}%" if total_steps else ""
        progress.update(task_id, completed=completed, pct=pct)
    return total_steps


def _poll_watch_data(
    state: dict[str, Any],
    coord_id: str,
    coord_since: int,
    progress: Progress,
    task_id: TaskID,
) -> int:
    """
    Poll AWS for job status and Snakemake progress.

    Updates ``state`` in-place with current job counts and Snakemake step
    progress.  Returns the number of active (non-terminal) child jobs.
    """
    log_stream = state.get("log_stream")
    if log_stream is None:
        log_stream = _get_log_stream(coord_id)
        state["log_stream"] = log_stream

    job_plan = state.get("job_plan")
    if log_stream:
        if job_plan is None:
            job_plan, plan_total = _parse_job_plan(log_stream)
            if job_plan:
                state["job_plan"] = job_plan
            # Seed the progress bar's denominator from the Job stats table so
            # the UI shows `?/N` immediately instead of `0/0` for the 2–3 min
            # it takes snakemake to emit its first `X of Y steps` line.
            if plan_total and state.get("total_steps", 0) == 0:
                state["total_steps"] = plan_total
                progress.update(task_id, total=plan_total)
        state["total_steps"] = _update_snakemake_progress(
            log_stream, progress, task_id, state.get("total_steps", 0)
        )

    cmd_counts, counts = _count_jobs_by_command_and_state(since=coord_since)
    state["cmd_counts"] = cmd_counts
    state["counts"] = counts

    return sum(counts.get(s, 0) for s in _ACTIVE_STATES)


def _wait_for_refresh(  # noqa: PLR0913
    interval: int,
    cmd_counts: dict[str, dict[str, int]],
    job_plan: dict[str, int] | None,
    progress: Progress,
    counts: dict[str, int],
    live: Live,
    prev_cmd_counts: dict[str, dict[str, int]] | None = None,
) -> None:
    """
    Wait for the countdown interval, or until the user presses 'r' to refresh.

    Raises KeyboardInterrupt if the user presses 'q'.
    """
    for remaining in range(interval, 0, -1):
        layout = _build_watch_layout(
            cmd_counts,
            job_plan,
            progress,
            counts,
            countdown=remaining,
            prev_cmd_counts=prev_cmd_counts,
        )
        live.update(layout)
        if select.select([sys.stdin], [], [], 1.0)[0]:
            ch = sys.stdin.read(1)
            if ch.lower() == "r":
                return
            if ch == "q":
                raise KeyboardInterrupt
            if ch == "?":
                _show_help(live)


# ---------------------------------------------------------------------------
# Public CLI entry point
# ---------------------------------------------------------------------------


def _resolve_coordinator(job_id: str) -> tuple[str, str, str, int]:
    """
    Resolve coordinator job_id, coord_name, commit, and coord_since timestamp.

    When ``job_id`` is empty, auto-detects the most recently created active
    coordinator in COORDINATOR_QUEUE.

    Returns (job_id, coord_name, commit, coord_since_ms).
    """
    if not job_id:
        coordinator = _get_coordinator_job()
        if not coordinator:
            err_console.print(
                f"[red]No active coordinator found in {COORDINATOR_QUEUE}.[/red]\n"
                "Is a benchmark running?  Pass --job-id to watch a specific job."
            )
            sys.exit(1)
        job_id = str(coordinator["jobId"])
        coord_name = str(coordinator.get("jobName", "?"))
        coord_since = int(coordinator.get("createdAt", 0))
    else:
        described = _get_coordinator_job_by_id(job_id)
        coord_name = str(described.get("jobName", "?")) if described else job_id
        coord_since = int(described.get("createdAt", 0)) if described else 0

    # Extract commit SHA from job name: last hyphen-separated token that looks
    # like a git short-hash (7–40 hex chars).
    match = re.search(r"-([a-f0-9]{7,40})$", coord_name)
    commit = match.group(1) if match else "unknown"

    return job_id, coord_name, commit, coord_since


def watch(*, job_id: str = "", interval: int = 30, once: bool = False) -> None:
    """Watch benchmark progress with a live rich progress display.

    Finds the running coordinator job, parses Snakemake's step progress from its
    CloudWatch logs, and polls AWS Batch job counts on an interval.

    Press 'r' during the countdown to trigger an immediate refresh.
    Press 'q' to quit.  Press '?' to show column / key-binding help.

    :param job_id: Coordinator job ID. Auto-detects if empty.
    :param interval: Seconds between polls.
    :param once: Poll once and exit (useful for scripting / CI checks).
    """
    job_id, coord_name, commit, coord_since = _resolve_coordinator(job_id)

    console.print(f"[bold]Watching:[/bold] {coord_name}")
    console.print(f"[bold]Commit:[/bold] {commit}")
    console.print()

    state: dict[str, Any] = {}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[pct]}"),
        TimeElapsedColumn(),
    )
    task_id = progress.add_task("Snakemake steps", total=0, pct="")

    def _layout(*, refreshing: bool = False, countdown: int = 0) -> Table:
        return _build_watch_layout(
            state.get("cmd_counts", {}),
            state.get("job_plan"),
            progress,
            state.get("counts", {}),
            refreshing=refreshing,
            countdown=countdown,
            prev_cmd_counts=state.get("prev_cmd_counts"),
        )

    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        with Live(progress, console=console, refresh_per_second=4) as live:
            while True:
                live.update(_layout(refreshing=True))

                prev_snapshot = {cmd: dict(c) for cmd, c in state.get("cmd_counts", {}).items()}

                active = _poll_watch_data(state, job_id, coord_since, progress, task_id)

                if prev_snapshot:
                    state["prev_cmd_counts"] = prev_snapshot

                live.update(_layout())

                if once:
                    break

                # Exit only when the coordinator itself has finished AND no
                # workers are running. Plain `active == 0` exits during the
                # bootstrap window before the coordinator has submitted any
                # workers (pip install, workflow deploy, DAG resolution).
                coord = _get_coordinator_job_by_id(job_id)
                coord_status = coord.get("status") if coord else None
                if active == 0 and coord_status in _TERMINAL_STATES:
                    total_steps = state.get("total_steps", 0)
                    if total_steps > 0:
                        progress.update(task_id, completed=total_steps, pct="100%")
                    break

                _wait_for_refresh(
                    interval,
                    state.get("cmd_counts", {}),
                    state.get("job_plan"),
                    progress,
                    state.get("counts", {}),
                    live,
                    prev_cmd_counts=state.get("prev_cmd_counts"),
                )

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    console.print()
    log_stream = state.get("log_stream")
    total_steps = state.get("total_steps", 0)
    if total_steps > 0 and log_stream:
        sm_progress = _parse_snakemake_progress(log_stream)
        if sm_progress:
            completed, total = sm_progress
            if completed == total:
                console.print(f"[green]Benchmark complete: {completed}/{total} steps[/green]")
            else:
                console.print(f"[cyan]Stopped watching at {completed}/{total} steps[/cyan]")
    else:
        console.print("[dim]Stopped watching[/dim]")
