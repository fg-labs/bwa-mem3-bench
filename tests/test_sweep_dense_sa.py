"""Guards for the sweep + thread-scaling SA-densification wiring
(`sweep_dense_sa_shift`, fg-labs/bwa-mem3#510).

Text-based on the Snakefile sources, matching `tests/test_arena_smk.py` /
`tests/test_thread_packing.py`: the reference-selection logic lives in
`align.smk` at module scope and cannot be imported without Snakemake's injected
globals. The `snakemake --list` load guard in `test_arena_smk.py` proves it
parses; these tests pin that the dense path is actually WIRED to the fg-labs
alignments and NOT to the baseline (whose bwameth / upstream bwa-mem2 readers
cannot parse a densified on-disk SA).
"""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

ALIGN_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "align.smk"
SCALING_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "scaling.smk"
COMPARE_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "compare.smk"


def _rule_body(text: str, rule: str) -> str:
    start = text.index(f"rule {rule}:")
    nxt = text.find("\nrule ", start + 1)
    return text[start : nxt if nxt != -1 else len(text)]


def test_fg_labs_sweep_rule_uses_the_dense_reference_helper() -> None:
    """`align_fg_labs` must route its `ref` input through `_fg_labs_ref_inputs`
    (which swaps in `<ref>-u<shift>` when densifying), not the stock
    `_ref_inputs(..., meth_index="d3")` directly -- else the sweep silently
    aligns against the stock stride-8 index despite the shipped shift=2."""
    body = _rule_body(ALIGN_SMK.read_text(), "align_fg_labs")
    assert "ref = _fg_labs_ref_inputs" in body
    assert 'ref = lambda wc: _ref_inputs(wc, meth_index="d3")' not in body


def test_thread_scaling_rule_uses_the_dense_reference_helper() -> None:
    """Thread scaling is one of the "all others" fg-labs alignments and must
    densify the same way as the sweep."""
    body = _rule_body(SCALING_SMK.read_text(), "align_thread_scaling")
    assert "ref = _fg_labs_ref_inputs" in body


def test_every_fg_labs_align_rule_uses_the_dense_helper() -> None:
    """All THREE fg-labs alignment rules must route through
    `_fg_labs_ref_inputs`, or an "other" fg-labs align silently stays on the
    stock index despite the shipped shift. The thread-invariance gate in
    compare.smk is the easy one to miss (it uses `meth_index="none"`, and it is
    a correctness gate rather than a throughput benchmark) -- pin it too."""
    body = _rule_body(COMPARE_SMK.read_text(), "compat_thread_invariance")
    assert 'ref = lambda wc: _fg_labs_ref_inputs(wc, meth_index="none")' in body, (
        "compat-invariance rule must use _fg_labs_ref_inputs, not a bare "
        '_ref_inputs(wc, meth_index="none")'
    )
    assert 'ref = lambda wc: _ref_inputs(wc, meth_index="none")' not in body


def test_baseline_rule_keeps_the_stock_reference() -> None:
    """`align_baseline` (upstream bwa-mem2 / bwameth) must NOT use the dense
    helper: those readers cannot parse a densified on-disk SA, and its output is
    the concordance anchor. It stays on `_ref_inputs(..., meth_index="c2t")`."""
    body = _rule_body(ALIGN_SMK.read_text(), "align_baseline")
    assert "_fg_labs_ref_inputs" not in body
    assert 'meth_index="c2t"' in body


def test_dense_reference_helper_derives_the_suffixed_name() -> None:
    """`_sweep_dense_reference` returns `<ref>-u<shift>` when densifying and the
    bare stock reference when off (shift >= STOCK_SA_SHIFT)."""
    text = ALIGN_SMK.read_text()
    assert 'return stock if shift >= STOCK_SA_SHIFT else f"{stock}-u{shift}"' in text
