"""Unit tests for workflow_config loader."""

from pathlib import Path

import pytest

from bwa_mem3_bench.workflow_config import (
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
    cfg = load_config(CONFIG_DIR)
    # AVX-512BW hosts get the avx512bw baseline floor (fg-labs PR #84).
    assert cfg.archs["c7a"].baseline_arch == "avx512bw"
    assert cfg.archs["c7i"].baseline_arch == "avx512bw"
    assert cfg.archs["m7i"].baseline_arch == "avx512bw"
    # c6a (AVX2-only) uses the portable tag — AVX2 is the upstream default,
    # so the bare `<sha>` build already ships an AVX2-baselined binary.
    assert cfg.archs["c6a"].baseline_arch == ""
    # ARM archs ignore the field; empty string = no override.
    assert cfg.archs["c7g"].baseline_arch == ""
    assert cfg.archs["c8g"].baseline_arch == ""


_TEST_ECR = "550079046206.dkr.ecr.us-east-1.amazonaws.com/bwa-mem3-bench"
_TEST_SHA = "abcdef0"


def test_arch_image_uri_avx2_host_uses_portable_tag() -> None:
    """c6a (AVX2-only) uses the bare `<sha>` portable tag — AVX2 is the
    upstream BASELINE_ARCH default, so building a separate `<sha>-avx2`
    image would just duplicate content already in `<sha>`."""
    cfg = load_config(CONFIG_DIR)
    uri = cfg.archs["c6a"].image_uri(ecr_repo_uri=_TEST_ECR, fg_labs_sha=_TEST_SHA)
    assert uri == f"{_TEST_ECR}:{_TEST_SHA}"
    assert "-" not in uri.split(":")[-1]


def test_arch_image_uri_avx512_hosts_use_avx512bw_suffix() -> None:
    cfg = load_config(CONFIG_DIR)
    for arch in ("c7a", "c7i", "m7i"):
        uri = cfg.archs[arch].image_uri(ecr_repo_uri=_TEST_ECR, fg_labs_sha=_TEST_SHA)
        assert uri == f"{_TEST_ECR}:{_TEST_SHA}-avx512bw", f"{arch}: {uri}"


def test_arch_image_uri_arm_hosts_use_no_suffix() -> None:
    """ARM archs have empty baseline_arch -> bare <sha> tag (multi-arch
    manifest list, no host-locking)."""
    cfg = load_config(CONFIG_DIR)
    for arch in ("c7g", "c8g"):
        uri = cfg.archs[arch].image_uri(ecr_repo_uri=_TEST_ECR, fg_labs_sha=_TEST_SHA)
        assert uri == f"{_TEST_ECR}:{_TEST_SHA}", f"{arch}: {uri}"
        # No dash in the tag (would be a tier suffix).
        assert "-" not in uri.split(":")[-1], f"{arch} unexpected suffix in {uri}"


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
