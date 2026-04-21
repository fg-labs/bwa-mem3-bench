"""Top-level `bwa-mem3-bench` CLI wired via `defopt`."""

from __future__ import annotations

import sys

import defopt

from bwa_mem3_bench.commands import aws as aws_module
from bwa_mem3_bench.commands import bench as bench_module
from bwa_mem3_bench.commands import (
    bless_baseline,
    bless_golden,
    build,
    collect,
    submit,
    sync_local,
    upload_data,
    watch,
)
from bwa_mem3_bench.render_profile import render_profile


def main(argv: list[str] | None = None) -> None:
    """Entry point used by the `bwa-mem3-bench` console script."""
    defopt.run(
        {
            "build": build,
            "submit": submit,
            "collect": collect,
            "bless-baseline": bless_baseline,
            "bless-golden": bless_golden,
            "upload-data": upload_data,
            "render-profile": render_profile,
            "sync-local": sync_local,
            "watch": watch,
            "bench": {
                "summary": bench_module.summary,
                "report": bench_module.report,
                "compare": bench_module.compare,
                "regression": bench_module.regression,
                "trend": bench_module.trend,
                "full-report": bench_module.full_report,
                "speedup": bench_module.speedup,
            },
            "aws": {
                "jobs": aws_module.jobs,
                "describe": aws_module.describe,
                "kill": aws_module.kill,
                "kill-all": aws_module.kill_all,
                "logs": aws_module.logs,
                "setup": aws_module.setup,
                "teardown": aws_module.teardown,
                "cost": aws_module.cost,
                "cleanup": aws_module.cleanup,
            },
        },
        argv=argv if argv is not None else sys.argv[1:],
    )


if __name__ == "__main__":
    main()
