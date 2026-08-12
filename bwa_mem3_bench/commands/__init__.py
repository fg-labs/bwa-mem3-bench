"""CLI subcommand implementations."""

from bwa_mem3_bench.commands import aws, bench
from bwa_mem3_bench.commands.bless_baseline import bless_baseline
from bwa_mem3_bench.commands.bless_golden import bless_golden
from bwa_mem3_bench.commands.build import build, build_base
from bwa_mem3_bench.commands.collect import collect
from bwa_mem3_bench.commands.submit import submit
from bwa_mem3_bench.commands.sync_local import sync_local
from bwa_mem3_bench.commands.upload_data import upload_data
from bwa_mem3_bench.commands.watch import watch

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
