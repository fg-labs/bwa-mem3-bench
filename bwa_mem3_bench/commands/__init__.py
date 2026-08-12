"""CLI subcommand implementations."""

from bwa_mem3_bench.commands import aws, bench
from bwa_mem3_bench.commands._bless_baseline import bless_baseline
from bwa_mem3_bench.commands._bless_golden import bless_golden
from bwa_mem3_bench.commands._build import build, build_base
from bwa_mem3_bench.commands._collect import collect
from bwa_mem3_bench.commands._submit import submit
from bwa_mem3_bench.commands._sync_local import sync_local
from bwa_mem3_bench.commands._upload_data import upload_data
from bwa_mem3_bench.commands._watch import watch

__all__ = [
    "aws",
    "bench",
    "bless_baseline",
    "bless_golden",
    "build",
    "build_base",
    "collect",
    "submit",
    "sync_local",
    "upload_data",
    "watch",
]
