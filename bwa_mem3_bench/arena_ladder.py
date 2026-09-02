"""The arena release ladder and the invariants that keep it honest.

The arena benchmarks today's candidate against every prior BLESSED bwa-mem3
release. That ladder lives in three hand-maintained places that a bless must
keep in lockstep (see ``tests/test_arena_ladder.py`` for the full rationale):

1. ``docs/release-allowances.yaml`` -- the canonical ledger (one ``to_sha`` per
   blessed release, appended oldest-first). The source of truth.
2. ``docker/Dockerfile.base`` -- one ``RUN`` block per release that builds its
   binary into the base image as ``/out/bin/bwa-mem3.<label>``.
3. ``ARENA_RELEASES`` in ``workflow/rules/arena.smk`` -- the labels the workflow
   runs; the last entry is the fgumi-correctness arm.

This module parses (2) and (3) out of their source files (arena.smk is not
importable as a Python module) and exposes :func:`ladder_problems`, the single
consistency check both the guard tests and the ``bless-release`` preflight run.
Every entry in the arena ladder is itself a blessed release -- there are no
"reference-only" arms (the mid-stream ``394f8f8`` point is a blessed ``to_sha``
too), so the ladder's release set and the ledger's ``to_sha`` set are a
bijection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    ReleaseAllowance,
    load_allowances,
    sha_prefix_match,
)

ARENA_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "arena.smk"
DOCKERFILE_BASE = Path(REPO_ROOT) / "docker" / "Dockerfile.base"


def arena_releases() -> list[tuple[str, str]]:
    """The ``(label, sha)`` pairs from ``ARENA_RELEASES`` in ``arena.smk``.

    Parses the actual Python list literal with ``ast.literal_eval`` rather than
    a regex, so a commented-out entry is treated as commented-out (not live).
    """
    body = ARENA_SMK.read_text().split("ARENA_RELEASES = [", 1)[1].split("]", 1)[0]
    parsed = ast.literal_eval("[" + body + "]")
    return [(str(label), str(sha)) for label, sha in parsed]


def dockerfile_release_blocks() -> list[tuple[str, str]]:
    """The ``(label, checkout_sha)`` pairs from the arena-release RUN blocks.

    Each block is a ``RUN git clone ... /build/arena-releases/<label>`` that
    checks out ``<sha>`` and installs it as ``/out/bin/bwa-mem3.<label>``. The
    clone-dir label, the checkout SHA, and the *install* label are parsed
    independently and the two labels asserted equal -- the install name is what
    ``arena.smk`` actually invokes, so a clone/install label mismatch would
    build one binary and run under a different name.
    """
    text = DOCKERFILE_BASE.read_text()
    # Anchor to a line-start RUN so a commented-out (`# RUN ...`) block is skipped.
    starts = [
        m.start()
        for m in re.finditer(r"(?m)^RUN git clone --quiet \S+ /build/arena-releases/", text)
    ]
    pairs: list[tuple[str, str]] = []
    for i, start in enumerate(starts):
        block = text[start : starts[i + 1] if i + 1 < len(starts) else len(text)]
        clone = re.search(r"/build/arena-releases/(\S+?) &&", block)
        checkout = re.search(r"git checkout --quiet ([0-9a-fA-F]{7,40})", block)
        install = re.search(r"install -Dm755 bwa-mem3 /out/bin/bwa-mem3\.(\S+)", block)
        if clone is None or checkout is None or install is None:
            raise ValueError(f"malformed arena-release RUN block near offset {start}")
        if clone.group(1) != install.group(1):
            raise ValueError(
                f"arena-release block clones label {clone.group(1)!r} but installs "
                f"bwa-mem3.{install.group(1)} -- the install name is what arena.smk runs"
            )
        pairs.append((clone.group(1), checkout.group(1)))
    return pairs


def ladder_problems(allowances: list[ReleaseAllowance] | None = None) -> list[str]:
    """Human-readable descriptions of every arena-ladder inconsistency.

    An empty list means the ledger, the base-image build blocks, and
    ``ARENA_RELEASES`` are mutually consistent and the arena's prior-release
    arm is the most recently blessed release. Non-empty means a bless updated
    some but not all of the three places -- the v0.10.0 class of bug.
    """
    if allowances is None:
        allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    problems: list[str] = []

    smk = arena_releases()
    docker = dockerfile_release_blocks()
    if smk != docker:
        problems.append(
            "ARENA_RELEASES (arena.smk) and the arena-release RUN blocks "
            f"(Dockerfile.base) disagree: {smk} vs {docker}"
        )

    arena_labels = [label for label, _ in smk]
    arena_shas = [sha for _, sha in smk]
    ledger_shas = [a.to_sha for a in allowances]

    # No label or SHA may appear twice: two blocks with the same label both
    # install to `bwa-mem3.<label>` (the later overwrites the earlier), and a
    # duplicate SHA double-counts a release -- either silently mislabels a
    # measurement while every membership check still passes.
    for dup in sorted({x for x in arena_labels if arena_labels.count(x) > 1}):
        problems.append(f"arena label {dup!r} appears more than once (installs collide)")
    for dup in sorted({x for x in arena_shas if arena_shas.count(x) > 1}):
        problems.append(f"arena SHA {dup} appears more than once")

    # Every blessed release must be benched EXACTLY once (the v0.10.0 regression
    # is the zero-match case), and every arena arm must be a blessed release (no
    # un-vetted reference commits).
    for allowance in allowances:
        matches = sum(sha_prefix_match(allowance.to_sha, s) for s in arena_shas)
        if matches == 0:
            problems.append(
                f"blessed release {allowance.to_sha} ({allowance.pr}) is not in "
                "the arena ladder (add to arena.smk + Dockerfile.base, rebuild base)"
            )
        elif matches > 1:
            problems.append(
                f"blessed release {allowance.to_sha} ({allowance.pr}) matches "
                f"{matches} arena arms; it must map to exactly one"
            )
    for label, sha in smk:
        if not any(sha_prefix_match(sha, s) for s in ledger_shas):
            problems.append(
                f"arena arm {label} ({sha}) is not a blessed release in "
                "docs/release-allowances.yaml"
            )

    if not smk:
        problems.append("ARENA_RELEASES is empty")
    elif allowances:
        tail_label, tail_sha = smk[-1]
        newest = allowances[-1]
        if not sha_prefix_match(tail_sha, newest.to_sha):
            problems.append(
                f"arena prior-release arm ({tail_label}={tail_sha}) is not the "
                f"newest blessed golden ({newest.to_sha}, {newest.pr}) -- the "
                "arena ladder is stale for the next bless"
            )

    return problems
