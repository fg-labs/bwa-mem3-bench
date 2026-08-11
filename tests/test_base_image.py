"""Tests for the builder base image's identity, tagging, and build command.

The base image carries everything ``FG_LABS_SHA`` does not invalidate. Its whole
value depends on one property: the tag must change whenever the contents would.
If it doesn't, a pin bump silently reuses a base built from the old pins and the
benchmark attributes the difference to bwa-mem3 -- a wrong *result*, not a build
failure, which is the expensive kind.
"""

from __future__ import annotations

import importlib
import re
from itertools import pairwise
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.base_image import (
    BASE_PIN_NAMES,
    base_dockerfile,
    base_image_tag,
    base_image_uri,
    base_pins,
)

# `bwa_mem3_bench.commands.build` the ATTRIBUTE is the re-exported function, not
# the module -- same trap as in `test_compat_arm.py`.
build_module = importlib.import_module("bwa_mem3_bench.commands.build")


def test_the_base_dockerfile_exists() -> None:
    """The recipe the tag is hashed over has to be there for any of this to work."""
    assert base_dockerfile().is_file(), f"missing {base_dockerfile()}"


def test_every_base_pin_resolves() -> None:
    """A pin named here but absent from build-arg-defaults.env would hash as a crash."""
    pins = base_pins()
    assert set(pins) == set(BASE_PIN_NAMES)
    assert all(value for value in pins.values()), f"empty pin value in {pins}"


def test_tag_is_stable_for_identical_inputs() -> None:
    """Same recipe + same pins must produce the same tag, or nothing is ever reused."""
    assert base_image_tag() == base_image_tag()


def test_tag_is_prefixed_by_the_upstream_tag() -> None:
    """The prefix is what makes the tag mean something at a glance in ECR."""
    tag = base_image_tag()
    assert tag.startswith(f"{base_pins()['UPSTREAM_TAG']}-")
    assert re.fullmatch(r".+-[0-9a-f]{12}", tag), tag


@pytest.mark.parametrize("pin_name", BASE_PIN_NAMES)
def test_changing_any_pin_changes_the_tag(pin_name: str) -> None:
    """This is the correctness property the whole split rests on.

    Keying the base on ``UPSTREAM_TAG`` alone was the obvious first design and
    is wrong: bumping ``HOLODECK_REF`` while ``UPSTREAM_TAG`` stayed at v2.2.1
    would reuse a base built from the old holodeck.
    """
    pins = base_pins()
    bumped = dict(pins) | {pin_name: f"{pins[pin_name]}-changed"}
    assert base_image_tag(pins=bumped) != base_image_tag(pins=pins)


def test_editing_the_recipe_changes_the_tag(tmp_path: Path) -> None:
    """Pins alone don't describe the base -- LLVM_VERSION and the Rust pin live in the file."""
    original = base_dockerfile().read_bytes()
    edited = tmp_path / "Dockerfile.base"
    edited.write_bytes(original + b"\nRUN echo changed\n")
    assert base_image_tag(dockerfile=edited) != base_image_tag()


def test_base_image_uri_targets_a_sibling_repository() -> None:
    """The base must NOT share the benchmark repo -- its lifecycle rule would reap it."""
    uri = base_image_uri("123.dkr.ecr.us-east-1.amazonaws.com/bwa-mem3-bench", tag="v2.2.1-abc")
    assert uri == "123.dkr.ecr.us-east-1.amazonaws.com/bwa-mem3-bench-base:v2.2.1-abc"


def _captured_build_command(fn_name: str, **kwargs: object) -> list[str]:
    """Run a build entry point with `run_cmd` stubbed and return the buildx command.

    :param fn_name: ``build`` or ``build_base``.
    :param kwargs: forwarded to the function.
    :return: the generated buildx argv.
    """
    captured: list[list[str]] = []

    def _capture(cmd: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:  # noqa: ARG001
        captured.append(cmd)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "run_cmd", _capture)
        monkeypatch.setattr(build_module, "_ecr_login", lambda image_name, *, dry_run: None)
        getattr(build_module, fn_name)(dry_run=True, **kwargs)  # type: ignore[operator]
    finally:
        monkeypatch.undo()

    buildx = next((c for c in captured if "buildx" in c), None)
    assert buildx is not None, f"no buildx command generated; captured {captured}"
    return buildx


def test_build_passes_the_resolved_base_image() -> None:
    """`FROM ${BASE_IMAGE}` is defaultless, so an unpassed arg is an empty FROM."""
    cmd = _captured_build_command("build", fg_labs_sha="0" * 40, image_name="test")
    passed = dict(
        arg.split("=", 1) for flag, arg in pairwise(cmd) if flag == "--build-arg" and "=" in arg
    )
    assert passed["BASE_IMAGE"] == f"test-base:{base_image_tag()}"


def test_build_base_targets_the_content_addressed_tag() -> None:
    """A floating tag would let one push silently change every image built FROM it."""
    cmd = _captured_build_command("build_base", image_name="test")
    tags = [arg for flag, arg in pairwise(cmd) if flag == "--tag"]
    assert tags == [f"test-base:{base_image_tag()}"]
    assert "--file" in cmd
    assert cmd[cmd.index("--file") + 1] == "docker/Dockerfile.base"


def test_every_defaultless_base_arg_reaches_the_generated_build_command() -> None:
    """Same audit `test_compat_arm.py` runs on the per-SHA image, for the base.

    BuildKit exports an unset `ARG` to the RUN shell as a set-but-EMPTY variable,
    so a forgotten `--build-arg` does not fail the build -- it silently clones
    the wrong ref or installs the wrong version.
    """
    dockerfile = base_dockerfile().read_text()
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
    assert defaultless, "no defaultless ARGs found -- has Dockerfile.base moved?"

    cmd = _captured_build_command("build_base", image_name="test")
    passed = {
        arg.split("=", 1)[0] for flag, arg in pairwise(cmd) if flag == "--build-arg" and "=" in arg
    }
    missing = sorted(defaultless - passed)
    assert not missing, (
        f"Dockerfile.base declares defaultless ARG(s) `build-base` never passes: "
        f"{missing}. BuildKit exports those as set-but-empty, which does not fail "
        "the build -- it silently builds the wrong thing."
    )


def test_main_dockerfile_no_longer_builds_what_the_base_carries() -> None:
    """Guards the split itself: these must not drift back into the per-SHA image.

    Re-adding any of them would restore the original problem -- expensive layers
    below a `FG_LABS_SHA`-invalidated one, rebuilt or cache-retained per SHA.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    builder_stage = dockerfile.split("############ Stage 2: runtime ############")[0]
    # Comments are stripped before matching. The prose here necessarily NAMES
    # what the code must not do -- line 118 explains that fg-labs no longer uses
    # `make multi` -- and a guard run against raw text fails on the explanation,
    # whose obvious "fix" is to delete it. Same reasoning as `_code_only` in
    # `test_compat_arm.py`.
    instructions = "\n".join(
        line for line in builder_stage.splitlines() if not line.lstrip().startswith("#")
    )
    for moved in ("rustup.rs", "cargo +stable install", "apt.llvm.org", "make multi"):
        assert moved not in instructions, (
            f"{moved!r} is back in the per-SHA builder stage; it belongs in docker/Dockerfile.base"
        )


def test_missing_base_image_failure_names_the_driver_cause() -> None:
    """buildx blames authentication for what is actually a driver-visibility problem.

    A `docker-container` builder runs in its own container with its own image
    store, so a locally-`--load`ed base is invisible to it and `FROM` becomes a
    registry pull -- surfacing as `pull access denied ... insufficient_scope`,
    which sends you off checking ECR credentials. Found by actually running the
    two-stage build; no amount of `buildx build --check` or unit testing reaches
    it, because it only appears when a real `FROM` is resolved.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "_image_in_local_daemon", lambda image: True)
        present = build_module._explain_base_image_failure("base:tag")
    finally:
        monkeypatch.undo()
    assert "docker-container" in present
    assert "BUILDX_BUILDER" in present
    assert "build-docker-base" in present


def test_absent_base_image_failure_points_at_rebuilding_it() -> None:
    """The other cause needs the opposite fix, so the two must not share a message."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "_image_in_local_daemon", lambda image: False)
        absent = build_module._explain_base_image_failure("base:tag")
    finally:
        monkeypatch.undo()
    assert "not in " in absent
    assert "build-docker-base" in absent
    assert "docker-container" not in absent, (
        "the driver hint is wrong when the image is simply absent"
    )


def test_build_base_also_rejects_multiple_platforms_with_load() -> None:
    """Sibling of the same guard on `build`.

    `build_base` carries the identical `--load` / `--platforms` shape, so fixing
    only `build` would leave the base-image path able to compile both
    architectures and then fail at the export step.
    """
    with pytest.raises(ValueError, match="cannot export multiple platforms"):
        build_module.build_base(
            image_name="test",
            platforms="linux/amd64,linux/arm64",
            load=True,
            dry_run=True,
        )
