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
