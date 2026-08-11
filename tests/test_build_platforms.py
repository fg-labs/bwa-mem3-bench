"""Tests for how `cli build` chooses the `--platform` value it passes to buildx.

A multi-arch build is only useful for an image that will actually be pulled by
both x86 and Graviton workers, i.e. one that gets pushed to ECR as a manifest
list. A `docker buildx build` with neither `--push` nor `--load` writes its
result to the build cache and nowhere else, so defaulting to the full fleet
matrix for a local build deposits two complete sets of compile layers that
nothing will ever consume. Doing that once per benchmarked SHA is what grew the
builder's cache into the tens of GB.

Asserted against the COMMAND `build()` generates, captured by monkeypatching
`run_cmd` -- the same approach as `test_compat_arm.py`, and for the same reason:
grepping the source would be satisfied by a comment or a dead branch.
"""

from __future__ import annotations

import importlib
from itertools import pairwise
from pathlib import Path

import pytest

# `bwa_mem3_bench.commands.build` the ATTRIBUTE is the re-exported function, not
# the module, so `from ... import build as build_module` binds the wrong object.
# Fetch the module itself -- same trap as in `test_compat_arm.py`.
build_module = importlib.import_module("bwa_mem3_bench.commands.build")


def _platform_arg(**build_kwargs: object) -> str:
    """Run `build()` with `run_cmd` stubbed and return the `--platform` value.

    :param build_kwargs: forwarded to `build()`, on top of a dry-run default.
    :return: the single argument following `--platform` in the buildx command.
    """
    captured: list[list[str]] = []

    def _capture(cmd: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:  # noqa: ARG001
        captured.append(cmd)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "run_cmd", _capture)
        monkeypatch.setattr(build_module, "_ecr_login", lambda image_name, *, dry_run: None)
        build_module.build(
            fg_labs_sha="0" * 40,
            image_name="test",
            dry_run=True,
            **build_kwargs,  # type: ignore[arg-type]
        )
    finally:
        monkeypatch.undo()

    buildx = next((c for c in captured if "buildx" in c), None)
    assert buildx is not None, f"no buildx command generated; captured {captured}"
    platforms = [arg for flag, arg in pairwise(buildx) if flag == "--platform"]
    assert len(platforms) == 1, f"expected exactly one --platform, got {platforms}"
    return platforms[0]


def test_local_build_is_single_arch() -> None:
    """A build that is neither pushed nor loaded must not fan out across the fleet."""
    assert _platform_arg() == build_module._native_platform()


def test_load_build_is_single_arch() -> None:
    """`--load` cannot accept a manifest list at all, so it must stay single-arch."""
    assert _platform_arg(load=True) == build_module._native_platform()


def test_push_build_covers_the_whole_fleet() -> None:
    """A pushed image is pulled by both x86 and Graviton workers, so it needs both."""
    assert _platform_arg(push=True) == build_module.FLEET_PLATFORMS
    assert "linux/amd64" in build_module.FLEET_PLATFORMS
    assert "linux/arm64" in build_module.FLEET_PLATFORMS


def test_explicit_platforms_override_both_defaults() -> None:
    """An explicit `--platforms` wins whether or not the image is being pushed."""
    assert _platform_arg(platforms="linux/riscv64") == "linux/riscv64"
    assert _platform_arg(platforms="linux/riscv64", push=True) == "linux/riscv64"


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "linux/amd64"),
        ("AMD64", "linux/amd64"),
        ("aarch64", "linux/arm64"),
        ("arm64", "linux/arm64"),
    ],
)
def test_native_platform_maps_machine_names_to_oci_arches(machine: str, expected: str) -> None:
    """`platform.machine()` spellings differ by OS; buildx only accepts the OCI names."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module.platform, "machine", lambda: machine)
        assert build_module._native_platform() == expected
    finally:
        monkeypatch.undo()


def test_load_rejects_multiple_platforms() -> None:
    """`--load` cannot export a manifest list, so the combination must fail fast.

    The docker exporter writes into the local daemon's image store, which has no
    concept of a manifest list. buildx does reject this, but only after the build
    has run -- so an unguarded call compiles both architectures and then throws
    the result away. Caught here instead, with a message naming the alternative.
    """
    with pytest.raises(ValueError, match="cannot export multiple platforms"):
        build_module.build(
            fg_labs_sha="0" * 40,
            image_name="test",
            platforms="linux/amd64,linux/arm64",
            load=True,
            dry_run=True,
        )


def test_load_still_accepts_a_single_explicit_platform() -> None:
    """The guard must not break the case it exists to steer people toward."""
    assert _platform_arg(platforms="linux/arm64", load=True) == "linux/arm64"
