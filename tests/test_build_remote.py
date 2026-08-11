"""Tests for `build-remote` — Batch image builds on arch-native hardware.

The point of this path is that neither architecture is emulated. Most of what can
go wrong is a routing or plumbing mistake that still produces a *correct image*,
just slowly or from the wrong inputs, so the assertions here are about where jobs
go and what they are told.
"""

from __future__ import annotations

import importlib
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from bwa_mem3_bench import REPO_ROOT

# `bwa_mem3_bench.commands.build_remote` the ATTRIBUTE is the re-exported
# function, not the module, so `from ... import build_remote` binds the wrong
# object. Third place in this repo where that bites -- see `test_compat_arm.py`.
remote = importlib.import_module("bwa_mem3_bench.commands.build_remote")

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "batch-image-build.sh"


def test_every_fleet_platform_has_a_native_queue() -> None:
    """Both arches run in the suite, so both need somewhere native to build."""
    assert set(remote.NATIVE_QUEUE_ARCH_BY_PLATFORM) == {"linux/amd64", "linux/arm64"}
    assert set(remote.ARCH_TAG_SUFFIX) == set(remote.NATIVE_QUEUE_ARCH_BY_PLATFORM)


def test_queue_arch_matches_the_platform_it_claims_to_build() -> None:
    """Routing amd64 to a Graviton queue would silently emulate — the one thing to avoid.

    Asserted against `config/archs.yaml`, so re-pointing a queue at a different
    instance type fails here rather than producing a quietly-emulated build.
    """
    archs = yaml.safe_load((REPO_ROOT / "config" / "archs.yaml").read_text())["archs"]
    for platform, arch_key in remote.NATIVE_QUEUE_ARCH_BY_PLATFORM.items():
        assert archs[arch_key]["platform"] == platform, (
            f"build-remote sends {platform} to the {arch_key} queue, but "
            f"config/archs.yaml says {arch_key} is {archs[arch_key]['platform']}"
        )


def test_context_tarball_ships_minibwa_but_not_the_junk(tmp_path: Path) -> None:
    """lh3/minibwa is private and cannot be cloned in the build, so it must travel.

    The mirror-image risk is shipping `data/` or a `target/` dir and turning a
    small context upload into a multi-GB one.
    """
    tarball = remote.build_context_tarball(tmp_path / "context.tar.gz")
    with tarfile.open(tarball) as tar:
        names = tar.getnames()

    assert any(name.startswith("vendor/minibwa") for name in names), (
        "vendor/minibwa missing from the context; the build cannot clone it"
    )
    assert any(name == "docker/Dockerfile" for name in names)
    assert any(name == "docker/Dockerfile.base" for name in names)
    for excluded in (".git", ".pixi", "data", "runs", "target"):
        assert not any(name == excluded or name.startswith(f"{excluded}/") for name in names), (
            f"{excluded} leaked into the build context"
        )


def test_manifest_list_sources_are_the_per_arch_tags() -> None:
    """The join has to name the tags the jobs actually pushed."""
    sources = remote._manifest_list_sources("repo", "abc123", ["linux/amd64", "linux/arm64"])
    assert sources == ["repo:abc123-amd64", "repo:abc123-arm64"]


def _submitted_command(**kwargs: object) -> list[str]:
    """Return the `aws batch submit-job` argv for one platform, without submitting."""
    captured: list[list[str]] = []

    defaults: dict[str, object] = {
        "job_queue": "bwa-mem3-bench-c6a",
        "job_definition": "bwa-mem3-bench-image-build",
        "job_name": "build-abc-amd64",
        "context_s3_uri": "s3://bucket/image-builds/abc/context.tar.gz",
        "ecr_repo": "123.dkr.ecr.us-east-1.amazonaws.com/bwa-mem3-bench",
        "image_tag": "abc-amd64",
        "dockerfile": "docker/Dockerfile",
        "platform": "linux/amd64",
        "region": "us-east-1",
        "build_args": {"FG_LABS_SHA": "abc"},
        "dry_run": False,
    }
    defaults.update(kwargs)

    monkeypatch = pytest.MonkeyPatch()

    class _Result:
        stdout = json.dumps({"jobId": "job-1"})

    try:
        monkeypatch.setattr(
            remote.subprocess,
            "run",
            lambda cmd, **_: captured.append(cmd) or _Result(),  # type: ignore[func-returns-value]
        )
        remote._submit_build_job(**defaults)  # type: ignore[arg-type]
    finally:
        monkeypatch.undo()

    assert captured, "no command submitted"
    return captured[0]


def test_submitted_job_carries_the_platform_and_target() -> None:
    """The job script refuses to build if PLATFORM and the host arch disagree.

    That guard is only useful if PLATFORM actually reaches the container.
    """
    cmd = _submitted_command()
    overrides = json.loads(cmd[cmd.index("--container-overrides") + 1])
    env = {entry["name"]: entry["value"] for entry in overrides["environment"]}

    assert env["PLATFORM"] == "linux/amd64"
    assert env["IMAGE_TAG"] == "abc-amd64"
    assert env["DOCKERFILE"] == "docker/Dockerfile"
    assert env["CONTEXT_S3_URI"].endswith("context.tar.gz")
    assert "--job-queue" in cmd
    assert cmd[cmd.index("--job-queue") + 1] == "bwa-mem3-bench-c6a"


def test_build_args_survive_as_newline_separated_pairs() -> None:
    """Pairs go over as one env var so the submitter stays the single source of truth."""
    cmd = _submitted_command(build_args={"A": "1", "B": "two words"})
    overrides = json.loads(cmd[cmd.index("--container-overrides") + 1])
    env = {entry["name"]: entry["value"] for entry in overrides["environment"]}
    assert env["BUILD_ARGS"] == "A=1\nB=two words"


def test_bootstrap_command_fetches_the_script_from_beside_the_context() -> None:
    """The dind image has no aws CLI, so the command has to install one first."""
    cmd = _submitted_command()
    overrides = json.loads(cmd[cmd.index("--container-overrides") + 1])
    bootstrap = " ".join(overrides["command"])
    assert "aws-cli" in bootstrap
    assert "batch-image-build.sh" in bootstrap
    assert "CONTEXT_S3_URI" in bootstrap


def test_build_remote_requires_exactly_one_of_sha_or_base() -> None:
    """Neither is a no-op and both is ambiguous; fail before touching S3."""
    with pytest.raises(ValueError, match="exactly one"):
        remote.build_remote()
    with pytest.raises(ValueError, match="exactly one"):
        remote.build_remote(fg_labs_sha="abc", base=True)


def test_the_job_script_refuses_to_build_the_wrong_architecture() -> None:
    """The guard that makes a mis-routed queue loud instead of silently slow.

    Without it, a queue wired to the wrong compute environment would fall back to
    emulation and still succeed -- producing a correct image while removing the
    entire reason this path exists.
    """
    script = SCRIPT.read_text()
    assert "uname -m" in script
    assert "x86_64" in script and "aarch64" in script
    assert "emulation" in script, "the failure message should say why this matters"


def test_the_job_script_waits_for_dockerd_before_building() -> None:
    """dind's entrypoint is bypassed because Batch overrides the command."""
    script = SCRIPT.read_text()
    assert "dockerd" in script
    assert "docker info" in script, "must poll for readiness, not sleep and hope"
