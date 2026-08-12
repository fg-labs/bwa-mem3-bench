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
import io
import re
import tomllib
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from bwa_mem3_bench import REPO_ROOT

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


@pytest.mark.parametrize("machine", ["", "i386", "ppc64le", "s390x"])
def test_native_platform_refuses_an_unmappable_machine(machine: str) -> None:
    """`platform.machine()` returns "" when it cannot determine the value.

    Forwarding it produced `--platform linux/`, and buildx's own error names
    neither the empty value nor where it came from. An unmapped-but-real arch is
    the same problem: the fleet has no such workers, so building for it is a
    silent waste rather than a useful result.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module.platform, "machine", lambda: machine)
        with pytest.raises(ValueError, match="cannot map this host's architecture"):
            build_module._native_platform()
    finally:
        monkeypatch.undo()


def test_an_explicit_platform_rescues_an_unmappable_host() -> None:
    """`--platforms` must work on a host whose arch cannot be mapped.

    That escape hatch is exactly what `_native_platform()`'s error recommends, so
    it has to keep working on the host that triggers the error. Resolving the
    default eagerly -- `x if push else _native_platform()` evaluated before the
    override is consulted -- raised regardless, making the advice impossible to
    follow.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module.platform, "machine", lambda: "s390x")
        assert _platform_arg(platforms="linux/amd64") == "linux/amd64"
        assert _platform_arg(platforms="linux/amd64", push=True) == "linux/amd64"
    finally:
        monkeypatch.undo()


def test_fleet_platforms_covers_every_arch_the_fleet_runs() -> None:
    """A pushed image must carry a platform for every arch that will pull it.

    `FLEET_PLATFORMS` restates what `config/archs.yaml` already declares per
    arch. Adding an arch on a third platform without updating the constant would
    push a manifest list its workers cannot select from -- discovered at pull
    time on a Batch host, not here.
    """
    archs = yaml.safe_load((REPO_ROOT / "config" / "archs.yaml").read_text())["archs"]
    declared = {spec["platform"] for spec in archs.values()}
    assert set(build_module.FLEET_PLATFORMS.split(",")) == declared, (
        "FLEET_PLATFORMS and config/archs.yaml disagree about which platforms "
        "the fleet runs; a pushed image would not cover every worker"
    )


def _builder_warning(
    driver: str, *, name: str = "bwa-mem3-bench-builder", returncode: int = 0, timeout: bool = False
) -> str:
    """Return what `_warn_if_builder_is_not_gc_bounded` writes to stderr.

    Calls the probe DIRECTLY rather than through `build()`. Routing it through
    the real `build()` needs `dry_run=False` for the probe to run at all, which
    also trips build()'s vendored-minibwa precondition -- passing on a developer
    checkout with submodules and failing in CI, which does not fetch them. The
    unit under test is the probe.
    """

    class _Probe:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = f"{name} {driver}"

    def _run(*_a: object, **_k: object) -> _Probe:
        if timeout:
            raise build_module.subprocess.TimeoutExpired(cmd="docker", timeout=1)
        return _Probe()

    monkeypatch = pytest.MonkeyPatch()
    stderr = io.StringIO()
    try:
        monkeypatch.setattr(build_module.subprocess, "run", _run)
        monkeypatch.setattr(build_module.sys, "stderr", stderr)
        build_module._warn_if_builder_is_not_gc_bounded(dry_run=False)
    finally:
        monkeypatch.undo()
    return stderr.getvalue()


def test_a_builder_that_ignores_the_gc_ceiling_warns() -> None:
    """The cache bound is this PR's whole point and binds to ONE driver.

    On any other builder -- the daemon's default, or anything selected via
    BUILDX_BUILDER -- `docker/buildkitd.toml` is silently ignored and the cache
    grows exactly as it did before. That has no symptom until the disk fills,
    which is how it reached ~95 GB unnoticed.
    """
    warning = _builder_warning("docker")
    assert "may NOT apply" in warning
    assert "'docker' driver" in warning, "the warning must name the driver it found"
    assert "docker-builder-create" in warning, "the warning must name the remedy"


def test_the_gc_bounded_builder_does_not_warn() -> None:
    """The warning must not cry wolf on the builder it exists to recommend."""
    assert _builder_warning(build_module.GC_BOUND_BUILDX_DRIVER) == ""


def test_a_failed_driver_probe_stays_quiet() -> None:
    """No daemon, or no such builder — the build is about to say so far better."""
    assert _builder_warning("", returncode=1) == ""


def test_a_right_driver_builder_of_unknown_provenance_still_warns() -> None:
    """The driver alone does not prove the ceiling applies.

    buildx does not report which config file a builder was created with, and a
    hand-rolled `docker-container` builder has the right driver with no
    `--config` -- so its cache is unbounded exactly like the default builder's.
    Assuming otherwise would defeat the check on the one builder shape most
    likely to be mistaken for the right one.
    """
    warning = _builder_warning(build_module.GC_BOUND_BUILDX_DRIVER, name="some-other-builder")
    assert "may NOT apply" in warning
    assert "some-other-builder" in warning


def test_an_unresponsive_docker_does_not_stall_the_build() -> None:
    """This probe only prints a warning; it must never delay the build itself."""
    assert _builder_warning("docker", timeout=True) == ""


def test_the_probe_is_bounded_by_a_timeout() -> None:
    """A hung daemon would otherwise block indefinitely before the build starts."""
    captured: dict[str, object] = {}

    class _Probe:
        returncode = 0
        stdout = f"{build_module.GC_BOUND_BUILDX_BUILDER} {build_module.GC_BOUND_BUILDX_DRIVER}"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            build_module.subprocess,
            "run",
            lambda *a, **k: (captured.update(k), _Probe())[1],
        )
        build_module._warn_if_builder_is_not_gc_bounded(dry_run=False)
    finally:
        monkeypatch.undo()
    assert captured.get("timeout"), "the advisory probe must pass a timeout"


def test_the_probe_is_skipped_entirely_under_dry_run() -> None:
    """`--dry-run` must not require a live docker daemon."""

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("subprocess.run called under dry_run")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module.subprocess, "run", _explode)
        build_module._warn_if_builder_is_not_gc_bounded(dry_run=True)
    finally:
        monkeypatch.undo()


def test_the_prune_task_targets_the_builder_the_create_task_makes() -> None:
    """pixi tasks are plain strings with no interpolation, so the name is repeated.

    `docker buildx prune --builder <name>` ERRORS on a nonexistent builder rather
    than no-opping, so a rename that updates only one task turns the documented
    cleanup command into a hard failure.
    """
    tasks = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())["tasks"]
    create = tasks["docker-builder-create"]
    prune = tasks["docker-prune"]
    created = re.search(r"--name (\S+)", create)
    pruned = re.search(r"--builder (\S+)", prune)
    assert created and pruned, f"could not parse builder names from {create!r} / {prune!r}"
    assert created.group(1) == pruned.group(1), (
        f"docker-builder-create makes {created.group(1)!r} but docker-prune targets "
        f"{pruned.group(1)!r}; the prune task would error instead of pruning"
    )
