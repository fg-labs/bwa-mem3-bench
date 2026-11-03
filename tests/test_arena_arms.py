"""Unit tests for `bwa_mem3_bench.arena_arms`."""

from __future__ import annotations

import pytest

from bwa_mem3_bench.arena_arms import (
    FAMILY_BWA,
    FAMILY_BWA_MEM2_DENSE,
    FAMILY_BWA_MEM2_STOCK,
    FAMILY_MINIBWA,
    WRITER_BAM,
    WRITER_SAM,
    DenseSaPolicy,
    ScheduledRun,
    build_schedule,
    check_candidate_not_on_ladder,
    output_writer,
)
from bwa_mem3_bench.release_allowances import ReleaseAllowance

# Oldest-first release labels, mirroring `arena.smk`'s ARENA_RELEASES. `v120`
# (v0.12.0) is the dense-SA threshold; a hypothetical future `v130` is included
# to prove a release NEWER than the threshold flips to dense automatically.
_RELEASES = ("v021", "v090", "v100", "v110", "v120", "v130")
_MIN_LABEL = "v120"
_STOCK_SHIFT = 3


def _dense(label: str, binary: str, shift: int = 1) -> bool:
    return DenseSaPolicy(
        release_labels=_RELEASES,
        min_label=_MIN_LABEL,
        dense_shift=shift,
        stock_shift=_STOCK_SHIFT,
    ).uses_dense_sa(label, binary)


def test_candidate_is_always_dense() -> None:
    assert _dense("fg-labs-default", "bwa-mem2.fg-labs") is True
    assert _dense("fg-labs-fast", "bwa-mem2.fg-labs") is True


def test_bwa_minibwa_upstream_are_never_dense() -> None:
    assert _dense("bwa", "bwa") is False
    assert _dense("minibwa", "minibwa") is False
    assert _dense("bwa-mem2-upstream", "bwa-mem2.upstream") is False


def test_release_arm_is_dense_iff_newer_than_the_threshold() -> None:
    # At or below v0.12.0: stock.
    assert _dense("v120", "bwa-mem3.v120") is False
    assert _dense("v120-fast", "bwa-mem3.v120") is False
    assert _dense("v090", "bwa-mem3.v090") is False
    # Strictly newer: dense (proves list position, not label parsing, drives it).
    assert _dense("v130", "bwa-mem3.v130") is True
    assert _dense("v130-fast", "bwa-mem3.v130") is True


def test_feature_off_makes_every_arm_stock() -> None:
    """dense_shift == stock_shift disables the feature for all arms, including
    the candidate."""
    assert _dense("fg-labs-default", "bwa-mem2.fg-labs", shift=_STOCK_SHIFT) is False
    assert _dense("v130", "bwa-mem3.v130", shift=_STOCK_SHIFT) is False


def test_unknown_release_label_defaults_to_stock() -> None:
    """A bwa-mem3 arm whose label is absent from the release list (e.g. the raw
    `394f8f8` SHA label, or a typo) is treated as stock rather than raising."""
    assert _dense("394f8f8", "bwa-mem3.394f8f8") is False


_ARMS = [
    ("bwa", "bwa", "default"),
    ("minibwa", "minibwa", "default"),
    ("bwa-mem2-upstream", "bwa-mem2.upstream", "default"),
    ("v021", "bwa-mem3.v021", "default"),
    ("v021-fast", "bwa-mem3.v021", "fast"),
    ("v130", "bwa-mem3.v130", "default"),
    ("v130-fast", "bwa-mem3.v130", "fast"),
    ("v151", "bwa-mem3.v151", "default"),
    ("v151-fast", "bwa-mem3.v151", "fast"),
    ("fg-labs-default", "bwa-mem2.fg-labs", "default"),
    ("fg-labs-fast", "bwa-mem2.fg-labs", "fast"),
]
_DENSE = frozenset({"v130", "v130-fast", "v151", "v151-fast", "fg-labs-default", "fg-labs-fast"})
_PAIRS = (("fg-labs-default", "v151"), ("fg-labs-fast", "v151-fast"))
_HEADLINE = 5
_CONTEXT = 2
_SEED = "0a65bec1f47f8b773cf40b85d16833de010c8107"


def _schedule(seed: str = _SEED, **overrides: object) -> list[ScheduledRun]:
    kwargs: dict[str, object] = {
        "dense_labels": _DENSE,
        "pairs": _PAIRS,
        "candidate_label": "fg-labs-default",
        "seed": seed,
        "headline_rounds": _HEADLINE,
        "context_rounds": _CONTEXT,
        **overrides,
    }
    return build_schedule(_ARMS, **kwargs)  # type: ignore[arg-type]


def _rounds(schedule: list[ScheduledRun], block: str) -> dict[int, list[ScheduledRun]]:
    rounds: dict[int, list[ScheduledRun]] = {}
    for run in schedule:
        if run.block == block:
            rounds.setdefault(run.round, []).append(run)
    return rounds


def test_blocks_partition_arms_by_index_family() -> None:
    blocks = {run.label: run.block for run in _schedule()}
    assert blocks["bwa"] == FAMILY_BWA
    assert blocks["minibwa"] == FAMILY_MINIBWA
    assert blocks["bwa-mem2-upstream"] == FAMILY_BWA_MEM2_STOCK
    assert blocks["v021"] == blocks["v021-fast"] == FAMILY_BWA_MEM2_STOCK
    assert {blocks[label] for label in _DENSE} == {FAMILY_BWA_MEM2_DENSE}


def test_each_block_runs_contiguously() -> None:
    """Interleaving across index families would evict each run's page cache, so a
    block must never be split by another block's runs."""
    blocks_in_order = [run.block for run in _schedule()]
    collapsed = [b for i, b in enumerate(blocks_in_order) if i == 0 or blocks_in_order[i - 1] != b]
    assert len(collapsed) == len(set(collapsed))


def test_every_round_runs_every_arm_of_its_block_exactly_once() -> None:
    schedule = _schedule()
    for block in {run.block for run in schedule}:
        block_labels = sorted({run.label for run in schedule if run.block == block})
        for runs in _rounds(schedule, block).values():
            assert sorted(run.label for run in runs) == block_labels
            assert [run.position for run in runs] == list(range(1, len(runs) + 1))


def test_round_counts_warmup_plus_headline_or_context() -> None:
    schedule = _schedule()
    assert sorted(_rounds(schedule, FAMILY_BWA_MEM2_DENSE)) == list(range(_HEADLINE + 1))
    for block in (FAMILY_BWA, FAMILY_MINIBWA, FAMILY_BWA_MEM2_STOCK):
        assert sorted(_rounds(schedule, block)) == list(range(_CONTEXT + 1))


def test_rounds_are_contiguous_within_a_block() -> None:
    """Round r+1 starts only after every run of round r: the paired ratio is per
    round, so rounds must not interleave."""
    for block in {run.block for run in _schedule()}:
        rounds = [run.round for run in _schedule() if run.block == block]
        assert rounds == sorted(rounds)


def test_candidate_and_prior_are_adjacent_in_every_round() -> None:
    for first, second in _PAIRS:
        for runs in _rounds(_schedule(), FAMILY_BWA_MEM2_DENSE).values():
            positions = {run.label: run.position for run in runs}
            assert abs(positions[first] - positions[second]) == 1


def test_pair_order_alternates_every_round() -> None:
    """Whichever of the pair runs second inherits the first's after-effects; the
    lead alternates so neither member always pays it."""
    for first, second in _PAIRS:
        leads = []
        for _round, runs in sorted(_rounds(_schedule(), FAMILY_BWA_MEM2_DENSE).items()):
            positions = {run.label: run.position for run in runs}
            leads.append(positions[first] < positions[second])
        assert all(leads[i] != leads[i + 1] for i in range(len(leads) - 1)), leads


def test_a_pair_member_missing_from_the_block_runs_unpaired() -> None:
    arms_without_prior = [arm for arm in _ARMS if arm[0] != "v151"]
    schedule = build_schedule(
        arms_without_prior,
        dense_labels=_DENSE,
        pairs=_PAIRS,
        candidate_label="fg-labs-default",
        seed=_SEED,
        headline_rounds=_HEADLINE,
        context_rounds=_CONTEXT,
    )
    assert sum(run.label == "fg-labs-default" for run in schedule) == _HEADLINE + 1


def test_a_pair_split_across_blocks_runs_unpaired_in_each() -> None:
    """A stock-index prior and a dense candidate cannot share a round: each runs
    in its own block, so neither may be forced adjacent to anything."""
    dense_without_prior = _DENSE - {"v151", "v151-fast"}
    schedule = _schedule(dense_labels=dense_without_prior)
    blocks = {run.label: run.block for run in schedule}
    assert blocks["v151"] == FAMILY_BWA_MEM2_STOCK
    assert blocks["fg-labs-default"] == FAMILY_BWA_MEM2_DENSE
    assert sum(run.label == "v151" for run in schedule) == _CONTEXT + 1


def test_block_order_depends_on_the_seed() -> None:
    orders = {
        tuple(dict.fromkeys(run.block for run in _schedule(seed=f"seed-{i}"))) for i in range(8)
    }
    assert len(orders) > 1


def test_same_seed_is_deterministic() -> None:
    assert _schedule() == _schedule()


def test_different_seeds_produce_different_orders() -> None:
    other = _schedule(seed="bb0e7cf7021c838dcfe8fb262f06dea47300867f")
    assert [run.label for run in _schedule()] != [run.label for run in other]


def test_no_arm_is_pinned_to_one_position_across_rounds() -> None:
    """A per-round permutation, not one shuffled order repeated."""
    for label in ("v130", "v130-fast"):
        positions = {run.position for run in _schedule() if run.label == label and run.round >= 1}
        assert len(positions) > 1, (label, positions)


def test_writer_is_native_bam_for_every_bwa_mem3_build_only() -> None:
    writers = {run.label: run.writer for run in _schedule()}
    for label in ("v021", "v021-fast", "v151", "fg-labs-default", "fg-labs-fast"):
        assert writers[label] == WRITER_BAM, label
    for label in ("bwa", "minibwa", "bwa-mem2-upstream"):
        assert writers[label] == WRITER_SAM, label


def test_sam_control_arms_use_the_sam_writer() -> None:
    assert output_writer("fg-labs-default-sam", "bwa-mem2.fg-labs") == WRITER_SAM
    assert output_writer("v151-sam", "bwa-mem3.v151") == WRITER_SAM


def test_sam_control_arm_density_follows_its_release() -> None:
    policy = DenseSaPolicy(
        release_labels=("v120", "v151"), min_label="v120", dense_shift=1, stock_shift=3
    )
    assert policy.uses_dense_sa("v151-sam", "bwa-mem3.v151") is True


def test_token_round_trips_every_field() -> None:
    run = _schedule()[0]
    fields = run.to_token().split("|")
    assert fields == [
        run.block,
        str(run.round),
        str(run.position),
        run.label,
        run.binary,
        run.mode,
        str(int(run.dense)),
        run.writer,
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"headline_rounds": 0},
        {"context_rounds": 0},
        {"pairs": (("fg-labs-default", "v151"), ("v151", "v130"))},
    ],
)
def test_rejects_invalid_arguments(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _schedule(**overrides)


def test_rejects_duplicate_labels() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_schedule(
            [*_ARMS, _ARMS[0]],
            dense_labels=_DENSE,
            pairs=_PAIRS,
            candidate_label="fg-labs-default",
            seed=_SEED,
            headline_rounds=_HEADLINE,
            context_rounds=_CONTEXT,
        )


_LADDER = [
    ("v151", "a6b2ba1eb5cda2efbd45d22e2010b8afc87f928e"),
    ("v160", "0a65bec1f47f8b773cf40b85d16833de010c8107"),
]


def test_a_new_candidate_passes_the_ladder_check() -> None:
    check_candidate_not_on_ladder("1" * 40, _LADDER)


@pytest.mark.parametrize("sha", [_LADDER[-1][1], _LADDER[0][1], "0a65bec"])
def test_a_candidate_already_on_the_ladder_is_rejected(sha: str) -> None:
    """Re-running a listed release would pair `fg-labs-default` with its own build.

    Its `vs_prior_release` ratio and fgumi spot-check would then compare the
    candidate with itself; an abbreviated SHA must be caught too.
    """
    with pytest.raises(ValueError, match=r"already on the arena ladder"):
        check_candidate_not_on_ladder(sha, _LADDER)


def test_a_ledger_alias_of_a_ladder_release_is_rejected() -> None:
    """A release's tag commit is the same code under another SHA, so it self-pairs too."""
    tag = "fa688f0bc019968844073ae7d3fe124cf66d0011"
    ledger = [
        ReleaseAllowance(
            to_sha=_LADDER[-1][1],
            pr="fg-labs/bwa-mem3-bench#135",
            date="2026-11-02",
            summary="v0.16.0",
            expected_drift_pct=0.0,
            aliases=(tag,),
        )
    ]
    check_candidate_not_on_ladder(tag, _LADDER)  # without the ledger it is unknown
    with pytest.raises(ValueError, match=r"already on the arena ladder as 'v160'"):
        check_candidate_not_on_ladder(tag, _LADDER, ledger)


def test_an_alias_of_an_earlier_ladder_release_passes() -> None:
    """Only the last ladder entry is the prior-release pairing.

    An alias of an earlier release, such as a measured alias that carries a real
    code change (``e722ed06`` of ``394f8f8``), never pairs the candidate with its
    own build, so the guard leaves it alone.
    """
    alias = "e722ed06dd71d0186c401ce2f0e163d32b0a7ad7"
    ledger = [
        ReleaseAllowance(
            to_sha=_LADDER[0][1],
            pr="fg-labs/bwa-mem3-bench#135",
            date="2026-11-02",
            summary="v0.15.1",
            expected_drift_pct=0.0,
            aliases=(alias,),
        )
    ]
    check_candidate_not_on_ladder(alias, _LADDER, ledger)
