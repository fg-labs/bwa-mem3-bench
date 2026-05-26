"""Smoke-tests the defopt CLI: imports, --help per subcommand."""

from __future__ import annotations

import subprocess
import sys


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bwa_mem3_bench.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_root_help() -> None:
    r = _run(["--help"])
    assert r.returncode == 0, r.stderr
    assert "build" in r.stdout
    assert "submit" in r.stdout
    assert "collect" in r.stdout
    assert "bless-baseline" in r.stdout
    assert "upload-data" in r.stdout
    assert "bench" in r.stdout


def test_subcommand_help_does_not_error() -> None:
    for name in ("build", "submit", "collect", "bless-baseline", "upload-data"):
        r = _run([name, "--help"])
        assert r.returncode == 0, f"{name} --help failed: {r.stderr}"


def test_bench_subgroup_help() -> None:
    r = _run(["bench", "--help"])
    assert r.returncode == 0, r.stderr
    assert "summary" in r.stdout


def test_subcommand_stubs_run_in_dry_mode() -> None:
    r = _run(["build", "--fg-labs-sha", "abcdef1", "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert "dry-run" in r.stdout.lower()


def test_build_baseline_arch_suffixes_tag_and_passes_build_arg() -> None:
    """`--baseline-arch avx512bw` appends -avx512bw to the SHA tag, passes
    the build-arg, and does NOT also tag :latest (would clobber the
    portable tag with a host-locked variant)."""
    r = _run(
        [
            "build",
            "--fg-labs-sha",
            "abcdef1",
            "--baseline-arch",
            "avx512bw",
            "--push",
            "--dry-run",
        ]
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "BASELINE_ARCH=avx512bw" in out
    assert ":abcdef1-avx512bw" in out
    assert ":latest" not in out


def test_build_no_baseline_arch_keeps_latest_tag_on_push() -> None:
    r = _run(
        [
            "build",
            "--fg-labs-sha",
            "abcdef1",
            "--push",
            "--dry-run",
        ]
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "BASELINE_ARCH=" in out  # passed as empty string
    assert ":abcdef1" in out
    assert ":latest" in out


def test_build_make_target_suffixes_tag_and_suppresses_latest() -> None:
    """`--make-target lto-build` appends -lto-build to the SHA tag, passes
    the FG_LABS_MAKE_TARGET build-arg, and does NOT also tag :latest
    (a build-flag variant tagged :latest would silently become the default
    for any submit that didn't specify the matching --make-target)."""
    r = _run(
        [
            "build",
            "--fg-labs-sha",
            "abcdef1",
            "--make-target",
            "lto-build",
            "--push",
            "--dry-run",
        ]
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "FG_LABS_MAKE_TARGET=lto-build" in out
    assert ":abcdef1-lto-build" in out
    assert ":latest" not in out


def test_build_unsupported_make_target_rejected() -> None:
    """An unsupported ``--make-target`` value must fail fast with a clear error."""
    r = _run(
        [
            "build",
            "--fg-labs-sha",
            "abcdef1",
            "--make-target",
            "bogus-target",
            "--dry-run",
        ]
    )
    assert r.returncode != 0, "expected non-zero exit for unsupported make-target"
    assert "make-target" in r.stderr.lower() or "make_target" in r.stderr.lower()


def test_build_baseline_arch_and_make_target_compose_suffix_order() -> None:
    """Combined ``--baseline-arch avx512bw --make-target lto-build`` composes
    the suffix as ``-avx512bw-lto-build`` (arch first, build flag second —
    matches the order they appear in the conceptual build pipeline). Both
    build-args are passed and ``:latest`` is suppressed."""
    r = _run(
        [
            "build",
            "--fg-labs-sha",
            "abcdef1",
            "--baseline-arch",
            "avx512bw",
            "--make-target",
            "lto-build",
            "--push",
            "--dry-run",
        ]
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "BASELINE_ARCH=avx512bw" in out
    assert "FG_LABS_MAKE_TARGET=lto-build" in out
    assert ":abcdef1-avx512bw-lto-build" in out
    assert ":latest" not in out
