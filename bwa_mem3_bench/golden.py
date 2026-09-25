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

import re
import subprocess

# A `PRE <name>/` directory row from `aws s3 ls` splits into exactly two tokens.
_PRE_ROW_PARTS = 2

# Cap the golden listing so a network/DNS stall can't hang workflow init.
_LS_TIMEOUT_SECONDS = 30


def parse_golden_samples(ls_output: str) -> frozenset[str]:
    """Parse non-recursive ``aws s3 ls golden/fg-labs-<sha>/`` output to sample names.

    A non-recursive listing of a prefix renders each immediate subprefix as a
    ``PRE <name>/`` line. Each such ``<name>`` is a per-sample subdirectory of the
    golden; we ignore any non-``PRE`` rows (stray objects, blank lines).
    """
    samples: set[str] = set()
    for line in ls_output.splitlines():
        parts = line.split()
        if len(parts) == _PRE_ROW_PARTS and parts[0] == "PRE":
            samples.add(parts[1].rstrip("/"))
    return frozenset(samples)


def golden_backed_samples(bucket: str, golden_ref_sha: str) -> frozenset[str]:
    """Sample names that have a blessed golden under ``golden/fg-labs-<sha>/``.

    Returns an empty set when the golden prefix has no entries (e.g. an
    unblessed SHA) — that is a benign "nothing to compare against", not an error.
    A genuine S3 failure (bad credentials, region mismatch) writes to stderr and
    is raised, since silently treating it as "no samples" would skip Gate #2
    without anyone noticing.
    """
    prefix = f"s3://{bucket}/golden/fg-labs-{golden_ref_sha}/"
    try:
        proc = subprocess.run(
            ["aws", "s3", "ls", prefix],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"aws s3 ls timed out after {_LS_TIMEOUT_SECONDS}s for {prefix}"
        ) from exc
    if proc.returncode != 0 and proc.stderr.strip():
        raise RuntimeError(
            f"aws s3 ls failed for {prefix} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return parse_golden_samples(proc.stdout)


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
    error -- the same convention as :func:`golden_backed_samples`.
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
