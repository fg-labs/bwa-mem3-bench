"""bwa-mem3-bench — benchmarking suite for bwa-mem3 vs upstream bwa-mem2 v2.2.1."""

import os
from pathlib import Path

__version__ = "0.1.0"

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "benchmarks" / "benchmark.db"

# Local mirror of the S3 bucket. `collect` downloads run/baseline artifacts
# here (BAMs excluded — only logs, benchmarks, compare JSONs). Override with
# the BWA_MEM3_BENCH_LOCAL_MIRROR env var if you want the mirror somewhere
# other than the repo's `local-mirror/` (e.g. a fast external SSD).
LOCAL_MIRROR_ROOT = Path(
    os.environ.get("BWA_MEM3_BENCH_LOCAL_MIRROR", str(REPO_ROOT / "local-mirror"))
)

# Canonical pin for the vendored lh3/minibwa source. The submodule commit at
# `vendor/minibwa` is the real source of truth for the build, but its SHA is
# also recorded here (in `docker/build-arg-defaults.env`) so the workflow and
# the SQLite ingest can key minibwa cache outputs / trial rows on it WITHOUT a
# git call. Keep the two in sync: bump the submodule, then bump MINIBWA_SHA.
_BUILD_ARG_DEFAULTS = REPO_ROOT / "docker" / "build-arg-defaults.env"


def _build_arg_default(key: str) -> str:
    """Return a non-empty ``KEY=value`` default from ``build-arg-defaults.env``.

    Raises if the key is absent or empty so a missing pin fails loudly rather
    than silently producing an empty value downstream.
    """
    prefix = f"{key}="
    for line in _BUILD_ARG_DEFAULTS.read_text().splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise ValueError(f"{key} not set in {_BUILD_ARG_DEFAULTS}")


def minibwa_sha() -> str:
    """Return the pinned lh3/minibwa SHA from ``docker/build-arg-defaults.env``.

    Single source of truth shared by the Snakemake workflow (cache-path keying)
    and ``cli collect`` (trial ingest).
    """
    return _build_arg_default("MINIBWA_SHA")


def holodeck_ref() -> str:
    """Return the pinned fg-labs/holodeck git ref from ``build-arg-defaults.env``.

    Single source of truth for the holodeck commit the Docker image builds
    `holodeck eval` from, and for accuracy-cache keying / ingest. Bump it (and
    rebuild) to pull in a new holodeck; while fg-labs/holodeck#20 is in draft
    this points at that branch's SHA, to be moved to a tagged release on merge.
    """
    return _build_arg_default("HOLODECK_REF")


def holodeck_repo() -> str:
    """Return the fg-labs/holodeck repository URL from ``build-arg-defaults.env``.

    Pinned alongside :func:`holodeck_ref` so the Dockerfile's holodeck repo and
    ref stay resolved from a single source rather than a hardcoded URL.
    """
    return _build_arg_default("HOLODECK_REPO")
