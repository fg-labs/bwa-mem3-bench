"""Tests for the release-allowances registry + bless-golden sign-off guard."""

import importlib
from pathlib import Path

import pytest

from bwa_mem3_bench.commands.bless_golden import bless_golden
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    ReleaseAllowance,
    allowance_for,
    load_allowances,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_shipped_allowances_file_loads() -> None:
    # Ships empty; must still parse.
    assert load_allowances(DEFAULT_ALLOWANCES_PATH) == []


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_allowances_parse_and_match(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "a.yaml",
        """
allowances:
  - to_sha: 44cbaec
    pr: fg-labs/bwa-mem3#123
    date: 2026-06-07
    summary: "v0.2.0 tie-breaks"
    expected_drift_pct: 0.05
""".strip(),
    )
    entries = load_allowances(p)
    assert len(entries) == 1
    assert isinstance(entries[0], ReleaseAllowance)
    # prefix match works both directions (short tag SHA <-> full SHA)
    assert allowance_for(entries, "44cbaec") is not None
    assert allowance_for(entries, "44cbaec0deadbeef") is not None
    assert allowance_for(entries, "89bd589") is None


def _alw(to_sha: str) -> ReleaseAllowance:
    return ReleaseAllowance(
        to_sha=to_sha, pr="p", date="2026-06-07", summary="s", expected_drift_pct=0.1
    )


def test_allowance_for_empty_query_never_matches() -> None:
    assert allowance_for([_alw("44cbaec")], "") is None
    assert allowance_for([_alw("44cbaec")], "   ") is None


def test_allowance_for_ambiguous_raises() -> None:
    # Two entries whose SHAs both prefix-match a short query → ambiguous.
    entries = [_alw("44cbaec0"), _alw("44cbaec1")]
    with pytest.raises(ValueError, match="ambiguous"):
        allowance_for(entries, "44cbaec")


def test_allowances_missing_field_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "a.yaml", "allowances: [{to_sha: x}]\n")
    with pytest.raises(ValueError, match="summary"):
        load_allowances(p)


def test_bless_refuses_unauthorized_sha(tmp_path: Path) -> None:
    empty = _write(tmp_path / "empty.yaml", "allowances: []\n")
    with pytest.raises(ValueError, match="refusing to bless"):
        bless_golden(fg_labs_sha="deadbeef", allowances_path=empty, dry_run=True)


def _isolate_runs_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The "no local run" assertions below depend on runs/<sha> NOT existing.
    # Point REPO_ROOT at an empty tmp dir so the precondition is controlled and
    # the test can't flake on a stray local runs/ tree under the repo root.
    # Resolve the module via import_module (the sys.modules entry) rather than a
    # dotted target: commands/__init__ re-exports the bless_golden *function*,
    # which shadows the submodule on attribute access.
    bless_golden_mod = importlib.import_module("bwa_mem3_bench.commands.bless_golden")
    monkeypatch.setattr(bless_golden_mod, "REPO_ROOT", tmp_path)


def test_bless_force_bypasses_allowance_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_runs_root(monkeypatch, tmp_path)
    empty = _write(tmp_path / "empty.yaml", "allowances: []\n")
    # force skips the sign-off guard; it then fails later on the missing local
    # run dir — proving the guard was bypassed rather than the bless succeeding.
    with pytest.raises(FileNotFoundError, match="no local run"):
        bless_golden(fg_labs_sha="deadbeef", allowances_path=empty, force=True, dry_run=True)


def test_bless_allowed_sha_passes_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_runs_root(monkeypatch, tmp_path)
    authorized = _write(
        tmp_path / "a.yaml",
        """
allowances:
  - to_sha: deadbeef
    pr: fg-labs/bwa-mem3#1
    date: 2026-06-07
    summary: "intentional"
    expected_drift_pct: 0.1
""".strip(),
    )
    # Guard passes (sha is authorized); proceeds to the missing-run-dir error.
    with pytest.raises(FileNotFoundError, match="no local run"):
        bless_golden(fg_labs_sha="deadbeef", allowances_path=authorized, dry_run=True)
