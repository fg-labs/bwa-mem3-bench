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
