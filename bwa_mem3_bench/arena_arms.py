"""Run scheduling for the arena (`workflow/rules/arena.smk`).

The arena runs every arm on ONE host per arch, so it cannot randomize across
hosts -- only across TIME within that one job. The estimator it feeds
(`vs_prior_release`, a candidate-vs-previous-release ratio) is only sound if
whatever slowly-varying host state exists at a given moment hits the candidate
and its comparator EQUALLY, so the ratio cancels it.

Two earlier schedules broke that pairing:

- A fixed rep-outer / arm-inner order always measured the newest release last,
  under the worst accumulated drift (a false v0.9.0 -> v0.10.0 `--fast`
  regression).
- Its replacement ran every rep of one label back-to-back and only shuffled the
  label order. All of a label's reps then shared one moment of host state, and
  the median of 3 could not average it out. Measured in the 0a65bec mock bless:
  on m8a the candidate's three reps all ran with 1.4-1.9 GiB of its heap on
  4 KB pages (BSW ~17% slower), while the SAME binary's pin-probe run earlier in
  the job measured PROCESS() 16.80 s vs 18.27-18.76 s -- a 1.02x headline that
  a position effect, not codegen, produced.

`build_schedule` therefore emits ROUNDS: within a block every arm runs once per
round in a seeded random order, the candidate and its comparator always run
back-to-back (alternating which goes first), and the round's ratio is the
paired measurement. Arms are grouped into blocks by INDEX FAMILY, because only
arms reading the same index files can alternate without each run evicting the
next one's page cache (round-robin across families would make every run a cold
"rep 1"). Each block opens with one discarded warmup round.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

ArenaArm = tuple[str, str, str]
"""An arena arm as ``(label, binary, mode)``."""

# Index families. Arms in one family read the same on-disk index, so they can
# share a warm page cache; see the module docstring.
FAMILY_BWA = "bwa"
FAMILY_MINIBWA = "minibwa"
FAMILY_BWA_MEM2_STOCK = "bwa-mem2-stock"
FAMILY_BWA_MEM2_DENSE = "bwa-mem2-dense"

# Output writers recorded per run. `bam` is a bwa-mem3 binary's native
# uncompressed BAM (`--bam=0 -o -`); `sam` is SAM text piped through
# `samtools view -u` -- the only route to BAM for bwa, upstream bwa-mem2 and
# minibwa, and the opt-in control arm (`SAM_CONTROL_SUFFIX`) for bwa-mem3.
WRITER_BAM = "bam"
WRITER_SAM = "sam"

SAM_CONTROL_SUFFIX = "-sam"
"""Label suffix of an opt-in control arm that runs a bwa-mem3 binary through the
SAM-text writer, to measure what native BAM output saves."""


def index_family(binary: str, *, dense: bool) -> str:
    """The index family an arm reads, which decides its schedule block.

    :param binary: the arm's executable name (as in `ArenaArm`).
    :param dense: whether the arm reads the densified (`re-sa`) index copy.
    :return: one of the ``FAMILY_*`` constants.
    """
    if binary == "bwa":
        return FAMILY_BWA
    if binary == "minibwa":
        return FAMILY_MINIBWA
    return FAMILY_BWA_MEM2_DENSE if dense else FAMILY_BWA_MEM2_STOCK


def output_writer(label: str, binary: str) -> str:
    """Which output path an arm's run uses (``WRITER_BAM`` or ``WRITER_SAM``).

    Every bwa-mem3 build -- the candidate (``bwa-mem2.fg-labs``) and every
    historical ``bwa-mem3.<release>`` -- writes uncompressed BAM natively, which
    is how it is actually used and how the sweep and thread-scaling ladder time
    it. ``--bam`` predates the oldest tracked release (fg-labs/bwa-mem3#12,
    before v0.2.1). SAM-only tools, and a bwa-mem3 arm whose label carries
    ``SAM_CONTROL_SUFFIX``, go through ``samtools view -u``.
    """
    is_bwa_mem3 = binary == "bwa-mem2.fg-labs" or binary.startswith("bwa-mem3.")
    if is_bwa_mem3 and not label.endswith(SAM_CONTROL_SUFFIX):
        return WRITER_BAM
    return WRITER_SAM


@dataclass(frozen=True)
class ScheduledRun:
    """One timed invocation in the arena's run order.

    :param block: the index family (``FAMILY_*``) the run's block shares.
    :param round: 0 for the block's discarded warmup round, else 1..N.
    :param position: 1-based position within the round.
    :param label: the arm label (``v151``, ``fg-labs-fast``, ``bwa`` ...).
    :param binary: the executable.
    :param mode: ``default`` or ``fast``.
    :param dense: whether the run reads the densified index copy.
    :param writer: ``WRITER_BAM`` or ``WRITER_SAM``.
    """

    block: str
    round: int
    position: int
    label: str
    binary: str
    mode: str
    dense: bool
    writer: str

    def to_token(self) -> str:
        """Render as the ``|``-delimited token `align_arena`'s shell loop reads."""
        return "|".join(
            (
                self.block,
                str(self.round),
                str(self.position),
                self.label,
                self.binary,
                self.mode,
                str(int(self.dense)),
                self.writer,
            )
        )


def build_schedule(  # noqa: PLR0913 -- every knob is a distinct scheduling input
    arms: Sequence[ArenaArm],
    *,
    dense_labels: frozenset[str],
    pairs: Sequence[tuple[str, str]],
    candidate_label: str,
    seed: str,
    headline_rounds: int,
    context_rounds: int,
) -> list[ScheduledRun]:
    """Order every arm's runs into index-family blocks of paired rounds.

    - Arms are grouped into blocks by `index_family`. Block order is shuffled.
    - Each block runs one warmup round (round 0, discarded by the report) and
      then its measured rounds: ``headline_rounds`` for the block holding
      ``candidate_label``, ``context_rounds`` for every other block.
    - Every round is a fresh seeded permutation of the block's arms, except
      that each ``pairs`` entry (e.g. candidate and prior release) runs
      back-to-back, alternating which of the two goes first from one round to
      the next. A pair split across blocks, or with a member absent, is left
      unpaired.

    The same arguments always produce the same schedule (a ``--forcerun``
    re-run reproduces it); a different ``seed`` -- in practice the fg-labs SHA
    under measurement -- produces a different one.

    :param arms: every ``(label, binary, mode)`` arm to run.
    :param dense_labels: labels of the arms that read the densified index.
    :param pairs: label pairs that must run adjacently in every round.
    :param candidate_label: the arm whose block gets ``headline_rounds``.
    :param seed: shuffle seed.
    :param headline_rounds: measured rounds for the candidate's block (>= 1).
    :param context_rounds: measured rounds for every other block (>= 1).
    :return: every run, in execution order.
    :raises ValueError: on a duplicate label, a round count below 1, or a label
        that appears in two pairs.
    """
    labels = [label for label, _, _ in arms]
    if len(set(labels)) != len(labels):
        raise ValueError(f"arena arm labels must be unique; got {labels}")
    if headline_rounds < 1 or context_rounds < 1:
        raise ValueError(
            f"round counts must be >= 1; got headline={headline_rounds}, context={context_rounds}"
        )
    paired_labels = [label for pair in pairs for label in pair]
    if len(set(paired_labels)) != len(paired_labels):
        raise ValueError(f"an arena label may appear in at most one pair; got {list(pairs)}")

    rng = random.Random(seed)
    blocks: dict[str, list[ArenaArm]] = {}
    for arm in arms:
        label, binary, _ = arm
        family = index_family(binary, dense=label in dense_labels)
        blocks.setdefault(family, []).append(arm)
    block_order = sorted(blocks)  # deterministic input to the shuffle
    rng.shuffle(block_order)

    schedule: list[ScheduledRun] = []
    for family in block_order:
        block_arms = blocks[family]
        by_label = {arm[0]: arm for arm in block_arms}
        units: list[tuple[str, ...]] = []
        unit_of: set[str] = set()
        for first, second in pairs:
            if first in by_label and second in by_label:
                units.append((first, second))
                unit_of.update((first, second))
        units.extend((label,) for label in by_label if label not in unit_of)
        units.sort()  # deterministic input to the per-round shuffle

        has_candidate = candidate_label in by_label
        n_measured = headline_rounds if has_candidate else context_rounds
        # Which member of each pair leads in round 0; alternates every round.
        lead_flip = rng.random() < 0.5  # noqa: PLR2004 -- a fair coin
        for round_number in range(n_measured + 1):
            order = list(units)
            rng.shuffle(order)
            reverse_pairs = (round_number % 2 == 1) != lead_flip
            position = 0
            for unit in order:
                members = unit[::-1] if len(unit) == 2 and reverse_pairs else unit  # noqa: PLR2004
                for label in members:
                    position += 1
                    _, binary, mode = by_label[label]
                    schedule.append(
                        ScheduledRun(
                            block=family,
                            round=round_number,
                            position=position,
                            label=label,
                            binary=binary,
                            mode=mode,
                            dense=label in dense_labels,
                            writer=output_writer(label, binary),
                        )
                    )
    return schedule


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
        - A historical ``bwa-mem3.<release>`` arm (default, ``<release>-fast``
          or a ``<release>-sam`` writer control) is dense iff its release comes
          strictly after ``min_label``.
        - lh3/bwa, minibwa, and upstream bwa-mem2 are never dense.
        - When the feature is off, NO arm is dense.
        """
        if self.dense_shift >= self.stock_shift:
            return False
        if binary == "bwa-mem2.fg-labs":
            return True
        if not binary.startswith("bwa-mem3."):
            return False
        release = label.removesuffix(SAM_CONTROL_SUFFIX).removesuffix("-fast")
        if release not in self.release_labels or self.min_label not in self.release_labels:
            return False
        return self.release_labels.index(release) > self.release_labels.index(self.min_label)
