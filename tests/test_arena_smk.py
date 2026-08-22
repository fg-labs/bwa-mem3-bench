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

from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

ARENA_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "arena.smk"


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
