"""Validate standalone reference-indexing configuration before constructing the DAG.

`config.schema.json` describes the same contract for the biffix form; the tests check that the
two agree on defaults, assembly names, FASTA paths and the executable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
BUCKET_PATTERN = r"[a-z0-9][a-z0-9.-]*[a-z0-9]"
FASTA_SUFFIXES = (".fa", ".fasta")
DEFAULTS: dict[str, Any] = {
    "threads": 16,
    "mem_mb": 98304,
    "emit_unpacked_ref": False,
    "binary": "bwa-mem2.fg-labs",
}
MIB = 1024 * 1024
MIN_RESERVE_MIB = 2048


@dataclass(frozen=True)
class IndexConfig:
    """Validated inputs and the CPU/memory reservation for each assembly."""

    assemblies: dict[str, str]
    threads: int
    mem_mb: int
    emit_unpacked_ref: bool
    binary: str


def validate_reference(name: str, reference: object) -> str:
    """Require an absolute local FASTA or an explicit S3 URI, without traversal or wildcards."""
    if not isinstance(reference, str) or not reference:
        raise ValueError(f"assembly {name!r}: FASTA path must be a nonempty string")

    def reject(reason: str) -> ValueError:
        return ValueError(f"assembly {name!r}: {reason}: {reference!r}")

    if any(not c.isprintable() or c in "{}\\?#%" for c in reference):
        raise reject("FASTA paths cannot contain control characters or any of {}\\?#%")
    if reference.startswith("s3://"):
        bucket, _, key = reference[len("s3://") :].partition("/")
        if not re.fullmatch(BUCKET_PATTERN, bucket):
            raise reject("S3 FASTA paths need a lowercase bucket name (s3://bucket/key)")
        components = key.split("/")
    elif reference.startswith("/"):
        components = reference[1:].split("/")
    else:
        raise reject("FASTA paths must be absolute local paths or s3://bucket/key URIs")
    if any(part in {"", ".", ".."} for part in components):
        raise reject("FASTA paths cannot contain empty, '.' or '..' components")
    basename = components[-1]
    if not any(
        basename.endswith(suffix) and len(basename) > len(suffix) for suffix in FASTA_SUFFIXES
    ):
        raise reject("FASTA filenames must be <name>.fa or <name>.fasta (uncompressed)")
    return reference


def validate_binary(binary: object) -> str:
    """Accept a bare executable name found on PATH, or an absolute executable path."""
    if not isinstance(binary, str) or not binary or any(c in binary for c in "\n\r\t{}"):
        raise ValueError(f"binary must be an executable name or absolute path: {binary!r}")
    if not binary.startswith("/") and ("/" in binary or any(c.isspace() for c in binary)):
        raise ValueError(f"binary must be an executable name or absolute path: {binary!r}")
    return binary


def validate_config(raw: dict[Any, Any]) -> IndexConfig:
    """Reject unknown keys and malformed values before any job or storage lookup."""
    allowed = {"assemblies", *DEFAULTS}
    if extra := [key for key in raw if key not in allowed]:
        raise ValueError(f"unknown config keys: {', '.join(sorted(map(repr, extra)))}")
    assemblies = raw.get("assemblies")
    if not isinstance(assemblies, dict) or not assemblies:
        raise ValueError("assemblies must be a nonempty map of names to FASTA paths")
    validated: dict[str, str] = {}
    folded: dict[str, str] = {}
    for name, reference in assemblies.items():
        if not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name):
            raise ValueError(f"assembly name {name!r} must match {NAME_PATTERN}")
        if (other := folded.setdefault(name.casefold(), name)) != name:
            raise ValueError(f"assembly names {other!r} and {name!r} differ only in case")
        validated[name] = validate_reference(name, reference)
    values = {key: raw.get(key, default) for key, default in DEFAULTS.items()}
    threads, mem_mb = values["threads"], values["mem_mb"]
    for key, value in (("threads", threads), ("mem_mb", mem_mb)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer: {value!r}")
    unpacked = values["emit_unpacked_ref"]
    if type(unpacked) is not bool:
        raise ValueError(f"emit_unpacked_ref must be a boolean: {unpacked!r}")
    return IndexConfig(validated, threads, mem_mb, unpacked, validate_binary(values["binary"]))


def fasta_basename(reference: str) -> str:
    """Preserve the source's exact filename, including its FASTA extension."""
    return PurePosixPath(reference).name


def index_memory_budget_bytes(mem_mb: int) -> int:
    """The indexer's --max-memory for a job holding `mem_mb` MiB.

    Mirrors the indexer's own automatic budget (memory less a reserve of
    min(max(2 GiB, 5%), 50%)), so the build's preflight check measures against what the job can
    actually use rather than the whole container limit.
    """
    reserve_mib = min(max(MIN_RESERVE_MIB, mem_mb // 20), mem_mb // 2)
    return (mem_mb - reserve_mib) * MIB
