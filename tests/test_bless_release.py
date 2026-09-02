"""Tests for the `bless-release` preflight + plan command."""

from __future__ import annotations

import pytest

from bwa_mem3_bench.commands import _bless_release
from bwa_mem3_bench.commands._bless_release import bless_release
from bwa_mem3_bench.release_allowances import DEFAULT_ALLOWANCES_PATH, load_allowances

# A synthetic full-length SHA that is absent from the allowance ledger (no ledger
# to_sha starts with `f`), so it stands in for an unblessed candidate without
# being invalidated when a real candidate is later blessed.
_CANDIDATE = "f" * 40

# A stale-golden test needs a release older than the newest to point at.
_MIN_RELEASES_FOR_STALE_TEST = 2


def _newest_golden() -> str:
    return load_allowances(DEFAULT_ALLOWANCES_PATH)[-1].to_sha


def test_preflight_passes_on_a_clean_tree(capsys: pytest.CaptureFixture[str]) -> None:
    bless_release(fg_labs_sha=_CANDIDATE, golden_ref_sha=_newest_golden(), strict=True)
    out = capsys.readouterr().out
    assert "Preflight passed." in out
    assert "[FAIL]" not in out
    # The plan is always printed, with the candidate SHA interpolated into it.
    assert f"submit --fg-labs-sha {_CANDIDATE}" in out
    assert "bless-golden" in out


def test_short_candidate_sha_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        bless_release(fg_labs_sha="deadbeef", golden_ref_sha=_newest_golden(), strict=True)
    assert "[FAIL] candidate SHA is a full 40-hex" in capsys.readouterr().out


def test_already_blessed_candidate_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A candidate matching ANY existing allowance (not just the newest) is rejected."""
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    older = allowances[0].to_sha  # an old blessed release, not the newest golden
    with pytest.raises(SystemExit):
        bless_release(fg_labs_sha=older, golden_ref_sha=_newest_golden(), strict=True)
    assert "not an already-blessed release" in capsys.readouterr().out


def test_stale_golden_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A golden older than the newest blessed release is rejected."""
    allowances = load_allowances(DEFAULT_ALLOWANCES_PATH)
    if len(allowances) < _MIN_RELEASES_FOR_STALE_TEST:
        pytest.skip("need at least two blessed releases to exercise a stale golden")
    older = allowances[-2].to_sha
    with pytest.raises(SystemExit):
        bless_release(fg_labs_sha=_CANDIDATE, golden_ref_sha=older, strict=True)
    out = capsys.readouterr().out
    assert "MOST recent blessed release" in out
    assert "[FAIL]" in out


def test_unresolvable_golden_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A golden SHA absent from the ledger fails check #3, not silently passes."""
    with pytest.raises(SystemExit):
        bless_release(fg_labs_sha=_CANDIDATE, golden_ref_sha="0" * 40, strict=True)
    assert "resolves to a blessed release" in capsys.readouterr().out


def test_ambiguous_golden_prefix_is_a_clean_fail_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ambiguous short golden prefix must FAIL with output, never a raw traceback.

    Several ledger SHAs start with `4`, so `--golden-ref-sha 4` is genuinely
    ambiguous. `allowance_for` raises ValueError on that; the preflight must
    catch it and still print every check line and the plan.
    """
    bless_release(fg_labs_sha=_CANDIDATE, golden_ref_sha="4", strict=False)
    out = capsys.readouterr().out
    assert "ambiguous SHA" in out
    assert "[FAIL]" in out
    assert "Plan (see docs/RELEASE.md" in out  # the plan still printed


def test_inconsistent_ladder_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale arena ladder (check #5) fails the preflight and prints the problem."""
    monkeypatch.setattr(_bless_release, "ladder_problems", lambda _a: ["arena tail is stale"])
    with pytest.raises(SystemExit):
        bless_release(fg_labs_sha=_CANDIDATE, golden_ref_sha=_newest_golden(), strict=True)
    out = capsys.readouterr().out
    assert "arena ladder is consistent" in out
    assert "arena tail is stale" in out


def test_non_strict_prints_plan_even_on_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --strict, failures are reported but the plan still prints."""
    bless_release(fg_labs_sha="deadbeef", golden_ref_sha=_newest_golden(), strict=False)
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Plan (see docs/RELEASE.md" in out
