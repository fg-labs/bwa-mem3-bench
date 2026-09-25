"""`bless-release` -- preflight + printed plan for a candidate-release bless.

A bless is a long, expensive, partly-irreversible sequence: build the base
image (only if the arena ladder changed), build + push the per-SHA image,
verify the push settled, submit the ``bless_release`` coordinator, watch it to
completion, collect, check the three gates, and -- only on a human's approval
-- promote the candidate to golden and write its release-allowances entry.

This command does NOT run that sequence. It runs the cheap, fail-fast
**preflight** -- the invariants that, when violated, waste a multi-hour run or
ship a stale comparison -- and then prints the ordered **plan** (the executable
face of ``docs/RELEASE.md``, which stays authoritative). Blessing stays a human
decision; the auto-run, resumable orchestrator is the documented next slice.
"""

from __future__ import annotations

import re
import shlex

from bwa_mem3_bench import aws_config
from bwa_mem3_bench.arena_ladder import ladder_problems
from bwa_mem3_bench.golden import missing_golden_cells
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    ReleaseAllowance,
    allowance_for,
    load_allowances,
    sha_prefix_match,
)

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# How many missing golden cells check #6 names before truncating.
_MAX_MISSING_SHOWN = 5


def _resolve(allowances: list[ReleaseAllowance], sha: str) -> tuple[ReleaseAllowance | None, str]:
    """Resolve ``sha`` to its allowance, turning an ambiguous prefix into a message.

    ``allowance_for`` raises ``ValueError`` on an ambiguous SHA prefix; a
    preflight must surface that as a clean FAIL line, never as a traceback that
    aborts before any output. Returns ``(allowance_or_None, error_or_empty)``.
    """
    if not sha:
        return None, ""
    try:
        return allowance_for(allowances, sha), ""
    except ValueError as exc:
        return None, str(exc)


def bless_release(
    *,
    fg_labs_sha: str,
    golden_ref_sha: str,
    strict: bool = False,
) -> None:
    """Preflight a candidate-release bless and print the ordered plan.

    Runs the invariant checks that a bless depends on, prints a PASS/FAIL line
    per check, then prints the ordered bless plan with the concrete commands to
    run. It launches nothing and changes nothing -- it is safe to run any time.

    :param fg_labs_sha: the candidate fg-labs/bwa-mem3 SHA being blessed. Full
        40-hex; it becomes the new golden if the bless succeeds.
    :param golden_ref_sha: the previous release's SHA -- the Gate #2 golden the
        candidate is compared against. Must be the most recently blessed release
        in ``docs/release-allowances.yaml`` and match the arena's prior-release
        arm.
    :param strict: exit non-zero if any check fails (for CI / an orchestrator).
        Default prints the failures but still emits the plan.
    :raises SystemExit: when ``strict`` and one or more checks fail.
    """
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    newest = allowances[-1] if allowances else None
    candidate_allowance, candidate_err = _resolve(allowances, fg_labs_sha)
    golden_allowance, golden_err = _resolve(allowances, golden_ref_sha)
    checks: list[tuple[bool, str]] = []

    # 1. The candidate SHA is well-formed.
    checks.append(
        (
            bool(_FULL_SHA.match(fg_labs_sha)),
            f"candidate SHA is a full 40-hex fg-labs SHA ({fg_labs_sha})",
        )
    )

    # 2. The candidate is not itself already blessed -- you bless an UNblessed
    #    candidate. Match against every allowance (and alias), not just the
    #    newest: an older blessed SHA would otherwise slip through and approve a
    #    costly run for historical code.
    candidate_label = "candidate is not an already-blessed release"
    if candidate_err:
        candidate_label += f" (ambiguous SHA: {candidate_err})"
    checks.append((candidate_allowance is None and not candidate_err, candidate_label))

    # 3. The golden resolves to a real allowance entry.
    golden_label = f"--golden-ref-sha resolves to a blessed release ({golden_ref_sha})"
    if golden_err:
        golden_label += f" (ambiguous SHA: {golden_err})"
    checks.append((golden_allowance is not None, golden_label))

    # 4. The golden IS the most recently blessed release -- comparing against an
    #    older golden silently understates or misattributes the candidate's delta.
    checks.append(
        (
            golden_allowance is not None
            and newest is not None
            and sha_prefix_match(golden_allowance.to_sha, newest.to_sha),
            "--golden-ref-sha is the MOST recent blessed release "
            f"({newest.to_sha if newest else '<none>'})",
        )
    )

    # 5. The arena ladder is consistent and its prior-release arm == the golden.
    problems = ladder_problems(allowances)
    checks.append((not problems, "arena ladder is consistent (ledger/Dockerfile/arena.smk)"))

    # 6. The golden is complete. Gate #2 compares only the samples the golden
    #    contains, so a partially copied golden silently drops samples from the
    #    comparison -- v0.13.0 was blessed with no vs-golden check on wgs-5M or
    #    wes-5M because the v0.12.0 golden copy had stopped partway.
    checks.append(_golden_complete_check(golden_allowance.to_sha if golden_allowance else ""))

    print(f"bless-release preflight: candidate {fg_labs_sha}\n")
    all_ok = True
    for ok, label in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok
    for problem in problems:
        print(f"         - {problem}")

    print("\nPlan (see docs/RELEASE.md for the authoritative runbook):")
    for i, step in enumerate(_plan(fg_labs_sha, golden_ref_sha), start=1):
        print(f"  {i}. {step}")

    if not all_ok:
        print("\nPreflight found failing checks -- resolve them before submitting.")
        if strict:
            raise SystemExit(1)
    else:
        print("\nPreflight passed.")


def _golden_complete_check(golden_sha: str) -> tuple[bool, str]:
    """Preflight check #6: every cell of the golden's own run is in the golden.

    A failed or unreadable S3 listing is a FAIL line, never a traceback -- the
    preflight's contract is one PASS/FAIL line per check.
    """
    label = "golden holds every cell its run produced (Gate #2 compares all samples)"
    if not golden_sha:
        return False, f"{label} (no resolvable golden)"
    try:
        missing = missing_golden_cells(aws_config.load().bucket, golden_sha)
    except (RuntimeError, OSError) as exc:  # OSError: no `aws` CLI on PATH
        return False, f"{label} (could not list S3: {exc})"
    if missing:
        shown = ", ".join(f"{s}/{a}" for s, a in missing[:_MAX_MISSING_SHOWN])
        return False, (
            f"{label} -- {len(missing)} cell(s) missing, e.g. {shown}; "
            f"re-run bless-golden --from-s3 --fg-labs-sha {golden_sha}"
        )
    return True, label


def _plan(fg_labs_sha: str, golden_ref_sha: str) -> list[str]:
    """The ordered bless steps, with concrete commands.

    Terse on purpose -- ``docs/RELEASE.md`` carries the rationale and gate
    definitions; this is the SHA-substituted command sequence. The drift
    declaration lands AFTER collect (it is what ``bench full-report`` and
    ``bless-golden`` consume), not before submit -- writing it early would make
    the candidate the newest ledger entry and break checks #2/#4 and the ladder
    guard until the step-12 arena bump.
    """
    cli = "pixi run python -m bwa_mem3_bench.cli"
    # The plan is command text a human may paste into a shell; shell-quote the
    # user-supplied SHAs so a malformed value cannot inject a second command. A
    # well-formed 40-hex SHA is returned unchanged.
    sha = shlex.quote(fg_labs_sha)
    golden = shlex.quote(golden_ref_sha)
    return [
        "One-time (shipped defaults enable this): ensure the densified re-sa index copies "
        "are uploaded -- hg38-u1 (arena, stride-2) and hg38-u2 + hg38-meth-u2 (sweep + "
        "thread-scaling, stride-4) -- or the >v0.12.0 fg-labs arms fail at input staging. "
        "Set *_dense_sa_shift: 3 to skip (and always for a pre-#510 SHA). See "
        "docs/data-setup.md.",
        f"If the arena ladder changed: {cli} build-base --image-name <ecr>-base --push",
        f"Build + push the per-SHA image: {cli} build --fg-labs-sha {sha} "
        "--image-name <ecr> --push",
        f"Verify the ECR push settled and match the digest buildx printed "
        f"(aws ecr describe-images ... imageTag={sha}).",
        f"Submit the release matrix: {cli} submit --fg-labs-sha {sha} "
        f"--target bless_release --golden-ref-sha {golden}  (auto-sets --reps to "
        "reps_release; a driver that bypasses `cli submit` must pass reps and archs itself)",
        f"Watch to completion: {cli} watch  (surface, do not ignore, spot-capacity stalls)",
        f"Collect + ingest: {cli} collect --fg-labs-sha {sha}",
        "Write the candidate's docs/release-allowances.yaml entry (to_sha, pr, "
        "expected_drift_pct, summary) -- the drift declaration the report + bless-golden need.",
        f"Check the gates: {cli} bench regression / full-report  "
        "(Gate #1 vs-upstream, #2 vs-golden, #3 thread-scaling; STOP on any failure).",
        "HUMAN APPROVAL: review the report; blessing is a decision, never automatic.",
        f"Promote: {cli} bless-golden --fg-labs-sha {sha}  "
        "(+ bless-baseline if the upstream tag moved).",
        f"Publish the results pages: {cli} bench results --fg-labs-sha {sha} "
        "--version <vX.Y.Z> --previous-version <prev vX.Y.Z>, and commit results/.",
        "Bump the arena ladder for the NEXT bless: add this release to arena.smk + "
        "Dockerfile.base (test_arena_ladder.py enforces this) and rebuild the base.",
    ]
