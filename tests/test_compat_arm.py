"""Wiring guards for the `--compat=bwa-mem2` byte-identity arm.

The Python half of this feature (strict tag policy, sibling validation) is
covered in `test_workflow_config.py`. What is left is the workflow wiring, which
lives in Snakemake files that cannot be imported -- so these read the sources as
text, the same approach `test_thread_packing.py` uses for the `threads:`
directive. Each guard pins a decision whose breakage would be silent: the run
would still succeed and still report a number, just not the number claimed.
"""

import ast
import importlib
import re
from itertools import pairwise
from pathlib import Path

import pytest

from bwa_mem3_bench.workflow_config import Sample, _validate_compat_siblings, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
# Length of a full git object name; a shorter FGUMI_REF would be a branch or an
# abbreviation, either of which can move under us.
_FULL_SHA_LEN = 40
# Thread count at which the DEFAULT batch (chunk_size 10,000,000 x threads)
# first exceeds the 256 Mbase bound bwa-mem3 used to cap at: 10M * 26 = 260M.
_CAP_THRESHOLD_THREADS = 26
SNAKEFILE = (REPO_ROOT / "workflow" / "Snakefile").read_text()
COMPARE_SMK = (REPO_ROOT / "workflow" / "rules" / "compare.smk").read_text()


def test_compat_siblings_are_scored_against_the_base_samples_baseline() -> None:
    """`compare_vs_baseline` must resolve the baseline through `_baseline_sample`.

    Without the alias each compat sibling gets its own
    `baseline/.../<sample>-compat/` prefix, which realigns identical FASTQs under
    a second name -- 25 redundant 5M-read bwa-mem2 runs across the five real
    datasets, and a silent doubling of baseline storage.
    """
    assert "def _baseline_sample(" in COMPARE_SMK
    assert "{_baseline_sample(wc.sample)}" in COMPARE_SMK


def test_arm_queries_fall_back_to_the_x86_baseline() -> None:
    """Upstream bwa-mem2 has no ARM build, so `baseline/.../c8g/` can never exist.

    The compat arm is the only caller that requests `vs-baseline` on ARM, and it
    is the coverage the arm exists for: the known phantom-XS defect
    (fg-labs/bwa-mem3#290) is specific to the NEON and AVX-512BW kernels. Without
    this fallback those cells are unsatisfiable and the ARM arm silently
    disappears from the target set.
    """
    assert "ARM_X86_REFERENCE_ARCH" in COMPARE_SMK
    assert "wc.arch if wc.arch in BASELINE_ARCHS else ARM_X86_REFERENCE_ARCH" in COMPARE_SMK


def test_compat_arm_runs_on_every_arch() -> None:
    """Cross-arch coverage is the point; a per-sample arch list would drop ARM.

    `_archs_for_sample` returns x86-only for non-meth samples, which is correct
    for the sweep and wrong here.
    """
    body = SNAKEFILE.split("def _compat_targets(")[1].split("\ndef ")[0]
    assert "for arch in ARCHS" in body
    assert "_archs_for_sample" not in body


def test_bless_release_includes_the_compat_arm() -> None:
    """A release blessed without it re-inherits the weaker claim.

    `vs_baseline` alone scores 100% only because MQ/HN are excused; the compat
    arm is what upgrades that to byte-identity.
    """
    body = SNAKEFILE.split("rule bless_release:")[1].split("\nrule ")[0]
    assert "_compat_targets(" in body


def test_compat_samples_stay_out_of_the_regression_sweep_and_baseline_all() -> None:
    """`rule all` is the cross-SHA regression baseline and must stay byte-stable,
    and `baseline_all` must not mint a redundant per-sibling baseline."""
    sweep = SNAKEFILE.split("SWEEP_SAMPLES = [")[1].split("\n]")[0]
    assert "_is_compat_sample(s)" in sweep
    baseline_all = SNAKEFILE.split("rule baseline_all:")[1].split("\nrule ")[0]
    assert "_is_compat_sample(sample)" in baseline_all


def test_compat_arm_is_gated_by_fgumi_full_content_identity() -> None:
    """The compat claim rests on fgumi, not on compare-bams.

    `compare-bams` never reads QNAME, SEQ, QUAL, TLEN, RNEXT/PNEXT, twelve of the
    sixteen FLAG bits, or the header, so a compat arm scored only by it can report
    100% while SEQ or the @SQ dictionary differ. If this target disappears the run
    still passes and the byte-identity claim silently becomes unearned.
    """
    body = SNAKEFILE.split("def _compat_targets(")[1].split("\ndef ")[0]
    assert "compat-identity.txt" in body
    assert "vs-baseline.json" in body, "the graded JSON must stay for DB/trend reporting"
    assert "rule compare_compat_identity:" in COMPARE_SMK
    assert "fgumi compare bams" in COMPARE_SMK


def test_fgumi_identity_output_is_not_left_behind_on_a_difference() -> None:
    """A DIFFER exit must not satisfy the output.

    fgumi exits 1 on a difference. If the report were written directly to
    `{output}`, the failed run would leave a complete file behind and the next
    invocation would treat the cell as done — converting a hard failure into a
    silent skip.
    """
    rule = COMPARE_SMK.split("rule compare_compat_identity:")[1].split("\nrule ")[0]
    assert "{output.report}.tmp" in rule
    assert "mv {output.report}.tmp {output.report}" in rule


def test_fgumi_diff_detail_survives_a_failing_gate() -> None:
    """A DIFFER exit must leave its diagnosis somewhere an operator can read.

    `> {output}.tmp 2>&1` captures BOTH streams into a file that a failing run
    then never `mv`s, that is not a declared output so Batch never uploads it,
    and that dies with the worker. Because stderr was captured too, CloudWatch
    gets nothing either — so `--max-diffs 20` computes the first twenty differing
    records and throws them away, and the cell can only be diagnosed by
    re-downloading both BAMs (measured cost: 2 GB).

    `report.rs` documents CloudWatch stderr as the escape hatch for exactly this
    case. Piping through `tee ... >&2` keeps the report file AND puts the detail
    in the worker log.

    Read through `_code_only`, not raw source. The comments in these rules
    necessarily QUOTE the constructs asserted on here (`| tee`, `>&2`) to explain
    why they are there, so a raw-text match is satisfied by the prose and passes
    even when the shell command has lost them. Mutation-checked: dropping `>&2`
    from `compare_compat_identity`'s command passes the raw form and fails this
    one.
    """
    for rule_name in (
        "compare_compat_identity",
        "compare_bwa_identity",
        "compat_thread_invariance",
    ):
        rule = _code_only(COMPARE_SMK.split(f"rule {rule_name}:")[1].split("\nrule ")[0])
        assert "| tee" in rule, f"{rule_name} must tee the fgumi output"
        assert ">&2" in rule, f"{rule_name} must send the diff detail to stderr"
        # The regression is specifically "captured stderr into the report file",
        # i.e. a `2>&1` whose target is the redirect rather than the tee pipe.
        assert "> {output.report}.tmp 2>&1" not in rule, (
            f"{rule_name} still redirects stderr into the report file, which is "
            f"what strands the diagnosis on a failing worker"
        )


def test_fgumi_gates_depend_on_pipefail_to_see_a_failure() -> None:
    """`tee` exits 0, so the gate's exit status is the pipeline's last command
    unless `pipefail` is set — without it a DIFFER would be reported as success.

    snakemake happens to prepend `set -euo pipefail` today, but only while
    nothing calls `shell.prefix()`, which replaces that prefix wholesale. These
    rules set it explicitly for that reason, and the `tee` form makes the
    dependency load-bearing rather than belt-and-braces.

    Read through `_code_only` for the same reason as the test above: the rules'
    own comments explain the `pipefail` dependency by naming it, so a raw match
    passes even with the real directive deleted.
    """
    for rule_name in (
        "compare_compat_identity",
        "compare_bwa_identity",
        "compat_thread_invariance",
    ):
        rule = _code_only(COMPARE_SMK.split(f"rule {rule_name}:")[1].split("\nrule ")[0])
        assert "set -euo pipefail" in rule, f"{rule_name} must set pipefail explicitly"


def test_fgumi_is_pinned_and_built_with_the_compare_feature() -> None:
    """`compare` is feature-gated off in a default fgumi build, and the pin is
    part of a release's evidence, so both must be explicit."""
    # fgumi is cargo-installed in the BASE image (docker/Dockerfile.base): it is
    # pinned independently of FG_LABS_SHA, so it lives with the other
    # SHA-independent tools rather than being rebuilt per benchmarked commit.
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.base").read_text()
    env = (REPO_ROOT / "docker" / "build-arg-defaults.env").read_text()
    assert "--features compare" in dockerfile
    assert "FGUMI_REF=" in env and "FGUMI_REPO=" in env
    # A moving ref would make the comparison's verdict irreproducible.
    ref = next(
        line.split("=", 1)[1].strip() for line in env.splitlines() if line.startswith("FGUMI_REF=")
    )
    assert len(ref) == _FULL_SHA_LEN and all(c in "0123456789abcdef" for c in ref), ref


def test_fgumi_threads_match_the_batch_reservation() -> None:
    """fgumi's content engine actually uses its threads, so the `--threads` it is
    given must be the `threads:` the executor reserves vCPUs from."""
    rule = COMPARE_SMK.split("rule compare_compat_identity:")[1].split("\nrule ")[0]
    assert "threads: 4" in rule
    assert "--threads {threads}" in rule


def test_every_aggregator_rule_is_a_localrule() -> None:
    """An input-only rule is a target alias, not work; it must run on the
    coordinator.

    Our executor fork submits any non-local rule to Batch, so an aggregator left
    out of `localrules:` is dispatched as a worker job that exists only to
    declare inputs -- burning a spot instance and a container pull to do nothing.
    Written as a sweep over every input-only rule rather than a check for
    `compat`/`compat_smoke` specifically, because the failure is one someone
    re-introduces by adding the NEXT target and forgetting the declaration.
    """
    declared = {
        line.strip().rstrip(",")
        for line in SNAKEFILE.split("localrules:")[1].split("\n\n")[0].splitlines()
        if line.strip().rstrip(",")
    }
    aggregators = set()
    for block in SNAKEFILE.split("\nrule ")[1:]:
        name = block.split(":", 1)[0].strip()
        body = block.split("\n\n\n")[0]
        directives = {"shell:", "run:", "output:", "script:", "wrapper:"}
        if "    input:" in body and not any(d in body for d in directives):
            aggregators.add(name)
    assert aggregators, "expected to find at least one aggregator rule"
    assert aggregators <= declared, f"aggregators missing from localrules: {aggregators - declared}"


@pytest.mark.parametrize("target", ["compat", "compat_bwa", "alt"])
def test_every_arch_iterating_compat_target_is_a_full_sweep_target(target: str) -> None:
    """`--target <compat arm>` with no `--archs` must expand to every arch.

    Targets outside `_FULL_SWEEP_TARGETS` fall back to `core_arch` alone -- which
    is how the `fast` run silently measured one arch instead of six. Both compat
    arms build their target list `for arch in ARCHS`, so both qualify, and the
    failure is silent: a one-arch run still reports a clean pass. It costs the
    bwa arm the most, since comparing ARM against an ARM upstream directly is
    the whole reason that arm exists.
    """
    submit = (REPO_ROOT / "bwa_mem3_bench" / "commands" / "_submit.py").read_text()
    full_sweep = submit.split("_FULL_SWEEP_TARGETS = frozenset(")[1].split("\n)")[0]
    assert f'"{target}"' in full_sweep


# ---------------------------------------------------------------------------
# `-K` batch pinning and the thread-invariance gate.
# ---------------------------------------------------------------------------


def test_batch_size_is_pinned_for_both_bwa_family_aligners() -> None:
    """`-K` must reach bwa-mem3 AND upstream bwa-mem2, or the comparison is asymmetric.

    Default batching is `chunk_size * n_threads`, and `mem_pestat` reads the batch,
    so an unpinned side would make output a function of `-t`.
    """
    align = (REPO_ROOT / "workflow" / "rules" / "align.smk").read_text()
    assert "def _batch_flag(" in align
    fg = align.split("rule align_fg_labs:")[1].split("\nrule ")[0]
    base = align.split("rule align_baseline:")[1].split("\nrule ")[0]
    for name, body in (("align_fg_labs", fg), ("align_baseline", base)):
        assert "batch_flag = _batch_flag()" in body, name
        assert "{params.batch_flag}" in body, name


def test_batch_pinning_does_not_reach_the_scaling_ladder() -> None:
    """The ladder measures the batch-size/thread interaction; pinning the batch
    would flatten exactly the effect Gate #3 reads.

    This is why `-K` is a separate helper rather than folded into `_mem_flags` —
    scaling.smk uses `_mem_flags` too, so it would have inherited it silently.
    """
    scaling = (REPO_ROOT / "workflow" / "rules" / "scaling.smk").read_text()
    assert "batch_flag" not in scaling
    assert "_batch_flag" not in scaling


def test_batch_pinning_does_not_reach_minibwa_or_bwameth() -> None:
    """minibwa's `-K` has different semantics and it is a wall-time comparator that
    should run at its author's defaults; bwameth has no such flag at all."""
    minibwa = (REPO_ROOT / "workflow" / "rules" / "align_minibwa.smk").read_text()
    assert "batch_flag" not in minibwa
    align = (REPO_ROOT / "workflow" / "rules" / "align.smk").read_text()
    # Only the bwameth arm of the if/else — the else arm is upstream bwa-mem2,
    # which SHOULD carry the flag.
    bwameth_branch = align.split("bwameth.py --threads")[1].split("else")[0]
    assert "batch_flag" not in bwameth_branch


def test_batch_bases_is_a_literal_not_derived_from_threads() -> None:
    """Deriving `-K` from `threads` would re-couple the golden to the knob this
    decouples it from. It must be a fixed literal equal to the historical default
    (chunk_size 10,000,000 x 16 threads), which is what makes it output-neutral.
    """
    cfg = load_config(CONFIG_DIR)
    assert cfg.batch_bases == 10_000_000 * 16
    raw = (CONFIG_DIR / "defaults.yaml").read_text()
    line = next(ln for ln in raw.splitlines() if ln.startswith("batch_bases:"))
    assert line.split(":", 1)[1].strip().isdigit(), line


def test_thread_invariance_gate_exists_and_compares_two_thread_counts() -> None:
    """Without this the `-K` pin is a claim in a comment rather than a property.

    Self-comparison by design: no upstream baseline, so it needs no x86 host and
    no new Batch queue.
    """
    rule = COMPARE_SMK.split("rule compat_thread_invariance:")[1].split("\nrule ")[0]
    assert "COMPAT_INVARIANCE_THREADS" in COMPARE_SMK
    assert "fgumi compare bams" in rule
    assert "--compat=bwa-mem2" in rule
    assert "{params.batch_flag}" in rule, "invariance is only expected WITH -K pinned"
    assert "{output.report}.tmp" in rule and "mv {output.report}.tmp" in rule


def test_invariance_high_rung_clears_the_historical_cap_threshold() -> None:
    """The high thread count must exceed 26, where the default batch (10M * 26 =
    260M) crossed the 256M bound bwa-mem3 used to cap at — the thread range where
    unpinned batching provably diverged from upstream."""
    smk = COMPARE_SMK.split("COMPAT_INVARIANCE_THREADS = (")[1].split(")")[0]
    hi = int(smk.split(",")[1].strip())
    assert hi > _CAP_THRESHOLD_THREADS, hi


def test_invariance_gate_is_in_bless_release() -> None:
    body = SNAKEFILE.split("rule bless_release:")[1].split("\nrule ")[0]
    assert "_compat_invariance_target()" in body


# ---------------------------------------------------------------------------
# The `--compat=bwa-mem` arm against lh3/bwa.
# ---------------------------------------------------------------------------

ALIGN_BWA_SMK = (REPO_ROOT / "workflow" / "rules" / "align_bwa.smk").read_text()


def _code_only(text: str) -> str:
    """`text` with docstrings and `#` comments removed, but shell bodies KEPT.

    Several guards below assert that a token is ABSENT. Run against raw source
    those are unsound: this feature's prose necessarily NAMES the things the
    code must not do (`.bwt.2bit.64` is the index family bwa cannot read,
    `ARM_X86_REFERENCE_ARCH` is the fallback this rule deliberately omits), so a
    docstring explaining the decision would fail the test enforcing it -- and
    the obvious "fix" is to delete the explanation.

    A rule's `shell:` body is code, not prose, and is deliberately preserved: it
    is where a wrong index suffix would most plausibly appear (`align_bwa`'s
    prewarm `cat`s index files by name), so stripping it would leave exactly the
    mutation `test_bwa_arm_stages_bwas_own_index_family` exists to catch outside
    the guard's view. The match alternates over raw and plain blocks in source
    order so triple-quote pairing stays correct; matching only the plain ones
    would let a shell body's CLOSING delimiter pair with the next docstring's
    opening one and delete every line between them.
    """
    text = re.sub(
        r'r?""".+?"""',
        lambda m: m.group(0) if m.group(0).startswith("r") else "",
        text,
        flags=re.DOTALL,
    )
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_bwa_arm_pins_the_batch_size_like_the_other_two_aligners() -> None:
    """`-K` must reach bwa too, for exactly the reason it reaches the other two.

    bwa computes its default batch as `chunk_size * n_threads` and `mem_pestat`
    reads whatever lands in it, so an unpinned bwa run would be a function of
    `-t` and could not be compared against a pinned bwa-mem3 run at all.
    """
    assert "batch_flag = _batch_flag()" in ALIGN_BWA_SMK
    assert "{params.batch_flag}" in ALIGN_BWA_SMK


def test_bwa_arm_applies_the_same_mem_flags_as_the_other_arms() -> None:
    """Asymmetric `mem` flags would make the comparison measure the flags.

    `hic-1M` carries `-5 -S -P`; a bwa arm without them would diverge on
    mate rescue and read as a compat failure.
    """
    assert "mem_flags = lambda wc: _mem_flags(wc.sample)" in ALIGN_BWA_SMK
    assert "{params.mem_flags}" in ALIGN_BWA_SMK


def test_bwa_arm_is_cached_on_the_bwa_version_not_the_fg_labs_sha() -> None:
    """bwa's output depends only on its own pin, so a new fg-labs SHA must reuse
    the cached BAMs rather than re-running a materially slower aligner."""
    # The path pattern only -- asserting the `output:` block's internal column
    # alignment too would break this on a reformat that changed no behaviour.
    assert '"bwa/{tool_version}/{sample}/{arch}/rep-{rep}/aligned.bam"' in ALIGN_BWA_SMK
    assert "runs/{sha}" not in ALIGN_BWA_SMK


def test_bwa_arm_stages_bwas_own_index_family() -> None:
    """bwa reads `.bwt`/`.sa`; bwa-mem2 and bwa-mem3 read `.bwt.2bit.64`/`.0123`.
    Neither can read the other's, so staging the wrong family fails at runtime
    on a Batch worker rather than here."""
    code = _code_only(ALIGN_BWA_SMK)
    for suffix in (".amb", ".ann", ".bwt", ".pac", ".sa"):
        assert f'f"{{base}}{suffix}"' in code, suffix
    # The bwa-mem2 family must NOT be staged for this rule.
    assert ".bwt.2bit.64" not in code
    assert ".0123" not in code


def test_bwa_arm_prewarms_the_index_so_the_timed_region_excludes_load() -> None:
    """bwa has no `shm` command, so warm symmetry with `align_fg_labs` comes from
    the same `cat`-into-page-cache prewarm `align_baseline` uses. Without it the
    bwa arm eats its cold index load inside the timed region."""
    prewarm = ALIGN_BWA_SMK.split("tricorder")[0]
    assert "cat {input.ref[0]}.bwt" in prewarm
    assert "> /dev/null" in prewarm


def test_bwa_identity_gate_uses_fgumi_and_compares_against_the_bwa_arm() -> None:
    """The assertion is boolean byte-identity over all core fields, which
    compare-bams cannot make -- it never reads QNAME, SEQ, QUAL, TLEN or the
    header, so it would report 100% while SEQ or the `@SQ` dictionary differed."""
    body = COMPARE_SMK.split("rule compare_bwa_identity:")[1].split("\nrule ")[0]
    assert "fgumi compare bams" in body
    assert "bwa/{CONFIG.bwa_version}/" in body


def test_bwa_identity_gate_does_not_fall_back_to_an_x86_reference_arch() -> None:
    """The whole point of this arm on ARM.

    `compare_vs_baseline` and `compare_compat_identity` fall back to
    `ARM_X86_REFERENCE_ARCH` because upstream bwa-mem2 has no ARM build, making
    ARM concordance transitive. bwa DOES build on ARM (NEON since 0.7.18), so
    this arm compares same-arch against same-arch -- a fallback here would throw
    away the directness that justifies the arm.
    """
    body = _code_only(COMPARE_SMK.split("rule compare_bwa_identity:")[1].split("\nrule ")[0])
    assert "ARM_X86_REFERENCE_ARCH" not in body
    assert "{wc.arch}/rep-1/aligned.bam" in body


def test_bwa_identity_output_is_not_left_behind_on_a_difference() -> None:
    """A DIFFER exit must not leave a satisfied output, or the next run treats
    the failure as already-done and skips it. Same contract as the bwa-mem2 gate."""
    body = COMPARE_SMK.split("rule compare_bwa_identity:")[1].split("\nrule ")[0]
    assert "{output.report}.tmp" in body
    assert "mv {output.report}.tmp {output.report}" in body


@pytest.mark.parametrize("rule", ["compare_compat_identity", "compare_bwa_identity"])
def test_identity_gates_set_shell_strictness_explicitly(rule: str) -> None:
    """Both `fgumi` gates must set `-e` themselves, not inherit it.

    `mv` is the last command in these bodies, so without `-e` it runs after a
    DIFFER exit and supplies the rule's exit code -- the gate reports success on
    a real difference, which is the one failure mode it exists to prevent.
    Snakemake prepends `set -euo pipefail` today because it sets bash as the
    shell executable, but that prefix is dropped the moment anything calls
    `shell.prefix()`. A gate should not depend on that staying untrue.
    """
    # The shell body alone -- both docstrings name `fgumi compare bams`, so the
    # ordering check below would compare against the prose otherwise.
    body = COMPARE_SMK.split(f"rule {rule}:")[1].split("\nrule ")[0].split("shell:")[1]
    assert "set -euo pipefail" in body
    # Ordering matters as much as presence: after `fgumi` it guards nothing.
    assert body.index("set -euo pipefail") < body.index("fgumi compare bams")


def test_code_only_keeps_shell_bodies_but_drops_docstrings() -> None:
    """The absence guards must still see a rule's `shell:` body.

    A wrong index suffix is most plausible in the prewarm `cat`, which lives in
    a shell body; stripping those would put that mutation outside the view of
    the very guards written to catch it.
    """
    q = '"""'
    src = "\n".join(
        [
            f"{q}a docstring naming .bwt.2bit.64{q}",
            "x = 1  # trailing",
            f"shell = r{q}",
            "cat ref.bwt",
            q,
        ]
    )
    code = _code_only(src)
    assert ".bwt.2bit.64" not in code
    assert "cat ref.bwt" in code
    assert "trailing" not in code
    assert "x = 1" in code


def test_bwa_smoke_covers_both_an_x86_and_an_arm_arch() -> None:
    """c6a proves the arm; c8g proves the thing the bwa-mem2 arm structurally
    cannot -- a direct ARM-vs-ARM upstream comparison."""
    body = SNAKEFILE.split("rule compat_bwa_smoke:")[1].split("\nrule ")[0]
    assert "/c6a/rep-1/compare/bwa-identity.txt" in body
    assert "/c8g/rep-1/compare/bwa-identity.txt" in body


def test_bwa_arm_covers_every_compat_cell_on_every_arch() -> None:
    """The bwa arm is promoted from one smoke cell to the full compat matrix.

    This test replaces the scope pin that previously asserted the arm was
    smoke-only; the pin existed so promotion had to be a deliberate edit, and
    this is that edit. It stays as the guard against the opposite failure --
    a cell silently dropping out of the matrix.

    Deliberately mirrors `COMPAT_REAL_SAMPLES`: the two arms must cover the
    same datasets, or "bwa-mem2 parity implies bwa parity" is asserted on a
    narrower base than it is claimed for.
    """
    cfg = load_config(CONFIG_DIR)
    bwa_arms = {n for n, s in cfg.samples.items() if s.compat_target == "bwa-mem"}
    mem2_arms = {n for n, s in cfg.samples.items() if s.compat_target == "bwa-mem2"}
    bases_bwa = {n.removesuffix("-compat-bwa-mem") for n in bwa_arms}
    bases_mem2 = {n.removesuffix("-compat") for n in mem2_arms}
    assert bases_bwa == bases_mem2, (
        f"compat arms cover different datasets; "
        f"bwa-only={bases_bwa - bases_mem2}, mem2-only={bases_mem2 - bases_bwa}"
    )
    # The target uses ARCHS, not BASELINE_ARCHS -- bwa builds on arm64, so
    # restricting it to x86 would throw away the arm's main advantage.
    body = _code_only(SNAKEFILE.split("rule compat_bwa:")[1].split("\nrule ")[0])
    assert "_compat_bwa_targets(COMPAT_BWA_SAMPLES, REPS)" in body
    targets = _code_only(SNAKEFILE.split("def _compat_bwa_targets(")[1].split("\n\n\n")[0])
    assert "for arch in ARCHS" in targets
    assert "BASELINE_ARCHS" not in targets


def test_bwa_arm_is_not_in_the_release_bless_yet() -> None:
    """Still out of `bless_release` on purpose.

    The arm's cache is keyed on `bwa_version`, so the first full run pays for
    every cell and later runs are free. Folding it into the release gate before
    it has a track record would fail a bless on an arm nobody has watched.
    Delete this test in the change that promotes it.
    """
    bless = _code_only(SNAKEFILE.split("rule bless_release:")[1].split("\nrule ")[0])
    assert "bwa-identity" not in bless
    assert "_compat_bwa_targets" not in bless


# ---------------------------------------------------------------------------
# ALT-aware arms.
# ---------------------------------------------------------------------------

ALIGN_SMK = (REPO_ROOT / "workflow" / "rules" / "align.smk").read_text()


def test_alt_aware_flag_survives_the_yaml_round_trip() -> None:
    """`alt_aware: true` in YAML must reach the dataclass.

    This is the regression that motivated the test: the field was added to
    `Sample` and to the YAML, but not to the `Sample(...)` construction in
    `load_config`, so every arm silently loaded with `alt_aware=False`. The runs
    would have completed, staged no sidecar, exercised none of the ALT path, and
    reported "identical" -- a green result proving nothing at all.
    """
    cfg = load_config(CONFIG_DIR)
    alt = {n for n, s in cfg.samples.items() if s.alt_aware}
    assert alt, "expected at least one alt_aware sample"
    assert all(n.startswith("wgs-5M-alt") for n in alt), sorted(alt)
    # And the default must stay off for everything else, or the existing blessed
    # corpus would silently change meaning.
    assert cfg.samples["wgs-5M"].alt_aware is False


def test_alt_aware_samples_are_excluded_from_the_regression_sweep() -> None:
    """`wgs-5M-alt` must not reach `SWEEP_SAMPLES`, and the filter must be real.

    The ALT base sample is not a compat sibling and not a truth sample, so the
    two existing exclusions do not cover it -- it would land in `rule all` and
    `baseline_all` by default. Three things break at once if it does, and none
    of them is a cost argument:

      - `vs_golden` has no `wgs-5M-alt` BAM to compare against. The blessed
        corpus predates the sample, so Gate #2 asks for an input that cannot
        exist.
      - `vs_baseline` runs compare-bams under the `expect_tags` allowlist, which
        has no `pa` entry ON PURPOSE (`test_known_but_unemitted_tags_are_
        deliberately_not_allowlisted` in test_workflow_config.py). ALT-aware
        mapping is exactly the path that emits `pa:f:`, so the first such
        comparison fails BY NAME.
      - `bench regression` diffs this run's `all` outputs against the golden's,
        so a new sweep member silently changes what the gate compares.

    Hence the arms are driven by `--target alt` alone, where every
    comparison is `fgumi compare bams` (no allowlist) against an upstream
    computed with the same flag.
    """
    # `\n]` for the closing bracket, not `]`: the body contains `CONFIG.samples[s]`,
    # so a bare split truncates the block after its first subscript.
    sweep = SNAKEFILE.split("SWEEP_SAMPLES = [")[1].split("\n]")[0]
    assert "_is_alt_sample(s)" in sweep, "SWEEP_SAMPLES does not exclude ALT-aware samples"
    # Not vacuous: the config must actually declare one for the filter to matter.
    cfg = load_config(CONFIG_DIR)
    assert [n for n, s in cfg.samples.items() if s.alt_aware and not s.is_compat]


def test_alt_sidecar_is_staged_by_both_aligner_arms() -> None:
    """bwa and bwa-mem2/bwa-mem3 read the SAME `<idxbase>.alt`.

    Staging it for one arm and not the other would compare an ALT-aware aligner
    against an alt-naive one and attribute the difference to bwa-mem3.
    """
    for name, text in (("align.smk", ALIGN_SMK), ("align_bwa.smk", ALIGN_BWA_SMK)):
        code = _code_only(text)
        assert "alt_aware" in code, name
        assert 'f"{base}.alt"' in code, name


def _top_level_def(text: str, name: str) -> str:
    """The source of the top-level ``def <name>`` in an .smk file.

    An .smk file is not importable and not parseable as a whole (`rule x:` is
    not Python), but its module-level helpers are ordinary functions. Slicing
    one out by its own `def` line and the next dedent to column 0 yields a
    fragment `ast` can parse, which is what lets the guard below inspect the
    real conditional structure instead of matching nearby text.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"def {name}("))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_alt_sidecar_is_conditional_not_unconditional() -> None:
    """The sidecar must be gated on the flag.

    Staging it unconditionally would turn ALT-awareness on for every sample in
    the harness and silently invalidate every blessed baseline and golden.

    Checked structurally, not by proximity: an `if ... alt_aware` block sitting
    immediately above an UNCONDITIONAL append reads the same to a text scan but
    stages the sidecar for every sample. So parse the helper and require each
    `.alt` mention to fall inside the body of an `alt_aware` conditional.
    """
    for name, text, func in (
        ("align.smk", ALIGN_SMK, "_ref_inputs"),
        ("align_bwa.smk", ALIGN_BWA_SMK, "_bwa_ref_inputs"),
    ):
        source = _top_level_def(_code_only(text), func)
        tree = ast.parse(source)
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "alt_aware" in ast.unparse(node.test):
                for stmt in node.body:
                    guarded.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
        mentions = [
            i for i, line in enumerate(source.splitlines(), start=1) if '{base}.alt"' in line
        ]
        assert mentions, f"{name}: {func} does not stage the .alt sidecar at all"
        assert set(mentions) <= guarded, (
            f"{name}: .alt append is not inside an alt_aware conditional"
        )


def test_alt_aware_is_a_baseline_input_for_sibling_validation() -> None:
    """A sibling that enabled ALT-awareness while its base did not would be
    scored against a baseline computed with it OFF -- a guaranteed and entirely
    spurious compat failure. The guard must cover it like the other inputs."""
    common = {"baseline_tool": "bwa-mem2-upstream", "reference": "hg38", "source": "data/wgs/"}
    samples = {
        "wgs": Sample(name="wgs", alt_aware=False, **common),
        "wgs-compat": Sample(
            name="wgs-compat", alt_aware=True, fg_labs_flags=["--compat=bwa-mem2"], **common
        ),
    }
    with pytest.raises(ValueError, match="alt_aware"):
        _validate_compat_siblings(samples)


def test_fg_labs_checkout_fetches_the_sha_not_just_branches() -> None:
    """The image must be buildable at a SHA that is no longer on any branch.

    `git fetch --all` walks configured refspecs — branches and tags — so it can
    only reach a commit that currently heads one. Every blessed golden in this
    project is a release-please branch head (docs/release-allowances.yaml records
    the pattern for v0.5.0 through v0.8.0), and those branches are deleted on
    squash-merge. Rebuilding the v0.8.0 golden 4acb0956 failed with
    `fatal: reference is not a tree` for exactly that reason, which makes every
    historical golden unrebuildable — including for a bisect or a re-bless.

    Asserted against non-comment lines only. The comment above that RUN step has
    to name `git fetch --all` to explain why it is wrong, so a whole-file match
    is satisfied by the prose — the same way the fgumi-gate guards were, before
    they were moved onto `_code_only`.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    assert 'git fetch --quiet origin "${FG_LABS_SHA}"' in code
    assert "git fetch --all" not in code, (
        "`git fetch --all` cannot reach a commit that is not a branch head; "
        "fetch the SHA directly so deleted release branches stay buildable"
    )
    # The build-arg reaches this shell verbatim, so an unquoted expansion would
    # let a value carrying shell metacharacters execute inside the builder and
    # bake the result into an image the fleet then runs. `cli build` also refuses
    # any SHA that is not 40 hex chars; this is the second layer, and it is the
    # one that holds for callers that invoke docker directly.
    for unquoted in ("origin ${FG_LABS_SHA}", "git checkout ${FG_LABS_SHA}"):
        assert unquoted not in code, (
            f"unquoted expansion {unquoted!r} in docker/Dockerfile; quote it so a "
            "hostile FG_LABS_SHA cannot inject shell into the build"
        )


def test_rule_alt_covers_all_three_compat_modes() -> None:
    """ALT-awareness has to be exercised in the DEFAULT path too, not only under
    `--compat`.

    fg-labs/bwa-mem3#362 was a divergence of the default path from both upstreams
    (fixed by #363); the two compat arms assert byte-identity, but a default-mode
    arm is what covers where users actually run. All three modes share one
    ALT-aware bwa-mem2 baseline, built once for the base sample.

    Mode 1 cannot be a boolean identity gate — default bwa-mem3 differs from
    bwa-mem2 by construction on HN, MQ and the sidecar-derived @SQ — so it is
    graded via compare-bams, which post-FLAG-widening can finally see 0x2.
    """
    targets = _code_only(_top_level_def(_code_only(SNAKEFILE), "_alt_targets"))
    assert '/wgs-5M-alt"' in targets, "mode 1 (default, no --compat) must be requested"
    assert "vs-baseline.json" in targets, "mode 1 is graded, not a boolean gate"
    assert "vs-x86.json" in targets, "ARM has no upstream bwa-mem2 and needs the transitive route"
    assert "wgs-5M-alt-compat/" in targets and "compat-identity.txt" in targets
    assert "wgs-5M-alt-compat-bwa-mem/" in targets and "bwa-identity.txt" in targets
    # The rule itself must actually request them.
    body = _code_only(SNAKEFILE.split("rule alt:")[1].split("\nrule ")[0])
    assert "_alt_targets()" in body


def test_alt_arms_are_in_bless_release_but_not_rule_all() -> None:
    """`rule all` is Gate #1's cross-SHA anchor and its target set must stay
    byte-stable, so ALT joins the release matrix only.

    Without the `bless_release` entry the arms exist but nothing routine ever
    requests them, and the regression lock on the fg-labs/bwa-mem3#362 / #365
    fixes never fires.
    """
    bless = _code_only(SNAKEFILE.split("rule bless_release:")[1].split("\nrule ")[0])
    assert "_alt_targets(" in bless
    all_rule = _code_only(SNAKEFILE.split("rule all:")[1].split("\nrule ")[0])
    assert "alt" not in all_rule.replace("_all", "").replace("baseline_all", "")


def test_alt_smoke_covers_all_three_modes_on_one_baseline_arch() -> None:
    """The smoke must exercise the same three modes as `rule alt`, on an x86 arch.

    Deriving its targets from `_alt_targets` rather than restating the paths is
    the point: a smoke that drifts from the rule it screens proves nothing about
    it. The arch must be one upstream bwa-mem2 builds on, or mode 1 -- the arm's
    only graded mode -- cannot be requested at all.
    """
    body = _code_only(SNAKEFILE.split("rule alt_smoke:")[1].split("\nrule ")[0])
    assert "_alt_targets(" in body, "the smoke must derive its targets from the rule's"
    arches = re.findall(r'_alt_targets\(\["([^"]+)"\]\)', body)
    assert arches, "the smoke must pin an explicit arch list"
    cfg = load_config(CONFIG_DIR)
    for arch in arches:
        assert cfg.archs[arch].platform == "linux/amd64", (
            f"{arch} has no upstream bwa-mem2 build, so the graded mode-1 cell "
            f"cannot be produced there"
        )


def test_alt_targets_routes_on_platform_not_on_the_selected_arch_list() -> None:
    """`_alt_targets` must ask `_has_upstream_baseline`, never `BASELINE_ARCHS`.

    `BASELINE_ARCHS` is derived from ARCHS, the user's selection, which defaults
    to `core_arch` alone -- an ARM arch. `rule alt_smoke` names c6a literally, so
    a membership test against that list would find it absent and route the cell
    to `vs-x86.json`: a target no rule can produce for an x86 arch, and a silent
    one, because the mode-1 path would simply disappear from the smoke.
    """
    source = _code_only(_top_level_def(_code_only(SNAKEFILE), "_alt_targets"))
    assert "_has_upstream_baseline(" in source
    assert "BASELINE_ARCHS" not in source, (
        "BASELINE_ARCHS is scoped to the run's arch selection and misroutes an "
        "arch named literally by a smoke target"
    )


def test_alt_base_sample_stays_out_of_the_regression_sweep() -> None:
    """`wgs-5M-alt` must not join SWEEP_SAMPLES: `rule all`'s target set is the
    cross-SHA regression anchor, and a new member silently changes what Gate #1
    compares against the golden."""
    sweep = _code_only(SNAKEFILE.split("SWEEP_SAMPLES = [")[1].split("\n]")[0])
    assert "_is_alt_sample(s)" in sweep


def test_tachyon_is_pinned_to_an_exact_version() -> None:
    """The probe's version is what makes its readings comparable across releases.

    tachyon measures the HOST -- memory access rate under current contention -- so
    its scores get recorded beside timings and compared weeks later. A change to
    WHAT it measures silently invalidates every earlier score, with no error to
    notice, which makes a floating pin worse here than for a tool whose output is
    a pass/fail verdict.

    So an EXACT `x.y.z`, never a range and never a caret: `0.1` or `^0.1.0` would
    let an image rebuild months later install a different probe and quietly
    change what the numbers mean.
    """
    env = (REPO_ROOT / "docker" / "build-arg-defaults.env").read_text()
    assert "TACHYON_VERSION=" in env
    version = next(
        line.split("=", 1)[1].strip()
        for line in env.splitlines()
        if line.startswith("TACHYON_VERSION=")
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"TACHYON_VERSION must be an exact x.y.z, got {version!r}"
    )
    # `--locked` matters as much as the version: without it cargo may resolve
    # dependencies differently from what the crate was published with.
    #
    # Shell continuations are joined first -- the install is written across two
    # lines, so a per-line search finds `cargo` and `tachyon` in different lines
    # and matches neither.
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.base").read_text().replace("\\\n", " ")
    # Comment lines are dropped before the search. Dockerfile.base's header prose
    # names the cargo-installed tools, tachyon among them, so a raw scan matches
    # the explanation instead of the install and asserts against prose.
    install = next(
        line
        for line in dockerfile.splitlines()
        if not line.lstrip().startswith("#") and "tachyon" in line and "cargo" in line
    )
    assert "--locked" in install, install
    assert '--version "${TACHYON_VERSION}"' in install, (
        f"the install must consume the pinned ARG, not a literal: {install}"
    )


def test_every_defaultless_dockerfile_arg_reaches_the_generated_build_command() -> None:
    """An `ARG` with no default that `cli build` never passes is a silent empty string.

    BuildKit exports an unset `ARG` to the RUN shell as a set-but-EMPTY variable,
    so a forgotten `--build-arg` does not fail the build -- it produces
    `--rev ""`, or falls through to whatever the tool's own default is. This repo
    has already shipped months of sse41-baselined binaries on AVX2 hosts through
    exactly that hole (the pre-#6 `BASELINE_ARCH` bug).

    Asserted against the COMMAND `build()` actually generates, captured by
    monkeypatching `run_cmd`, rather than by grepping build.py's source. A raw
    text search is satisfied by a comment, a docstring or a dead branch -- the
    same trap `_code_only` exists for elsewhere in this file -- and would pass
    while the flag never reached buildx.

    Scoped to DEFAULTLESS args on purpose. `ARG LLVM_VERSION=19` and
    `ARG TRICORD_VERSION=0.1.2` are safe unpassed because the in-Dockerfile
    default applies, so requiring them would be noise that trains people to
    weaken the test. BuildKit's own `TARGET*`/`BUILD*` built-ins are excluded for
    the same reason.

    Derived from the Dockerfile, so adding a pin and forgetting to wire it up
    fails here instead of at runtime.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    buildkit_builtins = {
        "TARGETPLATFORM",
        "TARGETOS",
        "TARGETARCH",
        "TARGETVARIANT",
        "BUILDPLATFORM",
        "BUILDOS",
        "BUILDARCH",
        "BUILDVARIANT",
    }
    defaultless = {
        arg
        for arg in re.findall(r"^ARG ([A-Z][A-Z0-9_]*)\s*$", dockerfile, re.MULTILINE)
        if arg not in buildkit_builtins
    }
    assert defaultless, "no defaultless ARGs found -- has the Dockerfile moved?"

    build_module = importlib.import_module("bwa_mem3_bench.commands._build")

    captured: list[list[str]] = []

    def _capture(cmd: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:  # noqa: ARG001
        captured.append(cmd)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "run_cmd", _capture)
        build_module.build(fg_labs_sha="0" * 40, image_name="test", dry_run=True)
    finally:
        monkeypatch.undo()

    buildx = next((c for c in captured if "buildx" in c), None)
    assert buildx is not None, f"no buildx command generated; captured {captured}"
    # Read the NAME half of each --build-arg pair, so a coincidental match
    # elsewhere in the command line cannot satisfy the assertion.
    passed = {
        arg.split("=", 1)[0]
        for flag, arg in pairwise(buildx)
        if flag == "--build-arg" and "=" in arg
    }
    missing = sorted(defaultless - passed)
    assert not missing, (
        f"Dockerfile declares defaultless ARG(s) the generated buildx command "
        f"never passes: {missing}. BuildKit exports those as set-but-empty, which "
        "does not fail the build -- it silently builds the wrong thing."
    )
