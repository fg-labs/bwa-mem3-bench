"""Guards tying the arena release ladder to the blessed-release ledger.

The arena benchmarks today's candidate against every prior BLESSED bwa-mem3
release. That ladder is expressed in THREE hand-maintained places, and a bless
that updates only some of them silently ships a stale arena:

1. ``docs/release-allowances.yaml`` -- the canonical ledger: one ``to_sha``
   entry per blessed release, appended oldest-first. Updated when a release is
   blessed. This is the source of truth.
2. ``docker/Dockerfile.base`` -- one ``RUN git clone ... arena-releases/<label>``
   block per release, which BUILDS that release's binary into the base image.
   Deliberately hand-authored one-block-per-release for Docker layer caching
   (``bwa_mem3_bench/base_image.py`` content-addresses the base tag over this
   file's bytes), so it cannot be auto-generated -- only guarded.
3. ``ARENA_RELEASES`` in ``workflow/rules/arena.smk`` -- the list of labels the
   workflow actually runs, and whose LAST entry is the fgumi-correctness arm
   (``ARENA_PRIOR_RELEASE_LABEL``).

Real bug these guards backfill: v0.10.0 was blessed (added to (1)) but never
added to (2) or (3), so the "fastest release yet" arena plot silently skipped
the immediately-preceding release and the correctness arm compared against
v0.9.0 instead of v0.10.0. These tests make that omission a hard ``pixi run
check`` failure on the NEXT bless, on every path -- including a manual bless
that bypasses any orchestration tool.

The first block pins the committed tree; the second feeds ``ladder_problems``
broken inputs so its detection logic is actually exercised (a stub returning
``[]`` must fail these, not pass the suite).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bwa_mem3_bench import arena_ladder
from bwa_mem3_bench.arena_ladder import (
    arena_releases,
    dockerfile_release_blocks,
    ladder_problems,
)
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    ReleaseAllowance,
    load_allowances,
)


def _allowance(to_sha: str, pr: str = "fg-labs/bwa-mem3#1") -> ReleaseAllowance:
    return ReleaseAllowance(
        to_sha=to_sha, pr=pr, date="2026-01-01", summary="x", expected_drift_pct=0.0, aliases=()
    )


# --------------------------------------------------------------------------- #
# Committed-tree guards                                                        #
# --------------------------------------------------------------------------- #


def test_arena_releases_matches_dockerfile_base_blocks() -> None:
    """`ARENA_RELEASES` and the base-image build blocks list the same releases."""
    smk = arena_releases()
    docker = dockerfile_release_blocks()
    assert smk == docker, (
        "ARENA_RELEASES (arena.smk) and the arena-release RUN blocks "
        f"(Dockerfile.base) disagree:\n  arena.smk : {smk}\n  Dockerfile: {docker}"
    )


def test_committed_tree_ladder_is_consistent() -> None:
    """The shared consistency check reports nothing on the healthy committed tree.

    `ladder_problems` is what both these guards and the `bless-release` preflight
    call; a clean result here is the invariant the preflight relies on.
    """
    assert ladder_problems() == []


# --------------------------------------------------------------------------- #
# Detection: ladder_problems must actually catch each inconsistency class      #
# --------------------------------------------------------------------------- #


def test_detects_a_blessed_release_missing_from_the_arena() -> None:
    """A ledger entry with no arena arm is reported -- the v0.10.0 regression."""
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    extra = _allowance("d" * 40, pr="fg-labs/bwa-mem3#999")
    problems = ladder_problems([*allowances, extra])
    assert any("is not in the arena ladder" in p for p in problems), problems


def test_detects_a_stale_prior_release_arm() -> None:
    """When the newest ledger entry is not the arena tail, it is reported."""
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    # Newest ledger entry is now a SHA the arena tail does not match.
    stale = [*allowances[:-1], _allowance("e" * 40, pr="fg-labs/bwa-mem3#998")]
    problems = ladder_problems(stale)
    assert any("prior-release arm" in p and "stale" in p for p in problems), problems
    # ...and the missing newest release is also flagged as absent from the arena.
    assert any("is not in the arena ladder" in p for p in problems), problems


def test_detects_an_arena_arm_that_is_not_a_blessed_release() -> None:
    """An arena arm with no ledger entry is reported (no un-vetted references)."""
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    tail_sha = arena_releases()[-1][1]
    # Drop the ledger entry the arena tail depends on -> tail is now unblessed.
    without_tail = [a for a in allowances if a.to_sha != tail_sha]
    problems = ladder_problems(without_tail)
    assert any("is not a blessed release" in p for p in problems), problems


def test_detects_a_duplicate_arena_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two arms sharing a label both install to bwa-mem3.<label> -- rejected."""
    dupe = [("v1", "a" * 40), ("v1", "b" * 40)]
    monkeypatch.setattr(arena_ladder, "arena_releases", lambda: dupe)
    monkeypatch.setattr(arena_ladder, "dockerfile_release_blocks", lambda: dupe)
    problems = ladder_problems(load_allowances(DEFAULT_ALLOWANCES_PATH))
    assert any("appears more than once (installs collide)" in p for p in problems), problems


def test_detects_a_duplicate_arena_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same SHA under two labels double-counts a release -- rejected."""
    dupe = [("v1", "a" * 40), ("v2", "a" * 40)]
    monkeypatch.setattr(arena_ladder, "arena_releases", lambda: dupe)
    monkeypatch.setattr(arena_ladder, "dockerfile_release_blocks", lambda: dupe)
    problems = ladder_problems(load_allowances(DEFAULT_ALLOWANCES_PATH))
    assert any(p.startswith("arena SHA") and "appears more than once" in p for p in problems), (
        problems
    )


def test_detects_arena_smk_vs_dockerfile_disagreement(monkeypatch: pytest.MonkeyPatch) -> None:
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    monkeypatch.setattr(arena_ladder, "arena_releases", lambda: [("v021", "89bd589" + "0" * 33)])
    monkeypatch.setattr(
        arena_ladder, "dockerfile_release_blocks", lambda: [("v021", "ffffffff" + "0" * 32)]
    )
    assert any("disagree" in p for p in ladder_problems(allowances))


# --------------------------------------------------------------------------- #
# Parser robustness                                                            #
# --------------------------------------------------------------------------- #


def test_arena_releases_ignores_commented_out_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A commented-out ARENA_RELEASES entry is not treated as live (ast, not regex)."""
    smk = tmp_path / "arena.smk"
    live, commented = "a" * 40, "b" * 40
    smk.write_text(f'ARENA_RELEASES = [\n    ("v1", "{live}"),\n    # ("v2", "{commented}"),\n]\n')
    monkeypatch.setattr(arena_ladder, "ARENA_SMK", smk)
    assert arena_releases() == [("v1", live)]


def test_dockerfile_parser_rejects_clone_install_label_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A block that clones <label> but installs bwa-mem3.<other> is rejected."""
    dockerfile = tmp_path / "Dockerfile.base"
    sha = "a" * 40
    dockerfile.write_text(
        f"RUN git clone --quiet https://x /build/arena-releases/v1 && \\\n"
        f"    git checkout --quiet {sha} && \\\n"
        f"    install -Dm755 bwa-mem3 /out/bin/bwa-mem3.vWRONG\n"
    )
    monkeypatch.setattr(arena_ladder, "DOCKERFILE_BASE", dockerfile)
    with pytest.raises(ValueError, match="install name is what arena.smk runs"):
        dockerfile_release_blocks()


def test_dockerfile_parser_skips_commented_out_run_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dockerfile = tmp_path / "Dockerfile.base"
    sha = "a" * 40
    dockerfile.write_text(
        f"# RUN git clone --quiet https://x /build/arena-releases/vOLD && checkout {sha}\n"
        f"RUN git clone --quiet https://x /build/arena-releases/v1 && \\\n"
        f"    git checkout --quiet {sha} && \\\n"
        f"    install -Dm755 bwa-mem3 /out/bin/bwa-mem3.v1\n"
    )
    monkeypatch.setattr(arena_ladder, "DOCKERFILE_BASE", dockerfile)
    assert dockerfile_release_blocks() == [("v1", sha)]


def test_all_arena_release_shas_are_full_length_in_the_committed_tree() -> None:
    """Sanity: the committed ladder carries full 40-hex SHAs (no abbreviations)."""
    for label, sha in arena_releases():
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{label} SHA is not full 40-hex: {sha}"
