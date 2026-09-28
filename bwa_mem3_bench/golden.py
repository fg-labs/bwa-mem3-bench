"""Helpers for the pinned previous-release golden (Gate #2).

The vs-golden comparison runs every aligned cell against a fixed, previously
blessed reference under ``s3://<bucket>/golden/fg-labs-<sha>/``. A golden blessed
before a sample existed does not contain that sample, so requesting vs-golden for
it makes ``rule all`` unsatisfiable (a ``MissingInputException`` on the absent
golden BAM). These helpers let the workflow request vs-golden only for the
samples the pinned golden actually contains.

That scoping cannot tell a sample the golden legitimately predates from one an
interrupted `bless-golden` failed to copy, so the module also checks a golden
against its own run tree (:func:`missing_golden_cells`), which `bless-golden`
and the `bless-release` preflight use to catch a partial copy.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
from pathlib import PurePosixPath
from typing import Any

import boto3
import botocore.config

from bwa_mem3_bench import aws_config

# Wall-clock cap on the whole golden listing. It runs while the Snakefile parses
# (on the coordinator and again on every worker), so a stalled S3 endpoint must
# fail the parse promptly rather than hold it. Socket timeouts alone cannot give
# this bound: they apply per read and per attempt, and not to DNS resolution.
_LS_WALL_CLOCK_SECONDS = 30

# Per-attempt socket limits, kept well inside the wall-clock cap so a transient
# failure can still be retried within it.
_LS_CONNECT_TIMEOUT_SECONDS = 5
_LS_READ_TIMEOUT_SECONDS = 10
_LS_TOTAL_ATTEMPTS = 2

# A golden is keyed by the full 40-hex SHA it blessed, optionally with a
# `-<build_variant>` suffix. Anything else can never match a golden prefix.
_GOLDEN_SHA_RE = re.compile(r"[0-9a-f]{40}(-[A-Za-z0-9._-]+)?")


def _s3_client() -> Any:
    """An S3 client for listings made while the workflow parses.

    The region is resolved like the ``aws`` CLI this replaced (``AWS_REGION``, then
    ``AWS_DEFAULT_REGION`` and the profile via boto3), falling back to the
    configured deploy region. Empty values count as unset.
    """
    config = botocore.config.Config(
        connect_timeout=_LS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_LS_READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": _LS_TOTAL_ATTEMPTS, "mode": "standard"},
    )
    region = (
        os.environ.get("AWS_REGION")
        or boto3.session.Session().region_name
        or aws_config.load().region
    )
    return boto3.client("s3", region_name=region, config=config)


def _list_sample_prefixes(bucket: str, prefix: str) -> frozenset[str]:
    """The immediate sub-prefix names of ``s3://<bucket>/<prefix>``."""
    samples: set[str] = set()
    pages = (
        _s3_client()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
    )
    for page in pages:
        for entry in page.get("CommonPrefixes", []):
            name = PurePosixPath(entry.get("Prefix", "")).name
            if name:
                samples.add(name)
    return frozenset(samples)


def golden_backed_samples(bucket: str, golden_ref_sha: str) -> frozenset[str]:
    """Sample names that have a blessed golden under ``golden/fg-labs-<sha>/``.

    Lists one level of the prefix with boto3 rather than the ``aws`` CLI: this runs
    while the Snakefile parses, and a coordinator need not ship the CLI (a
    control plane's Snakemake runtime carries boto3 but no ``aws`` binary).

    Returns an empty set when the golden prefix has no entries (e.g. an
    unblessed SHA) -- that is a benign "nothing to compare against", not an error.
    Everything else is raised, since silently treating it as "no samples" would
    skip Gate #2 without anyone noticing: a ``golden_ref_sha`` that is not a full
    SHA (a short SHA or a tag can never match a golden prefix), any S3 failure
    (credentials, missing bucket, region), and a listing that does not finish
    within ``_LS_WALL_CLOCK_SECONDS``.
    """
    if not _GOLDEN_SHA_RE.fullmatch(golden_ref_sha):
        raise RuntimeError(
            f"golden_ref_sha {golden_ref_sha!r} is not a full 40-hex SHA (optionally "
            f"with a -<build_variant> suffix); a golden is keyed by the full SHA, so "
            f"this would silently match no golden and skip the vs-golden gate"
        )
    prefix = f"golden/fg-labs-{golden_ref_sha}/"
    uri = f"s3://{bucket}/{prefix}"
    result: queue.Queue[frozenset[str] | BaseException] = queue.Queue(maxsize=1)

    def _list() -> None:
        try:
            result.put(_list_sample_prefixes(bucket, prefix))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            result.put(exc)

    # A daemon thread, so a listing still stalled when the parse fails cannot hold
    # the interpreter open at exit.
    threading.Thread(target=_list, name="golden-listing", daemon=True).start()
    try:
        outcome = result.get(timeout=_LS_WALL_CLOCK_SECONDS)
    except queue.Empty:
        raise RuntimeError(
            f"listing {uri} did not finish within {_LS_WALL_CLOCK_SECONDS}s"
        ) from None
    if isinstance(outcome, BaseException):
        raise RuntimeError(f"listing {uri} failed: {outcome}") from outcome
    return outcome


# A full `runs/<sha>/` listing covers every sample x arch x rep of a bless sweep,
# thousands of keys; give it more headroom than the one-level golden listing.
_LS_RECURSIVE_TIMEOUT_SECONDS = 300

# An aligned-BAM key relative to runs/<sha>/ is <sample>/<arch>/rep-<N>/aligned.bam.
_RUN_BAM_PARTS = 4

# A golden BAM key relative to golden/fg-labs-<sha>/ is <sample>/<arch>/aligned.bam.
_GOLDEN_BAM_PARTS = 3


# A replicate directory is `rep-<N>` for a positive integer N with no leading zero.
_REP_DIR_RE = re.compile(r"rep-[1-9][0-9]*")


def is_rep_dir(name: str) -> bool:
    """Whether ``name`` is a replicate directory (``rep-<N>``, ``N >= 1``).

    Rejects look-alikes such as ``rep-backup`` or ``rep-0`` so a stray directory
    cannot inflate a cell's rep count past the release minimum.
    """
    return _REP_DIR_RE.fullmatch(name) is not None


def _keys(ls_output: str) -> list[str]:
    """The object keys of a recursive ``aws s3 ls`` listing (last column of each row)."""
    return [parts[-1] for line in ls_output.splitlines() if (parts := line.split())]


def parse_run_reps(ls_output: str, fg_labs_sha: str) -> dict[tuple[str, str], int]:
    """Map a recursive ``runs/<sha>/`` listing to ``{(sample, arch): rep count}``.

    Counts only ``<sample>/<arch>/rep-<N>/aligned.bam`` keys (see :func:`is_rep_dir`),
    so compare JSONs, timing files, and stray non-replicate directories do not
    inflate the count.
    """
    prefix = f"runs/{fg_labs_sha}/"
    reps: dict[tuple[str, str], int] = {}
    for key in _keys(ls_output):
        if not key.startswith(prefix) or not key.endswith("/aligned.bam"):
            continue
        rel = key[len(prefix) :].split("/")
        if len(rel) != _RUN_BAM_PARTS or not is_rep_dir(rel[2]):
            continue
        cell = (rel[0], rel[1])
        reps[cell] = reps.get(cell, 0) + 1
    return reps


def parse_golden_cells(ls_output: str, golden_ref_sha: str) -> frozenset[tuple[str, str]]:
    """Map a recursive ``golden/fg-labs-<sha>/`` listing to its ``(sample, arch)`` cells."""
    prefix = f"golden/fg-labs-{golden_ref_sha}/"
    cells: set[tuple[str, str]] = set()
    for key in _keys(ls_output):
        if not key.startswith(prefix) or not key.endswith("/aligned.bam"):
            continue
        rel = key[len(prefix) :].split("/")
        if len(rel) == _GOLDEN_BAM_PARTS:
            cells.add((rel[0], rel[1]))
    return frozenset(cells)


def list_recursive(uri: str) -> str:
    """Return ``aws s3 ls --recursive <uri>`` output, raising on a real S3 failure.

    An absent prefix is an empty listing (``aws`` exits 1 with no stderr), not an
    error -- the same contract :func:`golden_backed_samples` keeps. This one still
    shells out to the ``aws`` CLI: it runs only from the bless commands on an
    operator's machine, never while the workflow parses.
    """
    try:
        proc = subprocess.run(
            ["aws", "s3", "ls", "--recursive", uri],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LS_RECURSIVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"aws s3 ls timed out after {_LS_RECURSIVE_TIMEOUT_SECONDS}s for {uri}"
        ) from exc
    if proc.returncode != 0 and proc.stderr.strip():
        raise RuntimeError(
            f"aws s3 ls failed for {uri} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def missing_golden_cells(bucket: str, golden_ref_sha: str) -> list[tuple[str, str]]:
    """The ``(sample, arch)`` cells the run produced but its golden lacks, sorted.

    ``bless-golden`` copies each cell's ``rep-1`` BAM one ``aws s3 cp`` at a time,
    so an interrupted bless leaves a golden that is silently partial. Gate #2
    scopes vs-golden to the samples the golden contains (:func:`golden_backed_samples`),
    so the missing samples are then never compared at all -- the v0.12.0 golden
    lost every sample after ``sim-wgs-vars-compat`` this way, and v0.13.0 shipped
    with no vs-golden check on wgs-5M or wes-5M. The run tree is the reference for
    "complete": ``cleanup-s3`` keeps blessed-golden runs' BAMs.

    :raises RuntimeError: if the run tree holds no aligned BAMs, since there is
        then nothing to check the golden against -- reporting "nothing missing"
        would pass a golden that was never verified.
    """
    run_uri = f"s3://{bucket}/runs/{golden_ref_sha}/"
    run_cells = parse_run_reps(list_recursive(run_uri), golden_ref_sha)
    if not run_cells:
        raise RuntimeError(f"no aligned.bam files under {run_uri} to check the golden against")
    golden_cells = parse_golden_cells(
        list_recursive(f"s3://{bucket}/golden/fg-labs-{golden_ref_sha}/"), golden_ref_sha
    )
    return sorted(set(run_cells) - golden_cells)
