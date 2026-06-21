"""Helpers for the pinned previous-release golden (Gate #2).

The vs-golden comparison runs every aligned cell against a fixed, previously
blessed reference under ``s3://<bucket>/golden/fg-labs-<sha>/``. A golden blessed
before a sample existed does not contain that sample, so requesting vs-golden for
it makes ``rule all`` unsatisfiable (a ``MissingInputException`` on the absent
golden BAM). These helpers let the workflow request vs-golden only for the
samples the pinned golden actually contains.
"""

from __future__ import annotations

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
