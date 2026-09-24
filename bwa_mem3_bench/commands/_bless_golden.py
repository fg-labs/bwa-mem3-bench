"""`bless-golden` — promote current fg-labs outputs to `s3://.../golden/fg-labs-<sha>/`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.golden import (
    is_rep_dir,
    list_recursive,
    parse_golden_cells,
    parse_run_reps,
)
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    allowance_for,
    load_allowances,
)
from bwa_mem3_bench.workflow_config import load_config

_DEFAULT_BUCKET = aws_config.load().bucket

# An aligned-BAM key relative to runs/<sha>/ is <sample>/<arch>/rep-<N>/aligned.bam.
_BAM_KEY_PARTS = 4

# How many missing golden cells to name in the error before summarizing the rest.
_MAX_MISSING_SHOWN = 10


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
        if len(rel) != _BAM_KEY_PARTS or not is_rep_dir(rel[2]):
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


def _check_reps(reps: dict[tuple[str, str], int], *, fg_labs_sha: str, min_reps: int) -> None:
    """Refuse to promote a run measured with fewer than ``min_reps`` reps.

    Checks the run's MAXIMUM rep count, not every cell's: some cells are
    single-rep by design (the ALT arms, `_alt_targets`), but a run submitted at
    ``reps=N`` has at least one cell with N reps. A maximum below ``min_reps``
    therefore means the whole sweep ran short -- which is how v0.13.0 was
    blessed at one rep per cell.
    """
    most = max(reps.values(), default=0)
    if most < min_reps:
        raise ValueError(
            f"refusing to bless {fg_labs_sha} as golden: the run has at most {most} rep(s) "
            f"per cell, below the {min_reps} a release is measured at (reps_release in "
            f"config/defaults.yaml). Re-run the sweep at --reps {min_reps}, or pass "
            f"--min-reps {most} to bless it anyway."
        )


def _dest_cells(
    bucket: str, fg_labs_sha: str, copies: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    """The ``(sample, arch)`` cells named by the golden destinations of ``copies``."""
    golden_prefix = f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/"
    cells: set[tuple[str, str]] = set()
    for _, dest in copies:
        # Every dest is built as <golden_prefix><sample>/<arch>/aligned.bam.
        sample, arch, _bam = dest[len(golden_prefix) :].split("/")
        cells.add((sample, arch))
    return cells


def _format_cells(cells: list[tuple[str, str]]) -> str:
    """Name up to ``_MAX_MISSING_SHOWN`` cells, summarizing the rest."""
    shown = ", ".join(f"{s}/{a}" for s, a in cells[:_MAX_MISSING_SHOWN])
    extra = len(cells) - _MAX_MISSING_SHOWN
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def _check_rep1_present(
    reps: dict[tuple[str, str], int],
    copies: list[tuple[str, str]],
    *,
    bucket: str,
    fg_labs_sha: str,
) -> None:
    """Refuse a run with a cell whose replicates lack ``rep-1``.

    Only ``rep-1`` is blessed, so such a cell would be silently left out of the
    golden -- and neither the rep-count guard (a run-wide maximum) nor
    :func:`_verify_copied` (which checks only what was copied) would notice.
    """
    missing = sorted(set(reps) - _dest_cells(bucket, fg_labs_sha, copies))
    if missing:
        raise ValueError(
            f"refusing to bless {fg_labs_sha} as golden: {len(missing)} run cell(s) have "
            f"no rep-1 aligned.bam: {_format_cells(missing)}. Only rep-1 is blessed, so "
            f"these cells would be missing from the golden."
        )


def _verify_copied(bucket: str, fg_labs_sha: str, copies: list[tuple[str, str]]) -> None:
    """Fail unless every copied cell is now present under the golden prefix.

    The copy is one `aws s3 cp` per cell, so an interrupted bless leaves a
    partial golden that Gate #2 then silently scopes down to. Re-listing makes
    that loud; re-running `bless-golden` completes it.
    """
    golden_prefix = f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/"
    present = parse_golden_cells(list_recursive(golden_prefix), fg_labs_sha)
    missing = sorted(_dest_cells(bucket, fg_labs_sha, copies) - present)
    if missing:
        raise RuntimeError(
            f"golden for {fg_labs_sha} is incomplete after the copy: {len(missing)} cell(s) "
            f"missing: {_format_cells(missing)}. Re-run bless-golden to finish it."
        )


def _local_copies(
    *, bucket: str, fg_labs_sha: str, dry_run: bool
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], int]]:
    """Walk a local ``runs/<sha>/`` tree for the ``rep-1`` BAMs to bless.

    Returns ``(copies, reps)``: the ``(src, golden-dest)`` pairs, and each
    ``(sample, arch)`` cell's rep count for the under-replication guard.
    """
    src_root = REPO_ROOT / "runs" / fg_labs_sha
    if not src_root.is_dir():
        raise FileNotFoundError(f"no local run at {src_root}; collect first (or --from-s3)")
    copies: list[tuple[str, str]] = []
    reps: dict[tuple[str, str], int] = {}
    for sample_dir in sorted(d for d in src_root.iterdir() if d.is_dir()):
        for arch_dir in sorted(d for d in sample_dir.iterdir() if d.is_dir()):
            higher_reps = sorted(
                d
                for d in arch_dir.iterdir()
                if d.is_dir() and is_rep_dir(d.name) and d.name != "rep-1"
            )
            if higher_reps and not dry_run:
                print(
                    f"note: blessing rep-1 only for {sample_dir.name}/{arch_dir.name}; "
                    f"ignoring {len(higher_reps)} additional rep(s)",
                    file=sys.stderr,
                )
            rep_count = sum(
                1 for d in arch_dir.iterdir() if is_rep_dir(d.name) and (d / "aligned.bam").exists()
            )
            # A cell with no replicate BAMs is not part of the run (matches the S3
            # listing, which only ever sees cells that have an aligned.bam).
            if rep_count:
                reps[(sample_dir.name, arch_dir.name)] = rep_count
            bam = arch_dir / "rep-1" / "aligned.bam"
            if not bam.exists():
                continue
            dest = (
                f"s3://{bucket}/golden/fg-labs-{fg_labs_sha}/"
                f"{sample_dir.name}/{arch_dir.name}/aligned.bam"
            )
            copies.append((str(bam), dest))
    return copies, reps


def bless_golden(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    bucket: str = _DEFAULT_BUCKET,
    force: bool = False,
    from_s3: bool = False,
    allowances_path: Path = DEFAULT_ALLOWANCES_PATH,
    min_reps: int = 0,
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

    Refuses a run with fewer reps than ``reps_release`` (``min_reps`` overrides,
    e.g. ``--min-reps 1`` for a deliberately single-rep bless) or with a cell that
    has replicates but no ``rep-1``, and after copying re-lists the golden and
    fails if any cell is missing.
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
        reps = parse_run_reps(proc.stdout, fg_labs_sha)
    else:
        copies, reps = _local_copies(bucket=bucket, fg_labs_sha=fg_labs_sha, dry_run=dry_run)

    _check_rep1_present(reps, copies, bucket=bucket, fg_labs_sha=fg_labs_sha)
    # Before the empty-copy return, so a run with no replicate BAMs at all is
    # refused as under-replicated rather than reported as a successful no-op.
    _check_reps(
        reps,
        fg_labs_sha=fg_labs_sha,
        min_reps=min_reps or load_config(REPO_ROOT / "config").reps_release,
    )

    if not copies:
        print("no rep-1 aligned.bam files to bless", file=sys.stderr)
        return

    for src, dest in copies:
        run_cmd(["aws", "s3", "cp", src, dest], dry_run=dry_run)

    if not dry_run:
        _verify_copied(bucket, fg_labs_sha, copies)
