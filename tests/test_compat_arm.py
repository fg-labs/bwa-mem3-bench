"""Wiring guards for the `--compat=bwa-mem2` byte-identity arm.

The Python half of this feature (strict tag policy, sibling validation) is
covered in `test_workflow_config.py`. What is left is the workflow wiring, which
lives in Snakemake files that cannot be imported -- so these read the sources as
text, the same approach `test_thread_packing.py` uses for the `threads:`
directive. Each guard pins a decision whose breakage would be silent: the run
would still succeed and still report a number, just not the number claimed.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def test_compat_is_a_full_sweep_target() -> None:
    """`--target compat` with no `--archs` must expand to every arch.

    Targets outside `_FULL_SWEEP_TARGETS` fall back to `core_arch` alone -- which
    is how the `fast` run silently measured one arch instead of six.
    """
    submit = (REPO_ROOT / "bwa_mem3_bench" / "commands" / "submit.py").read_text()
    full_sweep = submit.split("_FULL_SWEEP_TARGETS = frozenset(")[1].split(")")[0]
    assert '"compat"' in full_sweep
