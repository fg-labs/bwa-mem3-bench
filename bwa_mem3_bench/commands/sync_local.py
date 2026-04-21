"""`sync-local` — mirror S3 bucket prefixes to local scratch for offline work."""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench import LOCAL_MIRROR_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd

_DEFAULT_BUCKET = aws_config.load().bucket

_PREFIXES = ("references", "data", "runs", "baseline", "golden")


def sync_local(
    *,
    what: str = "references,data",
    bucket: str = _DEFAULT_BUCKET,
    dest: Path = LOCAL_MIRROR_ROOT,
    dry_run: bool = False,
) -> None:
    """Mirror S3 bucket prefixes to a local mirror directory.

    Useful for offline debugging — once mirrored, reproducers can read the
    reference and fastqs directly without re-downloading per invocation.

    :param what: comma-separated subset of `references,data,runs,baseline,golden`
        or the special value `all`. Default pulls only references + data, which
        is what bug reproducers usually need.
    :param bucket: source bucket. Default from `cdk/outputs.json` /
        `BWA_MEM3_BENCH_S3_BUCKET`.
    :param dest: local destination root. Default `LOCAL_MIRROR_ROOT`
        (override via `BWA_MEM3_BENCH_LOCAL_MIRROR`).
    :param dry_run: print commands only.
    """
    prefixes: tuple[str, ...]
    if what == "all":
        prefixes = _PREFIXES
    else:
        prefixes = tuple(p.strip() for p in what.split(",") if p.strip())
        unknown = [p for p in prefixes if p not in _PREFIXES]
        if unknown:
            raise ValueError(f"unknown prefix(es) {unknown}; known: {list(_PREFIXES)} or 'all'")

    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    for prefix in prefixes:
        src = f"s3://{bucket}/{prefix}/"
        local = dest / prefix
        if not dry_run:
            local.mkdir(parents=True, exist_ok=True)
        run_cmd(
            ["aws", "s3", "sync", src, str(local), "--only-show-errors"],
            dry_run=dry_run,
        )
