"""Tests for `bwa_mem3_bench.render_profile`."""

import dataclasses
from pathlib import Path

import pytest

from bwa_mem3_bench import aws_config, render_profile

_TEMPLATE = "image: ${ECR_REPO_URI}:latest\nregion: ${AWS_REGION}\n${COST_CENTER_LINE}"


def _no_ecr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `aws_config.load()` report no ECR repository (a fresh clone, no env)."""
    real = aws_config.load()
    monkeypatch.setattr(
        render_profile.aws_config,
        "load",
        lambda: dataclasses.replace(real, ecr_repo_uri="", bucket="some-bucket"),
    )


def test_explicit_ecr_repo_uri_renders_without_cdk_outputs_or_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--ecr-repo-uri` is the same override the Snakefile takes as `ecr_repo_uri`."""
    _no_ecr(monkeypatch)
    template = tmp_path / "t.yaml.template"
    template.write_text(_TEMPLATE)
    output = tmp_path / "aws-batch" / "config.yaml"
    render_profile.render_profile(
        template=template, output=output, ecr_repo_uri="123.dkr.ecr/bench"
    )
    assert output.read_text().startswith("image: 123.dkr.ecr/bench:latest\n")


def test_missing_ecr_repo_uri_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_ecr(monkeypatch)
    template = tmp_path / "t.yaml.template"
    template.write_text(_TEMPLATE)
    with pytest.raises(RuntimeError, match="ECR repository URI is not set"):
        render_profile.render_profile(template=template, output=tmp_path / "config.yaml")
