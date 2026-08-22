"""Guards for the per-cell tachyon host-contention probe on `align_fg_labs`.

Extends the thread-scaling ladder's pre/post design (see
`workflow/rules/scaling.smk`) to the regular sweep, so a flagged wall-time
regression can be checked against host quality instead of reasoned about only
from the archs' documented noise-CV pattern (fg-labs/bwa-mem3-bench#56).

Read as text, not imported: `align.smk` is a Snakemake rule file, not a Python
module, the same approach `test_thread_packing.py` and `test_compat_arm.py`
use for their own rule-body assertions.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALIGN_SMK = (REPO_ROOT / "workflow" / "rules" / "align.smk").read_text()


def _rule_body(rule_name: str) -> str:
    """Source text of one rule, from its `rule <name>:` line to the next rule."""
    start = ALIGN_SMK.index(f"rule {rule_name}:")
    nxt = ALIGN_SMK.find("\nrule ", start + 1)
    return ALIGN_SMK[start:] if nxt == -1 else ALIGN_SMK[start:nxt]


def test_align_fg_labs_declares_a_host_probe_output() -> None:
    """Without a declared output, a probe write outside `{output.*}` earns no
    Snakemake dependency tracking and a partial/crashed cell could leave a
    stale reading behind that the next run silently treats as current."""
    body = _rule_body("align_fg_labs")
    assert 'host_probe = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/host-probe.jsonl"' in body


def test_align_fg_labs_uses_the_sweep_probe_budget() -> None:
    """Must use `CONFIG.sweep_host_probe_seconds`, not the ladder's
    `thread_scaling.host_probe_seconds` -- the ladder's 10 s-per-side default
    would be a 10x overhead on a cell as short as smoke-1M."""
    body = _rule_body("align_fg_labs")
    assert "probe_seconds = CONFIG.sweep_host_probe_seconds" in body
    assert "thread_scaling.host_probe_seconds" not in body


def test_pre_probe_runs_after_index_staging_and_before_the_timed_alignment() -> None:
    """The pre/post pair must bracket the SAME resident index, or a difference
    between them could be the index's own memory footprint rather than a
    change in host contention -- the same ordering `align_thread_scaling`
    uses, and the same reason its own rule docstring gives for it."""
    body = _rule_body("align_fg_labs")
    shm_end = body.index("fi\n", body.index("bwa-mem2.fg-labs shm"))
    pre_probe = body.index("emit-host-probe pre")
    timed_align = body.index("tricorder --out {output.timing}")
    assert shm_end < pre_probe < timed_align, (
        "pre probe must run after shm staging completes and before the timed alignment starts"
    )


def test_post_probe_runs_after_the_timed_alignment_while_the_index_is_still_staged() -> None:
    """The post reading must still see the same staged index -- the `trap`
    that unstages it only fires on shell EXIT, after the whole rule body, so
    the post probe running anywhere before that point is correctly ordered;
    what must NOT happen is the post probe moving before the timed alignment,
    which would collapse it into a second pre reading."""
    body = _rule_body("align_fg_labs")
    timed_align = body.index("tricorder --out {output.timing}")
    post_probe = body.index("emit-host-probe post")
    assert timed_align < post_probe


def test_pre_probe_truncates_and_post_probe_appends() -> None:
    """`>` then `>>`, exactly as `align_thread_scaling` does: a retried
    attempt (e.g. after a spot interruption left a partial file) must produce
    exactly one pre/post pair, not a stacked extra pre reading."""
    body = _rule_body("align_fg_labs")
    assert "emit-host-probe pre {params.probe_seconds} > {output.host_probe}" in body
    assert "emit-host-probe post {params.probe_seconds} >> {output.host_probe}" in body
