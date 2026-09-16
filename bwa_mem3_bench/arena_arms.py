"""Arm-ordering helper for the arena's `--fast`-vs-host-drift confound.

The arena (`workflow/rules/arena.smk`) runs ~13-14 labels x 2 modes on ONE host
per arch, so it can never randomize across hosts -- only across TIME within
that one job. Measured evidence (a c8a arena run, wgs-5M): `process_s` (the
aligner's own internal timer) is flat within noise between two adjacent
bwa-mem3 releases' `--fast` arms, while `wall_s` grows monotonically across
the job's reps for every `--fast` label (an "outside-process" gap of ~1.3s in
the first measured rep growing to ~13.5s in the third, on the SAME label). The
`--default` arms (43-100+ s) do not show this; only the short (17-40s)
`--fast` arms are a large enough fraction of the drift to move a median.

Because the arm order was FIXED and identical every rep (today's candidate,
`fg-labs-fast`, always last; the immediately preceding release always second
to last), whichever release is newest was always measured under the worst
accumulated host state -- a release-recency-vs-queue-position confound, not a
codegen regression. `front_load_fast_arms` addresses this two ways: grouping
every `--fast` arm into one block that runs before any `--default` arm
(shrinking the fragile metric's exposure to accumulated drift), and shuffling
each block's order with a seed tied to the run under measurement (so a given
release does not always land in the same, possibly disadvantaged, position
across different arena submissions).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

ArenaArm = tuple[str, str, str]


def front_load_fast_arms(arms: list[ArenaArm], *, seed: str) -> list[ArenaArm]:
    """Reorder `arms` ((label, binary, mode) triples) into two blocks.

    Every ``mode == "fast"`` arm comes first, followed by every
    ``mode != "fast"`` (i.e. ``"default"``) arm -- see the module docstring
    for why the short `--fast` arms need to run before drift has had a chance
    to accumulate over the job. Within each block, arms are shuffled using
    ``random.Random(seed)``, so:

    - the same ``seed`` always reproduces the same order (a re-run of the
      same arena target, e.g. via ``--forcerun``, shuffles identically), and
    - a different ``seed`` (in practice, a different ``fg_labs_sha`` under
      measurement) decorrelates "release recency" from "block position" --
      no label is pinned to the same relative position run after run.

    Does not change which arms are present, only their order -- the returned
    list is a permutation of `arms`, split into the two blocks above.

    :param arms: every (label, binary, mode) arm the caller's arch runs, in
        any order.
    :param seed: shuffle seed. `random.Random` accepts any hashable value
        (a `str` -- e.g. the fg-labs SHA under measurement -- included), so
        no numeric conversion is needed at the call site.
    :return: `arms`, reordered into [shuffled fast block, shuffled default
        block].
    """
    fast = [arm for arm in arms if arm[2] == "fast"]
    default = [arm for arm in arms if arm[2] != "fast"]
    rng = random.Random(seed)
    rng.shuffle(fast)
    rng.shuffle(default)
    return fast + default


@dataclass(frozen=True)
class DenseSaPolicy:
    """Which arena arms align against the stride-2 ``re-sa`` index.

    Extracted from ``arena.smk`` so the "newer than v0.12.0" gate is
    unit-testable without a Snakemake runtime. The gate is decided by POSITION
    in the oldest-first ``release_labels`` list rather than by parsing the
    ambiguous ``vNNN`` label (``v120`` = v0.12.0 but ``v100`` = v0.10.0, not
    v1.0.0).

    :param release_labels: every historical release label, oldest first.
    :param min_label: the exclusive threshold label; arms after it are dense.
    :param dense_shift: the configured SA-sample-rate shift (1 = stride-2).
    :param stock_shift: the stock/off shift (3 = stride-8); when
        ``dense_shift >= stock_shift`` the feature is off and no arm is dense.
    """

    release_labels: tuple[str, ...]
    min_label: str
    dense_shift: int
    stock_shift: int

    def uses_dense_sa(self, label: str, binary: str) -> bool:
        """Whether the ``(label, binary)`` arm uses the densified index.

        - The candidate (``bwa-mem2.fg-labs``) is always dense: it is the SHA
          under measurement, newer than every blessed release.
        - A historical ``bwa-mem3.<release>`` arm (default or ``<release>-fast``)
          is dense iff its release comes strictly after ``min_label``.
        - lh3/bwa, minibwa, and upstream bwa-mem2 are never dense.
        - When the feature is off, NO arm is dense.
        """
        if self.dense_shift >= self.stock_shift:
            return False
        if binary == "bwa-mem2.fg-labs":
            return True
        if not binary.startswith("bwa-mem3."):
            return False
        release = label[: -len("-fast")] if label.endswith("-fast") else label
        if release not in self.release_labels or self.min_label not in self.release_labels:
            return False
        return self.release_labels.index(release) > self.release_labels.index(self.min_label)
