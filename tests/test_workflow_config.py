"""Unit tests for workflow_config loader."""

from pathlib import Path

import pytest

from bwa_mem3_bench.workflow_config import (
    Arch,
    WorkflowConfig,
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
    a perf win on this workload (see Phase C report at
    ~/work/git/bwa-mem3/avx512-baseline-build/PHASE_C_REPORT.md). When
    upstream lands a fix, set c7a / c7i / m7i back to "avx512bw" here.
    """
    cfg = load_config(CONFIG_DIR)
    for arch in ("c6a", "c7a", "c7i", "c7g", "c8g", "m7i"):
        assert cfg.archs[arch].baseline_arch == "", (
            f"{arch}.baseline_arch should be parked at ''; got "
            f"{cfg.archs[arch].baseline_arch!r}"
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
