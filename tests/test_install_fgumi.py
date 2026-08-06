"""Guards for ``scripts/install-fgumi.sh``'s preflight checks.

The script's job is to build the pinned `fgumi` before the test suite needs it.
Everything asserted here happens BEFORE `cargo install` runs, so these tests are
fast and never touch the network: stub `cargo` and `rustc` on PATH are enough to
drive the preflight to its exit.

The preflight exists because every failure mode it covers produces a confusing
message on its own — a bare `cargo: command not found` from `set -e`, or a
compile error thousands of lines into a dependency tree — for a cause the script
can state in one line.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-fgumi.sh"


def _stub_toolchain(tmp_path: Path, *, cargo: str | None = None, rustc: str | None = None) -> str:
    """Return a PATH whose `cargo` / `rustc` are stubs printing the given lines.

    `None` installs no stub for that tool, which reproduces a machine without
    it. The two are independent on purpose: the versions cargo and rustc report
    can disagree, and that disagreement is the case under test.

    The real PATH is dropped rather than prepended to, so a developer's own
    rustup toolchain cannot satisfy a lookup and mask the case under test.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, version_line in (("cargo", cargo), ("rustc", rustc)):
        if version_line is None:
            continue
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{version_line}"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f"{bin_dir}:/usr/bin:/bin"


def _run_install(path_value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        env={"PATH": path_value, "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_cargo_fails_with_an_actionable_message(tmp_path: Path) -> None:
    """No cargo on PATH must name the missing tool, not die inside a pipeline.

    `cargo --version | awk ...` with no cargo prints the shell's own
    `command not found` and — because the failing command is upstream of a pipe
    — reports whatever `awk` exited with unless `pipefail` catches it. Either
    way the operator is told nothing about what to install, and the message they
    do get points at awk.
    """
    result = _run_install(_stub_toolchain(tmp_path, rustc="rustc 1.94.0 (abc 2026)"))
    assert result.returncode != 0
    assert "cargo" in result.stderr.lower()
    assert "not found on PATH" in result.stderr
    # The remedy is the thing worth printing: this script is meant to run
    # through pixi, whose environment supplies a new enough toolchain.
    assert "pixi run install-fgumi" in result.stderr


def test_missing_rustc_fails_with_an_actionable_message(tmp_path: Path) -> None:
    """cargo alone is not a toolchain: the compiler must be there too.

    Checked separately from cargo because they are separately resolvable — a
    cargo can be on PATH with no `rustc` beside it, and the resulting failure
    comes out of the dependency build rather than the preflight.
    """
    result = _run_install(_stub_toolchain(tmp_path, cargo="cargo 1.94.0 (abc 2026)"))
    assert result.returncode != 0
    assert "rustc" in result.stderr.lower()
    assert "not found on PATH" in result.stderr


def test_rustc_below_the_floor_is_rejected_before_install(tmp_path: Path) -> None:
    """A too-old compiler must be refused up front, not thousands of lines into
    a dependency tree.

    fgumi declares `rust-version = 1.93`; a bare shell here picks up
    rust-toolchain.toml's deliberately older pin, so this is the common case for
    anyone running the script directly rather than through pixi.
    """
    result = _run_install(
        _stub_toolchain(tmp_path, cargo="cargo 1.85.0 (abc 2025)", rustc="rustc 1.85.0 (abc 2025)")
    )
    assert result.returncode != 0
    assert "1.93" in result.stderr
    assert "1.85.0" in result.stderr


def test_a_new_cargo_does_not_excuse_an_old_rustc(tmp_path: Path) -> None:
    """The floor is a COMPILER requirement, so cargo's version cannot answer it.

    `rust-version` is enforced against rustc, and the two are separately
    resolvable: a rust-toolchain.toml pin or an explicit `RUSTC` can point cargo
    at an older compiler than `cargo --version` reports. Checking only cargo
    lets that combination through the preflight and fails later, mid-build,
    with an error that names a dependency rather than the toolchain.
    """
    result = _run_install(
        _stub_toolchain(tmp_path, cargo="cargo 1.94.0 (abc 2026)", rustc="rustc 1.85.0 (abc 2025)")
    )
    assert result.returncode != 0
    assert "rustc" in result.stderr.lower(), "the message must name the tool that is too old"
    assert "1.85.0" in result.stderr


def _standalone_rustc(tmp_path: Path, name: str, version_line: str) -> Path:
    """An executable rustc stub OUTSIDE the stub PATH, for override tests."""
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    stub = other / name
    stub.write_text(f'#!/bin/sh\necho "{version_line}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run_install_with(path_value: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        env={"PATH": path_value, "HOME": os.environ.get("HOME", "/tmp"), **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("override", ["RUSTC", "CARGO_BUILD_RUSTC"])
def test_a_compiler_override_is_the_binary_checked(override: str, tmp_path: Path) -> None:
    """Both of cargo's compiler-override variables must be honoured.

    Checking PATH's `rustc` while cargo compiles with an overridden one would
    validate a compiler that never runs — the preflight passes on the wrong
    binary and the build fails later anyway. `RUSTC` and `CARGO_BUILD_RUSTC`
    both replace the compiler outright (unlike `RUSTC_WRAPPER`, which wraps the
    real one and so does not change its version), so both are checked.
    """
    old = _standalone_rustc(tmp_path, "rustc-old", "rustc 1.85.0 (abc 2025)")
    path = _stub_toolchain(
        tmp_path, cargo="cargo 1.94.0 (abc 2026)", rustc="rustc 1.94.0 (abc 2026)"
    )
    result = _run_install_with(path, **{override: str(old)})
    assert result.returncode != 0, "the overridden compiler is too old and must be rejected"
    assert "1.85.0" in result.stderr


def test_rustc_outranks_cargo_build_rustc(tmp_path: Path) -> None:
    """`RUSTC` wins when both are set, because that is cargo's own precedence.

    Resolving them the other way round would validate a compiler cargo is not
    going to use — rejecting a working toolchain, or passing a broken one.

    All three candidate compilers report a DIFFERENT version, so the banner the
    script prints once the preflight passes names which one it resolved. A
    negative assertion alone would not: this test would then also pass if the
    script died before reaching the check, for a reason that happens not to
    mention 1.85.0. The banner is asserted instead of the exit code because the
    script's last line runs the installed binary, which a checkout that has
    never run `pixi run install-fgumi` does not have.
    """
    old = _standalone_rustc(tmp_path, "rustc-old", "rustc 1.85.0 (abc 2025)")
    new = _standalone_rustc(tmp_path, "rustc-new", "rustc 1.94.0 (abc 2026)")
    path = _stub_toolchain(
        tmp_path, cargo="cargo 1.99.0 (abc 2026)", rustc="rustc 1.98.0 (abc 2026)"
    )
    # RUSTC names the NEW compiler, CARGO_BUILD_RUSTC the old one: cargo would
    # build with the new one, so the preflight must not reject on the old one.
    result = _run_install_with(path, RUSTC=str(new), CARGO_BUILD_RUSTC=str(old))
    assert "1.85.0" not in result.stderr, (
        "CARGO_BUILD_RUSTC must not outrank RUSTC — cargo reads RUSTC first"
    )
    assert "rustc 1.94.0" in result.stderr, (
        "the preflight must reach the install and report RUSTC's compiler — "
        f"neither CARGO_BUILD_RUSTC's 1.85.0 nor PATH's 1.98.0. Got: {result.stderr}"
    )
