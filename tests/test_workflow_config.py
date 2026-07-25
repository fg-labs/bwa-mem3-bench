"""Unit tests for workflow_config loader."""

import dataclasses
from pathlib import Path

import pytest
import yaml

from bwa_mem3_bench.workflow_config import (
    COMPARE_KINDS,
    METH_EXTRA_TAGS,
    METH_UNEMITTED_TAGS,
    Arch,
    Sample,
    ThreadScaling,
    WorkflowConfig,
    _as_bool,
    _as_positive_int,
    _as_str_list,
    _thread_scaling_from,
    load_config,
    parse_ladder_override,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

EXPECTED_THREADS = 16
EXPECTED_REPS_DEFAULT = 1
EXPECTED_REPS_BASELINE = 5


def _write_minimal_config(
    config_dir: Path,
    *,
    compare_options: object,
    compare_defaults: object | None = None,
) -> None:
    """Stage a loadable config whose single sample carries `compare_options`.

    Copies the real `archs.yaml` / `defaults.yaml` so only the sample block
    under test differs from production. `compare_defaults` defaults to a valid
    block for every kind, because `load_config` validates it BEFORE the samples
    and would otherwise mask whatever the test is actually probing; passing it
    explicitly is what makes its own validation branches reachable.

    `defaults.yaml`'s `thread_scaling.sample` is repointed at the synthetic
    sample: it names a production sample (`wgs-5M`) that this config does not
    define, and `load_config` validates that reference, so copying the block
    verbatim would make every config this helper writes unloadable for a reason
    unrelated to what the caller is testing.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "archs.yaml").write_text((CONFIG_DIR / "archs.yaml").read_text())
    defaults = yaml.safe_load((CONFIG_DIR / "defaults.yaml").read_text())
    if "thread_scaling" in defaults:
        defaults["thread_scaling"]["sample"] = "probe"
    (config_dir / "defaults.yaml").write_text(yaml.safe_dump(defaults))
    if compare_defaults is None:
        compare_defaults = {kind: {"expect_tags": ["NM"]} for kind in COMPARE_KINDS}
    sample = {
        "baseline_tool": "bwa-mem2-upstream",
        "reference": "hg38",
        "source": "data/test/",
        "compare_options": compare_options,
    }
    (config_dir / "samples.yaml").write_text(
        yaml.safe_dump({"compare_defaults": compare_defaults, "samples": {"probe": sample}})
    )


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
        "panel-twist-5M": "panel-twist-5M-fast",
        "hic-1M": "hic-1M-fast",
        "sbx-1M": "sbx-1M-fast",
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
        # Layout + mem_flags must be preserved so fast-vs-default stays
        # apples-to-apples (sbx is single-end; hic carries -5 -S -P).
        assert fast.layout == base.layout, fast_name
        assert fast.mem_flags == base.mem_flags, fast_name
        # The fast arm's flags are the base's flags plus `--fast`, nothing else.
        assert fast.fg_labs_flags == [*base.fg_labs_flags, "--fast"], fast_name
        # is_meth must agree with the (shared) reference — `--fast` never flips it.
        assert fast.is_meth == base.is_meth, fast_name


def test_meth_fast_sibling_keeps_compare_ignore_tags() -> None:
    """The meth `--fast` sibling carries the same bisulfite ignore-tags so its
    vs-default / vs-baseline compares stay apples-to-apples with the default."""
    cfg = load_config(CONFIG_DIR)
    base = cfg.ignore_tags("meth-twist-emseq-5M", "vs_baseline")
    fast = cfg.ignore_tags("meth-twist-emseq-5M-fast", "vs_baseline")
    assert fast == base


def test_meth_samples_share_one_anchored_ignore_list() -> None:
    """All four meth arms resolve to the same vs_baseline list, and the YAML
    anchor holding it is not mistaken for a sample.

    The list is shared via a `meth_vs_baseline` anchor declared beside
    `compare_defaults`. Two things must hold: every meth arm resolves to the
    same tags (so the smoke/fast arms cannot drift from the real ones), and the
    top-level anchor key is inert rather than being loaded as a sample.
    """
    cfg = load_config(CONFIG_DIR)
    meth_samples = [
        "meth-twist-emseq-5M",
        "meth-twist-emseq-5M-fast",
        "smoke-meth",
        "smoke-meth-fast",
    ]
    resolved = {s: cfg.ignore_tags(s, "vs_baseline") for s in meth_samples}
    assert len(set(map(tuple, resolved.values()))) == 1, resolved
    assert "meth_vs_baseline" not in cfg.samples


def test_ignore_tags_extend_rather_than_replace_the_kind_default() -> None:
    """A sample override must ADD to its kind's default, not shadow it.

    The meth samples need the cross-tool default (`MQ`/`HN`) *and* their own
    bisulfite additions; replace-semantics would silently drop the former and
    score 0% concordance.
    """
    cfg = load_config(CONFIG_DIR)
    tags = cfg.ignore_tags("meth-twist-emseq-5M", "vs_baseline")
    assert {"MQ", "HN"}.issubset(tags), "kind default survived the override"
    # NM/MD/XA/SA are, or embed, an edit distance against the converted
    # reference; XM/XG/XR and YD/YC/RG are the two aligners' disjoint tag sets.
    assert {"NM", "MD", "XA", "SA", "XM", "XG", "XR", "YD", "YC", "RG"}.issubset(tags)


def test_same_behaviour_comparisons_skip_nothing() -> None:
    """bwa-mem3 vs bwa-mem3 at the same search settings compares every tag.

    Covers meth samples too: the bisulfite exclusions exist only because bwameth
    computes those tags against a converted reference. Against another bwa-mem3
    they are directly comparable, and excluding them would blind the comparisons
    best placed to catch a tag-only regression.
    """
    cfg = load_config(CONFIG_DIR)
    for sample in ("wgs-5M", "meth-twist-emseq-5M", "sbx-1M"):
        for kind in ("vs_golden", "vs_x86"):
            assert cfg.ignore_tags(sample, kind) == [], f"{sample}/{kind}"


def test_vs_default_skips_the_candidate_set_tags() -> None:
    """`--fast` vs default is same-binary but NOT same-behaviour.

    The preset prunes the candidate set by design, so the tags describing that
    set (XS/XA/SA/HN, plus MQ via MAPQ repair) diverge mechanically and carry no
    placement information — measured at up to +21.8pp of drift, which would
    swamp the MAPQ-stratified placement signal this comparison exists to
    produce. Tags describing the CHOSEN alignment stay strict.
    """
    cfg = load_config(CONFIG_DIR)
    skipped = cfg.ignore_tags("wgs-5M", "vs_default")
    assert set(skipped) == {"XS", "HN", "XA", "SA", "MQ"}
    for kept in ("AS", "MD", "NM", "MC"):
        assert kept not in skipped, f"{kept} describes the chosen alignment; keep it strict"


def test_ignore_tags_rejects_unknown_comparison_kind() -> None:
    cfg = load_config(CONFIG_DIR)
    with pytest.raises(ValueError, match="unknown comparison kind"):
        cfg.ignore_tags("wgs-5M", "vs_nonsense")


def test_retired_flat_ignore_tags_is_rejected(tmp_path: Path) -> None:
    """A config from before the policy was per-kind must fail, not be ignored.

    Silently ignoring `compare_options` is precisely the bug this replaced
    (bench #34): the config read as though tags were filtered while nothing
    ever consulted it.
    """
    _write_minimal_config(tmp_path, compare_options={"ignore_tags": ["YD"]})
    with pytest.raises(ValueError, match="retired flat"):
        load_config(tmp_path)


def test_unknown_compare_options_kind_is_rejected(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path, compare_options={"vs_bogus": {"ignore_tags": ["YD"]}})
    with pytest.raises(ValueError, match="unknown comparison kind"):
        load_config(tmp_path)


@pytest.mark.parametrize("options", [["vs_baseline"], "vs_baseline", 3])
def test_non_mapping_compare_options_is_rejected_clearly(tmp_path: Path, options: object) -> None:
    """A mistyped `compare_options` must fail like every other malformed config:
    a `ValueError` naming the sample and the key.

    Without an explicit guard these reach `dict(...)`, which raises a `ValueError`
    about a "dictionary update sequence" -- naming neither the sample nor the
    key -- or, for a non-iterable, a `TypeError`, which is not even the type this
    loader documents itself as raising.
    """
    _write_minimal_config(tmp_path, compare_options=options)
    with pytest.raises(ValueError, match="compare_options"):
        load_config(tmp_path)


def test_absent_compare_options_is_not_an_error(tmp_path: Path) -> None:
    """A bare `compare_options:` header parses to `None`, which is exactly what
    `data.get(...)` yields for a sample that omits the key entirely -- the common
    case. The two are indistinguishable here, so `None` must mean "no overrides"
    rather than an error, matching `_validate_compare_defaults`.
    """
    _write_minimal_config(tmp_path, compare_options=None)
    cfg = load_config(tmp_path)
    assert cfg.samples["probe"].compare_options == {}


def test_unknown_compare_defaults_kind_is_rejected(tmp_path: Path) -> None:
    """A typo'd kind in the top-level block would silently apply to nothing."""
    _write_minimal_config(
        tmp_path, compare_options={}, compare_defaults={"vs_bogus": {"ignore_tags": ["YD"]}}
    )
    with pytest.raises(ValueError, match="unknown comparison kind"):
        load_config(tmp_path)


def test_compare_defaults_kind_body_must_be_a_mapping(tmp_path: Path) -> None:
    """`vs_baseline: [MQ]` is the natural mistake; it must not parse as a policy."""
    _write_minimal_config(tmp_path, compare_options={}, compare_defaults={"vs_baseline": ["MQ"]})
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(tmp_path)


def test_compare_defaults_ignore_tags_must_be_a_list_of_strings(tmp_path: Path) -> None:
    """A bare scalar would otherwise iterate per character into bogus tags."""
    _write_minimal_config(
        tmp_path, compare_options={}, compare_defaults={"vs_baseline": {"ignore_tags": "MQ"}}
    )
    with pytest.raises(ValueError, match="ignore_tags"):
        load_config(tmp_path)


def test_compare_defaults_must_be_a_mapping(tmp_path: Path) -> None:
    """The block itself, not just each kind's body, has to be a mapping."""
    _write_minimal_config(tmp_path, compare_options={}, compare_defaults=["vs_baseline"])
    with pytest.raises(ValueError, match="`compare_defaults` must be a mapping"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Tag-set guard: expect_tags / absent_ok_tags resolution
# ---------------------------------------------------------------------------


def test_every_comparison_kind_declares_a_non_empty_allowlist() -> None:
    """compare-bams skips its unexpected-tag check when handed no allowlist, so
    an empty `expect_tags` would leave the guard silently inert — the exact
    failure shape (config that does nothing) the guard exists to reject."""
    cfg = load_config(CONFIG_DIR)
    for kind in COMPARE_KINDS:
        assert cfg.expect_tags("wgs-5M", kind), f"{kind} has no expect_tags"


def test_missing_expect_tags_is_rejected_at_load(tmp_path: Path) -> None:
    _write_minimal_config(
        tmp_path,
        compare_options={},
        compare_defaults={kind: {"ignore_tags": ["MQ"]} for kind in COMPARE_KINDS},
    )
    with pytest.raises(ValueError, match="non-empty `expect_tags`"):
        load_config(tmp_path)


def test_expect_tags_extend_rather_than_replace_the_kind_default(tmp_path: Path) -> None:
    """Same extend-not-replace invariant `ignore_tags` relies on: a per-sample
    addition must not drop the kind's default out from under it."""
    _write_minimal_config(
        tmp_path,
        compare_options={"vs_baseline": {"expect_tags": ["ZZ"]}},
        compare_defaults={kind: {"expect_tags": ["NM", "MD"]} for kind in COMPARE_KINDS},
    )
    cfg = load_config(tmp_path)
    assert cfg.expect_tags("probe", "vs_baseline") == ["MD", "NM", "ZZ"]


def test_meth_samples_get_the_bisulfite_tags_without_declaring_them() -> None:
    """One fact about bisulfite alignment, derived rather than restated across
    ~10 meth samples x 3 comparison kinds."""
    cfg = load_config(CONFIG_DIR)
    for kind in ("vs_baseline", "vs_golden", "vs_default"):
        expected = set(cfg.expect_tags("meth-twist-emseq-5M", kind))
        assert expected >= METH_EXTRA_TAGS, f"{kind} missing bisulfite tags"
    # ...and non-meth samples do not get them.
    assert not (METH_EXTRA_TAGS & set(cfg.expect_tags("wgs-5M", "vs_baseline")))


def test_single_end_samples_excuse_the_mate_tags_from_the_audit() -> None:
    """`sbx-1M` is single-end, so MQ cannot exist and `vs_baseline`'s MQ ignore
    entry would otherwise read as dead config."""
    cfg = load_config(CONFIG_DIR)
    assert cfg.samples["sbx-1M"].layout == "single"
    assert "MQ" in cfg.absent_ok_tags("sbx-1M", "vs_baseline")
    # Paired-end samples emit MQ, so nothing is excused there.
    assert cfg.absent_ok_tags("wgs-5M", "vs_baseline") == []


def test_meth_samples_excuse_mq_and_hn_from_the_audit() -> None:
    """fg-labs/bwa-mem3#296: neither side emits MQ or HN under `--meth`, so both
    `vs_baseline` ignore entries match no record."""
    cfg = load_config(CONFIG_DIR)
    assert set(cfg.absent_ok_tags("meth-twist-emseq-5M", "vs_baseline")) == METH_UNEMITTED_TAGS


def test_absent_ok_tags_never_exceeds_the_ignore_list() -> None:
    """Excusing a tag that is not ignored would itself be inert config: only
    ignore entries are ever audited. `vs_golden` ignores nothing, so nothing can
    be excused there even though the sample is single-end and meth-adjacent."""
    cfg = load_config(CONFIG_DIR)
    for sample in ("sbx-1M", "meth-twist-emseq-5M"):
        for kind in COMPARE_KINDS:
            excused = set(cfg.absent_ok_tags(sample, kind))
            ignored = set(cfg.ignore_tags(sample, kind))
            assert excused <= ignored, f"{sample}/{kind}: {excused - ignored} not ignored"
    assert cfg.absent_ok_tags("sbx-1M", "vs_golden") == []


def test_expect_tags_rejects_unknown_comparison_kind() -> None:
    cfg = load_config(CONFIG_DIR)
    for resolver in (cfg.expect_tags, cfg.absent_ok_tags):
        with pytest.raises(ValueError, match="unknown comparison kind"):
            resolver("wgs-5M", "vs_nonsense")


def test_shipped_allowlist_covers_every_tag_the_shipped_policy_ignores() -> None:
    """An ignored tag absent from `expect_tags` is fine for the guard (ignore
    membership alone anticipates it), but it means the two lists disagree about
    what this comparison emits. Keep them consistent for the non-derived tags."""
    cfg = load_config(CONFIG_DIR)
    for kind in COMPARE_KINDS:
        ignored = set(cfg.ignore_tags("wgs-5M", kind))
        expected = set(cfg.expect_tags("wgs-5M", kind))
        assert ignored <= expected, f"{kind}: {ignored - expected} ignored but not in expect_tags"


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


def test_as_positive_int_accepts_an_int() -> None:
    assert _as_positive_int("ladder entry", "threads", 16) == EXPECTED_THREADS


@pytest.mark.parametrize(
    "value",
    [
        16.9,  # would silently TRUNCATE to 16 under int()
        "16",  # a quoted YAML scalar
        True,  # bool subclasses int; int(True) == 1
        0,  # a rung of zero threads is not a rung
        -1,
        None,
    ],
)
def test_as_positive_int_rejects_anything_int_like(value: object) -> None:
    """The point of the loader is to fail loudly, not to coerce."""
    with pytest.raises(ValueError, match="must be an integer >= 1"):
        _as_positive_int("ladder entry", "threads", value)


def _thread_scaling_yaml(**overrides: object) -> dict[str, object]:
    """A minimal valid `thread_scaling` block, with keys overridable per test."""
    return {
        "sample": "wgs-5M",
        "arch": "c8g64",
        "ladder": [{"threads": 1, "reps": 1}, {"threads": 16, "reps": 3}],
        "max_efficiency_drop_pp": 3.0,
        **overrides,
    }


def _thread_scaling(**overrides: object) -> ThreadScaling:
    cfg = load_config(CONFIG_DIR)
    return _thread_scaling_from(
        _thread_scaling_yaml(**overrides), samples=cfg.samples, archs=cfg.archs
    )


def test_thread_scaling_accepts_a_well_formed_ladder() -> None:
    scaling = _thread_scaling()
    assert [(step.threads, step.reps) for step in scaling.ladder] == [(1, 1), (16, 3)]
    assert scaling.max_threads == EXPECTED_THREADS


@pytest.mark.parametrize(
    "ladder",
    [
        [{"threads": 1, "reps": 1}, {"threads": 16.9, "reps": 3}],  # truncates to 16
        [{"threads": 1, "reps": 1}, {"threads": 16, "reps": True}],  # bool -> 1 rep
        [{"threads": 1, "reps": 1}, {"threads": "16", "reps": 3}],  # quoted scalar
        [{"threads": 1, "reps": 0}],  # zero reps measures nothing
    ],
)
def test_thread_scaling_rejects_non_integer_ladder_values(ladder: object) -> None:
    """A mistyped rung must fail at load time, not an hour into a spot job."""
    with pytest.raises(ValueError, match="must be an integer >= 1"):
        _thread_scaling(ladder=ladder)


@pytest.mark.parametrize("drop", [True, "3.0", -1.0, float("nan"), float("inf")])
def test_thread_scaling_rejects_a_non_numeric_tolerance(drop: object) -> None:
    """The tolerance gates releases; a coerced value gates against the wrong number.

    `nan` and `inf` are numeric and not `< 0`, so a plain range check lets them
    through: `nan` makes every comparison false (the gate never fires) and `inf`
    makes it unbounded (the gate can never fail). Both silently disable Gate #3.
    """
    with pytest.raises(ValueError, match="max_efficiency_drop_pp"):
        _thread_scaling(max_efficiency_drop_pp=drop)


def test_parse_ladder_override_parses_and_sorts_rungs() -> None:
    steps = parse_ladder_override("64:3, 16:3,32:3")
    assert [(step.threads, step.reps) for step in steps] == [(16, 3), (32, 3), (64, 3)]


def test_parse_ladder_override_allows_omitting_the_one_thread_rung() -> None:
    """Skipping T(1) is the whole point of the override; the gate no-ops instead."""
    assert [step.threads for step in parse_ladder_override("16:3,64:3")] == [16, 64]


@pytest.mark.parametrize(
    "spec",
    [
        "16",  # missing `:reps`
        "16:",  # empty reps
        "sixteen:3",
        "16.9:3",  # would truncate
        "16:3:1",  # too many fields
        "0:3",  # zero threads
        "16:0",  # zero reps
        "-16:3",
        "",  # no rungs at all
        ",",
        "16:3,16:1",  # duplicate thread count
    ],
)
def test_parse_ladder_override_rejects_a_malformed_spec(spec: str) -> None:
    """These tokens are pasted into the rule's shell loop; reject them up front."""
    with pytest.raises(ValueError, match="ladder override"):
        parse_ladder_override(spec)
