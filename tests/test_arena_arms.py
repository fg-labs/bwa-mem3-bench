"""Unit tests for `bwa_mem3_bench.arena_arms.front_load_fast_arms`."""

from __future__ import annotations

from bwa_mem3_bench.arena_arms import front_load_fast_arms

_ARMS = [
    ("bwa", "bwa", "default"),
    ("minibwa", "minibwa", "default"),
    ("bwa-mem2-upstream", "bwa-mem2.upstream", "default"),
    ("v021", "bwa-mem3.v021", "default"),
    ("v021-fast", "bwa-mem3.v021", "fast"),
    ("v090", "bwa-mem3.v090", "default"),
    ("v090-fast", "bwa-mem3.v090", "fast"),
    ("fg-labs-default", "bwa-mem2.fg-labs", "default"),
    ("fg-labs-fast", "bwa-mem2.fg-labs", "fast"),
]

_N_FAST = 3  # v021-fast, v090-fast, fg-labs-fast
_N_DEFAULT = 6


def test_every_fast_arm_precedes_every_default_arm() -> None:
    result = front_load_fast_arms(_ARMS, seed="abc123")
    modes = [mode for _, _, mode in result]
    first_default_idx = modes.index("default")
    assert all(mode == "fast" for mode in modes[:first_default_idx]), (
        "expected every 'fast' arm to sort before the first 'default' arm"
    )
    assert all(mode == "default" for mode in modes[first_default_idx:]), (
        "expected every arm from the first 'default' onward to also be 'default' "
        "-- a 'fast' arm leaked into the back half"
    )
    assert modes.count("fast") == _N_FAST
    assert modes.count("default") == _N_DEFAULT


def test_reordering_is_a_permutation_not_a_mutation() -> None:
    """No arm is added, dropped, or altered -- only reordered."""
    result = front_load_fast_arms(_ARMS, seed="some-sha")
    assert sorted(result) == sorted(_ARMS)
    assert len(result) == len(_ARMS)


def test_same_seed_is_deterministic() -> None:
    first = front_load_fast_arms(_ARMS, seed="371a1819802c2962b768c3b165f0d1319a6a75b3")
    second = front_load_fast_arms(_ARMS, seed="371a1819802c2962b768c3b165f0d1319a6a75b3")
    assert first == second


def test_different_seeds_produce_different_orders() -> None:
    """Not a strict guarantee for every possible pair of seeds, but this
    module's whole purpose is decorrelating release identity from block
    position across different arena submissions -- two long, distinct SHAs
    producing the identical shuffle would silently defeat that."""
    first = front_load_fast_arms(_ARMS, seed="371a1819802c2962b768c3b165f0d1319a6a75b3")
    second = front_load_fast_arms(_ARMS, seed="bb0e7cf7021c838dcfe8fb262f06dea47300867f")
    assert first != second


def test_a_release_is_not_pinned_to_the_same_position_across_seeds() -> None:
    """The bug this module fixes: a FIXED arm order means whichever label is
    last always eats the worst accumulated host drift. Confirm the fast
    block's last slot actually varies across seeds -- if it never did, the
    shuffle would be cosmetic and the confound this module exists to break
    would still be live."""
    last_fast_labels = set()
    for seed in ("seed-0", "seed-1", "seed-2", "seed-3", "seed-4", "seed-5"):
        result = front_load_fast_arms(_ARMS, seed=seed)
        fast_labels = [label for label, _, mode in result if mode == "fast"]
        last_fast_labels.add(fast_labels[-1])
    assert len(last_fast_labels) > 1, (
        f"expected the last fast-block slot to vary across seeds; always got "
        f"{last_fast_labels!r} -- the shuffle is not actually decorrelating position"
    )
