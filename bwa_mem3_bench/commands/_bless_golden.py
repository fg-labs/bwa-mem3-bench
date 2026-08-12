"""`bless-golden` — promote current fg-labs outputs to `s3://.../golden/fg-labs-<sha>/`."""

from __future__ import annotations

import subprocess
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

# An aligned-BAM key relative to runs/<sha>/ is <sample>/<arch>/rep-<N>/aligned.bam.
_BAM_KEY_PARTS = 4


def _parse_s3_bams(
    ls_output: str, *, bucket: str, fg_labs_sha: str, dry_run: bool = False
) -> list[tuple[str, str]]:
    """Map `aws s3 ls --recursive runs/<sha>/` output to (src, golden-dest) pairs.

    Selects only `<sample>/<arch>/rep-1/aligned.bam` keys and rewrites each to the
    de-repped `golden/fg-labs-<sha>/<sample>/<arch>/aligned.bam` destination.

    For parity with the local-tree branch, emits a stderr note for any
    `(sample, arch)` cell whose rep-1 is blessed while additional reps exist
    (suppressed under ``dry_run``).
    """
    prefix = f"runs/{fg_labs_sha}/"
    pairs: list[tuple[str, str]] = []
    blessed_cells: set[tuple[str, str]] = set()
    extra_reps: dict[tuple[str, str], int] = {}
    for line in ls_output.splitlines():
        parts = line.split()
        if not parts:
            continue
        key = parts[-1]
        if not key.startswith(prefix) or not key.endswith("/aligned.bam"):
            continue
        rel = key[len(prefix) :].split("/")
        # <sample>/<arch>/rep-<N>/aligned.bam
        if len(rel) != _BAM_KEY_PARTS or not rel[2].startswith("rep-"):
            continue
        sample, arch = rel[0], rel[1]
        if rel[2] == "rep-1":
            src = f"s3://{bucket}/{key}"
            dest = f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/{sample}/{arch}/aligned.bam"
            pairs.append((src, dest))
            blessed_cells.add((sample, arch))
        else:
            extra_reps[(sample, arch)] = extra_reps.get((sample, arch), 0) + 1

    if not dry_run:
        for (sample, arch), count in sorted(extra_reps.items()):
            if (sample, arch) in blessed_cells:
                print(
                    f"note: blessing rep-1 only for {sample}/{arch}; "
                    f"ignoring {count} additional rep(s)",
                    file=sys.stderr,
                )
    return pairs


def bless_golden(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    bucket: str = _DEFAULT_BUCKET,
    force: bool = False,
    from_s3: bool = False,
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

    With ``from_s3=True`` the source BAMs are read directly from
    `s3://<bucket>/runs/<sha>/` (S3→S3 copy) instead of a local ``runs/`` tree —
    needed for backfilling releases whose BAMs are only in S3 (``collect``
    excludes BAMs from the local mirror).
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

    copies: list[tuple[str, str]] = []
    if from_s3:
        listing = f"s3://{bucket}/runs/{fg_labs_sha}/"
        proc = subprocess.run(
            ["aws", "s3", "ls", "--recursive", listing],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            # We must capture stdout to parse it, so a check=True failure would
            # hide the real S3 error (bad creds, missing prefix, region mismatch)
            # behind an opaque CalledProcessError. Surface stderr explicitly.
            raise RuntimeError(
                f"aws s3 ls failed for {listing} (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        copies = _parse_s3_bams(
            proc.stdout, bucket=bucket, fg_labs_sha=fg_labs_sha, dry_run=dry_run
        )
    else:
        src_root = REPO_ROOT / "runs" / fg_labs_sha
        if not src_root.is_dir():
            raise FileNotFoundError(f"no local run at {src_root}; collect first (or --from-s3)")
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
                bam = arch_dir / "rep-1" / "aligned.bam"
                if not bam.exists():
                    continue
                dest = (
                    f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/"
                    f"{sample_dir.name}/{arch_dir.name}/aligned.bam"
                )
                copies.append((str(bam), dest))

    if not copies:
        print("no rep-1 aligned.bam files to bless", file=sys.stderr)
        return

    for src, dest in copies:
        run_cmd(["aws", "s3", "cp", src, dest], dry_run=dry_run)
