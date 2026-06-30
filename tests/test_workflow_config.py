"""Unit tests for workflow_config loader."""

import dataclasses
from pathlib import Path

import pytest

from bwa_mem3_bench.workflow_config import (
    Arch,
    Sample,
    WorkflowConfig,
    _as_bool,
    _as_str_list,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

EXPECTED_THREADS = 16
EXPECTED_REPS_DEFAULT = 1
EXPECTED_REPS_BASELINE = 5


def test_load_config_returns_expected_samples() -> None:
    cfg = load_config(CONFIG_DIR)
    assert isinstance(cfg, WorkflowConfig)
    assert "smoke-1M" in cfg.samples
    assert "wgs-5M" in cfg.samples
    assert "meth-twist-emseq-5M" in cfg.samples
    assert cfg.samples["wgs-5M"].baseline_tool == "bwa-mem2-upstream"
    assert cfg.samples["meth-twist-emseq-5M"].baseline_tool == "bwameth"
    assert cfg.samples["meth-twist-emseq-5M"].fg_labs_flags == ["--meth"]


def test_load_config_returns_expected_archs() -> None:
    cfg = load_config(CONFIG_DIR)
    assert "c8g" in cfg.archs
    assert cfg.archs["c8g"].instance_type == "c8g.4xlarge"
    assert cfg.archs["c8g"].platform == "linux/arm64"
    assert cfg.core_arch == "c8g"
    assert set(cfg.full_archs) == {"c7g", "c6a", "c7i", "c8g", "c7a", "m7i"}


def test_arch_baseline_arch_field() -> None:
    """Every arch currently uses the portable image (`baseline_arch=""`).

    The per-rule image plumbing is wired end-to-end and tested, but the
    AVX-512BW image variant produced by `BASELINE_ARCH=avx512bw` is not
    a perf win on this workload (per the fg-labs/bwa-mem3 AVX-512
    baseline-build Phase C benchmarking). When upstream lands a fix,
    set c7a / c7i / m7i back to "avx512bw" here.
    """
    cfg = load_config(CONFIG_DIR)
    for arch in ("c6a", "c7a", "c7i", "c7g", "c8g", "m7i"):
        assert cfg.archs[arch].baseline_arch == "", (
            f"{arch}.baseline_arch should be parked at ''; got {cfg.archs[arch].baseline_arch!r}"
        )


_TEST_ECR = "550079046206.dkr.ecr.us-east-1.amazonaws.com/bwa-mem3-bench"
_TEST_SHA = "abcdef0"


def test_arch_image_uri_all_archs_use_portable_tag_today() -> None:
    """Every arch resolves to the bare `<sha>` portable tag right now —
    matches the parked `baseline_arch=""` config (see test above)."""
    cfg = load_config(CONFIG_DIR)
    for arch in ("c6a", "c7a", "c7i", "c7g", "c8g", "m7i"):
        uri = cfg.archs[arch].image_uri(ecr_repo_uri=_TEST_ECR, fg_labs_sha=_TEST_SHA)
        assert uri == f"{_TEST_ECR}:{_TEST_SHA}", f"{arch}: {uri}"
        assert "-" not in uri.split(":")[-1], f"{arch} unexpected suffix in {uri}"


def test_arch_image_uri_with_baseline_arch_set_appends_suffix() -> None:
    """Method-level test: when `baseline_arch` is set on an Arch (e.g.
    after upstream lands an AVX-512 fix and we flip the config), the
    image URI gets the matching tag suffix. Constructs an Arch directly
    so this test stays green even if the production config stays parked."""
    arch = Arch(
        name="c7a",
        instance_type="c7a.4xlarge",
        batch_queue="q",
        simd="avx512",
        platform="linux/amd64",
        baseline_arch="avx512bw",
    )
    uri = arch.image_uri(ecr_repo_uri=_TEST_ECR, fg_labs_sha=_TEST_SHA)
    assert uri == f"{_TEST_ECR}:{_TEST_SHA}-avx512bw"


def test_load_config_returns_expected_defaults() -> None:
    cfg = load_config(CONFIG_DIR)
    assert cfg.bucket == "bwa-mem3-bench"
    assert cfg.region == "us-east-1"
    assert cfg.threads == EXPECTED_THREADS
    assert cfg.reps_default == EXPECTED_REPS_DEFAULT
    assert cfg.reps_baseline == EXPECTED_REPS_BASELINE


def test_unknown_sample_raises() -> None:
    cfg = load_config(CONFIG_DIR)
    with pytest.raises(KeyError):
        _ = cfg.samples["does-not-exist"]


def test_sample_compare_options_default_empty() -> None:
    cfg = load_config(CONFIG_DIR)
    sample = cfg.samples["wgs-5M"]
    assert sample.compare_options == {}


def _make_sample(layout: str = "paired") -> Sample:
    return Sample(
        name="t",
        baseline_tool="bwa-mem2-upstream",
        reference="hg38",
        source="data/x/hg002-1M/",
        layout=layout,
    )


def test_sample_layout_defaults_to_paired() -> None:
    assert _make_sample().layout == "paired"


def test_sample_fastq_names_paired_returns_r1_r2() -> None:
    assert _make_sample("paired").fastq_names == ("r1.fq.gz", "r2.fq.gz")


def test_sample_fastq_names_single_returns_r1_only() -> None:
    assert _make_sample("single").fastq_names == ("r1.fq.gz",)


def test_sample_invalid_layout_raises() -> None:
    with pytest.raises(ValueError, match="layout"):
        _make_sample("bogus")


def test_sample_is_meth_predicate() -> None:
    """`is_meth` is the single meth predicate: true iff the bwameth baseline or
    an fg-labs `--meth` flag is in play."""
    assert _make_sample().is_meth is False  # bwa-mem2-upstream / no --meth
    assert (
        Sample(name="t", baseline_tool="bwameth", reference="hg38-meth", source="s/").is_meth
        is True
    )
    assert (
        Sample(
            name="t",
            baseline_tool="bwa-mem2-upstream",
            reference="hg38-meth",
            source="s/",
            fg_labs_flags=["--meth"],
        ).is_meth
        is True
    )


def test_sample_meth_reference_invariant_enforced() -> None:
    """A meth sample MUST use a `-meth` reference and a non-meth sample MUST NOT
    — so reference-staging (keyed on the reference) and the `--meth` exec flag
    (keyed on `is_meth`) can never desync from a hand-edited config."""
    # meth tool but plain reference -> reject
    with pytest.raises(ValueError, match="meth"):
        Sample(name="t", baseline_tool="bwameth", reference="hg38", source="s/")
    # --meth flag but plain reference -> reject
    with pytest.raises(ValueError, match="meth"):
        Sample(
            name="t",
            baseline_tool="bwa-mem2-upstream",
            reference="hg38",
            source="s/",
            fg_labs_flags=["--meth"],
        )
    # non-meth tool but meth reference -> reject
    with pytest.raises(ValueError, match="meth"):
        Sample(name="t", baseline_tool="bwa-mem2-upstream", reference="hg38-meth", source="s/")
    # consistent configs are accepted
    Sample(name="t", baseline_tool="bwameth", reference="hg38-meth", source="s/")
    Sample(name="t", baseline_tool="bwa-mem2-upstream", reference="hg38", source="s/")


def test_config_samples_satisfy_meth_invariant() -> None:
    """Every shipped sample already satisfies the meth/reference invariant."""
    cfg = load_config(CONFIG_DIR)
    for sample in cfg.samples.values():
        assert sample.is_meth == sample.reference.endswith("-meth"), sample.name


def test_load_config_includes_new_samples() -> None:
    cfg = load_config(CONFIG_DIR)

    hic = cfg.samples["hic-1M"]
    assert hic.layout == "paired"
    assert hic.baseline_tool == "bwa-mem2-upstream"
    assert hic.reference == "hg38"
    assert hic.fastq_names == ("r1.fq.gz", "r2.fq.gz")

    sbx = cfg.samples["sbx-1M"]
    assert sbx.layout == "single"
    assert sbx.baseline_tool == "bwa-mem2-upstream"
    assert sbx.reference == "hg38"
    assert sbx.fastq_names == ("r1.fq.gz",)


def test_mem_flags_default_empty_and_hic_uses_canonical_hic_flags() -> None:
    cfg = load_config(CONFIG_DIR)
    # mem_flags are empty for ordinary samples (no alignment-mode change).
    assert cfg.samples["wgs-5M"].mem_flags == []
    assert cfg.samples["sbx-1M"].mem_flags == []
    # hic-1M uses canonical Hi-C flags (-5 -S -P): skip mate rescue/pairing,
    # smallest-coord split as primary. Disabling mate rescue also removes the
    # huge mate-SW windows that OOM'd ARM workers.
    assert cfg.samples["hic-1M"].mem_flags == ["-5", "-S", "-P"]


def test_minibwa_flags_translate_mem_flags() -> None:
    cfg = load_config(CONFIG_DIR)
    # Samples with no mem_flags get no minibwa flags.
    assert cfg.samples["wgs-5M"].minibwa_flags == []
    assert cfg.samples["sbx-1M"].minibwa_flags == []
    # hic-1M's bwa `-5 -S -P` maps to minibwa `-5 -P --rescue=0` (minibwa has
    # no `-S`; mate rescue is disabled with `--rescue=0`), so the minibwa Hi-C
    # run skips mate rescue exactly like the bwa-mem3 / bwa-mem2 arms.
    assert cfg.samples["hic-1M"].minibwa_flags == ["-5", "--rescue=0", "-P"]


def test_minibwa_flags_rejects_unmapped_flag() -> None:
    cfg = load_config(CONFIG_DIR)
    bad = dataclasses.replace(cfg.samples["wgs-5M"], mem_flags=["-Z"])
    with pytest.raises(ValueError, match="no minibwa"):
        _ = bad.minibwa_flags


def test_truth_defaults_false_for_ordinary_samples() -> None:
    cfg = load_config(CONFIG_DIR)
    assert cfg.samples["wgs-5M"].truth is False
    assert cfg.samples["meth-twist-emseq-5M"].truth is False
    assert _make_sample().truth is False


def test_truth_rejects_non_boolean() -> None:
    """A non-boolean `truth` value (e.g. quoted YAML) must fail loudly, not be
    silently coerced by bool()."""
    assert _as_bool("s", "truth", True) is True
    assert _as_bool("s", "truth", False) is False
    with pytest.raises(ValueError, match="truth"):
        _as_bool("s", "truth", "yes")


def test_sim_samples_are_truth_samples() -> None:
    cfg = load_config(CONFIG_DIR)
    for name in (
        "sim-wgs-place",
        "sim-wgs-vars",
        "sim-meth-place",
        "sim-meth-place-genomic",
        "sim-meth-vars",
        "sim-meth-vars-genomic",
        "sim-smoke-vars",
        "sim-smoke-meth-vars",
        "sim-smoke-meth-vars-genomic",
    ):
        assert cfg.samples[name].truth is True, name


def test_sim_non_meth_arms() -> None:
    """Non-meth sim datasets carry no fg_labs_flags and use the hg38 reference +
    bwa-mem2 baseline (graded vs minibwa + upstream)."""
    cfg = load_config(CONFIG_DIR)
    for name in ("sim-wgs-place", "sim-wgs-vars"):
        sample = cfg.samples[name]
        assert sample.reference == "hg38"
        assert sample.baseline_tool == "bwa-mem2-upstream"
        assert sample.fg_labs_flags == []


def test_sim_meth_collapsed_and_genomic_arms_share_source() -> None:
    """The genomic D3 arm is a separate sample sharing FASTQs + truth (`source`)
    with its collapsed sibling; only the fg_labs_flags differ."""
    cfg = load_config(CONFIG_DIR)
    collapsed = cfg.samples["sim-meth-vars"]
    genomic = cfg.samples["sim-meth-vars-genomic"]
    assert collapsed.source == genomic.source == "data/sim/sim-meth-vars/"
    assert collapsed.reference == genomic.reference == "hg38-meth"
    assert collapsed.fg_labs_flags == ["--meth"]
    assert genomic.fg_labs_flags == ["--meth", "--meth-scoring", "genomic"]


def test_fast_siblings_share_source_and_only_add_fast_flag() -> None:
    """Each `--fast` preset sibling (fg-labs/bwa-mem3 PR #189) shares its base
    sample's `source`, reference, and baseline_tool, and differs only by adding
    `--fast` to fg_labs_flags — so default-vs-fast isolates the preset."""
    cfg = load_config(CONFIG_DIR)
    pairs = {
        "wgs-5M": "wgs-5M-fast",
        "wes-5M": "wes-5M-fast",
        "meth-twist-emseq-5M": "meth-twist-emseq-5M-fast",
        "sim-wgs-place": "sim-wgs-place-fast",
        "sim-wgs-vars": "sim-wgs-vars-fast",
        "sim-meth-place": "sim-meth-place-fast",
        "sim-meth-vars": "sim-meth-vars-fast",
    }
    for base_name, fast_name in pairs.items():
        base = cfg.samples[base_name]
        fast = cfg.samples[fast_name]
        assert fast.source == base.source, fast_name
        assert fast.reference == base.reference, fast_name
        assert fast.baseline_tool == base.baseline_tool, fast_name
        assert fast.truth == base.truth, fast_name
        # The fast arm's flags are the base's flags plus `--fast`, nothing else.
        assert fast.fg_labs_flags == [*base.fg_labs_flags, "--fast"], fast_name
        # is_meth must agree with the (shared) reference — `--fast` never flips it.
        assert fast.is_meth == base.is_meth, fast_name


def test_meth_fast_sibling_keeps_compare_ignore_tags() -> None:
    """The meth `--fast` sibling carries the same bisulfite ignore-tags so its
    vs-default / vs-baseline compares stay apples-to-apples with the default."""
    cfg = load_config(CONFIG_DIR)
    fast = cfg.samples["meth-twist-emseq-5M-fast"]
    assert fast.compare_options.get("ignore_tags") == ["YD", "XM", "XG"]


def test_as_str_list_accepts_list_of_strings() -> None:
    assert _as_str_list("s", "mem_flags", ["-5", "-S"]) == ["-5", "-S"]


def test_as_str_list_accepts_empty_default() -> None:
    assert _as_str_list("s", "mem_flags", []) == []


def test_as_str_list_rejects_scalar_string() -> None:
    # A bare YAML string would be silently split into ['-', '5'] by list();
    # the loader must reject it instead.
    with pytest.raises(ValueError, match="mem_flags"):
        _as_str_list("s", "mem_flags", "-5")


def test_as_str_list_rejects_non_string_elements() -> None:
    with pytest.raises(ValueError, match="fg_labs_flags"):
        _as_str_list("s", "fg_labs_flags", [1, 2])
