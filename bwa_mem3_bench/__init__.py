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


def minibwa_sha() -> str:
    """Return the pinned lh3/minibwa SHA from ``docker/build-arg-defaults.env``.

    Single source of truth shared by the Snakemake workflow (cache-path keying)
    and ``cli collect`` (trial ingest). Raises if the key is absent so a missing
    pin fails loudly rather than silently producing an empty cache prefix.
    """
    for line in _BUILD_ARG_DEFAULTS.read_text().splitlines():
        if line.startswith("MINIBWA_SHA="):
            sha = line.split("=", 1)[1].strip()
            if sha:
                return sha
    raise ValueError(f"MINIBWA_SHA not set in {_BUILD_ARG_DEFAULTS}")
