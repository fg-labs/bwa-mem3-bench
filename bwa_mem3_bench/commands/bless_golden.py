"""`bless-golden` — promote current fg-labs outputs to `s3://.../golden/fg-labs-<sha>/`."""

from __future__ import annotations

import sys
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    allowance_for,
    load_allowances,
)

_DEFAULT_BUCKET = aws_config.load().bucket


def bless_golden(
    *,
    fg_labs_sha: str,
    bucket: str = _DEFAULT_BUCKET,
    force: bool = False,
    allowances_path: Path = DEFAULT_ALLOWANCES_PATH,
    dry_run: bool = False,
) -> None:
    """Copy `runs/<sha>/<sample>/<arch>/rep-1/aligned.bam` files to
    `s3://<bucket>/golden/fg-labs-<sha>/<sample>/<arch>/aligned.bam`.

    This is a deliberate action: it locks in a new fg-labs reference for
    regression gating (Gate #2). Moving the golden forward must be signed off in
    `docs/release-allowances.yaml` recording the intentional alignment change;
    `bless-golden` refuses a SHA no allowance authorizes. Pass ``force=True`` only
    for the very first golden. Only the first rep of each (sample, arch) is used.
    """
    if not force:
        allowances = load_allowances(allowances_path) if allowances_path.exists() else []
        if allowance_for(allowances, fg_labs_sha) is None:
            raise ValueError(
                f"refusing to bless {fg_labs_sha} as golden: no entry in "
                f"docs/release-allowances.yaml authorizes it. Blessing moves the "
                f"Gate #2 reference, so it must be signed off — add an allowance "
                f"(to_sha, pr, date, summary, expected_drift_pct) for the intentional "
                f"alignment change, or pass --force for the initial golden."
            )

    src_root = REPO_ROOT / "runs" / fg_labs_sha
    if not src_root.is_dir():
        raise FileNotFoundError(f"no local run at {src_root}; collect first")

    copies: list[tuple[Path, str]] = []
    for sample_dir in sorted(d for d in src_root.iterdir() if d.is_dir()):
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            higher_reps = sorted(
                d
                for d in arch_dir.iterdir()
                if d.is_dir() and d.name.startswith("rep-") and d.name != "rep-1"
            )
            if higher_reps and not dry_run:
                print(
                    f"note: blessing rep-1 only for {sample_dir.name}/{arch_dir.name}; "
                    f"ignoring {len(higher_reps)} additional rep(s)",
                    file=sys.stderr,
                )
            rep_dir = arch_dir / "rep-1"
            bam = rep_dir / "aligned.bam"
            if not bam.exists():
                continue
            dest = (
                f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/"
                f"{sample_dir.name}/{arch_dir.name}/aligned.bam"
            )
            copies.append((bam, dest))

    if not copies:
        print("no aligned.bam files to bless under runs/<sha>", file=sys.stderr)
        return

    for src, dest in copies:
        run_cmd(["aws", "s3", "cp", str(src), dest], dry_run=dry_run)
