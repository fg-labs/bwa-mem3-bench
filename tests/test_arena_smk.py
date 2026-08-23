"""Guards for `workflow/rules/arena.smk`'s config-driven arch wiring.

Real gap caught by CodeRabbit: `ARENA_QUEUES` and the rule's
`wildcard_constraints` were originally hardcoded literals (`{"c7i": ...,
"c8g": ...}`, `"c7i|c8g"`) independent of `CONFIG.arena.archs`
(config/defaults.yaml). `_arena_from` (workflow_config.py) validates
`arena.archs` entries only against the FULL `config/archs.yaml` registry, not
against those literals, so a third configured arch would pass config
validation and then raise a bare `KeyError` (or silently mismatch the
wildcard) building `align_arena`'s resources -- a paid on-demand job failing
on a config change that looked valid.

Text-based on the Snakefile source, matching `tests/test_thread_packing.py`'s
convention for Snakefile-level logic snakemake's own machinery keeps out of
reach of a normal unit test.
"""

from __future__ import annotations

import re
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

ARENA_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "arena.smk"
RULES_DIR = Path(REPO_ROOT) / "workflow" / "rules"


def test_arena_queues_are_derived_from_config_not_a_hardcoded_dict() -> None:
    text = ARENA_SMK.read_text()
    assert "CONFIG.arena.archs" in text.split("ARENA_QUEUES", 1)[1].split("\n\n", 1)[0], (
        "ARENA_QUEUES must be built from CONFIG.arena.archs, not a fixed literal "
        'dict like {"c7i": ..., "c8g": ...} -- a third configured arch would '
        "pass _arena_from's own validation and then KeyError building this "
        "rule's resources.batch_queue"
    )
    assert '"c7i": "bwa-mem3-bench-c7i-arena"' not in text
    assert '"c8g": "bwa-mem3-bench-c8g-arena"' not in text


def test_arena_wildcard_constraint_is_derived_from_config_not_a_literal_regex() -> None:
    text = ARENA_SMK.read_text()
    assert 'arch = "c7i|c8g"' not in text, (
        "the arch wildcard_constraints must not hardcode the arch set literally; "
        "derive it from CONFIG.arena.archs (e.g. via a module-level "
        '"|".join(CONFIG.arena.archs)) so it cannot drift from ARENA_QUEUES'
    )
    assert "ARENA_ARCH_PATTERN" in text
    assert "arch = ARENA_ARCH_PATTERN" in text


def test_align_arena_is_scheduled_ahead_of_every_other_rule() -> None:
    """`align_arena` must outrank every other rule's `priority:` (default 0
    where unset), so `bless_release`'s scheduler prefers to submit it first
    when the profile's `jobs:` cap forces a choice among ready jobs. The
    arena is the newest, least battle-tested part of a bless -- surfacing its
    result (and cost) early is the whole point of setting this at all; a
    future rule matching or exceeding it would silently defeat that.
    """
    arena_text = ARENA_SMK.read_text()
    arena_start = arena_text.index("rule align_arena:")
    arena_end = arena_text.find("\nrule ", arena_start + 1)
    arena_body = arena_text[arena_start:] if arena_end == -1 else arena_text[arena_start:arena_end]
    match = re.search(r"^\s*priority:\s*(\d+)\s*$", arena_body, re.MULTILINE)
    assert match, "align_arena must declare a `priority:` directive"
    arena_priority = int(match.group(1))
    assert arena_priority > 0, "priority 0 is the default every other rule already has"

    for smk_path in sorted(RULES_DIR.glob("*.smk")):
        text = smk_path.read_text()
        for rule_match in re.finditer(r"^rule (\w+):", text, re.MULTILINE):
            rule_name = rule_match.group(1)
            if smk_path == ARENA_SMK and rule_name == "align_arena":
                continue
            start = rule_match.start()
            end = text.find("\nrule ", start + 1)
            body = text[start:] if end == -1 else text[start:end]
            other_match = re.search(r"^\s*priority:\s*(\d+)\s*$", body, re.MULTILINE)
            if other_match:
                other_priority = int(other_match.group(1))
                assert other_priority < arena_priority, (
                    f"{smk_path.name}::{rule_name} has priority {other_priority} >= "
                    f"align_arena's {arena_priority}; the scheduler would no longer "
                    "prefer the arena when both are ready"
                )
