"""Tests for the release-allowances registry + bless-golden sign-off guard."""

import importlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module object (not the re-exported function) for patching its
# `subprocess` / `run_cmd` symbols — `commands/__init__` re-exports the
# `bless_golden` *function*, which shadows the submodule on attribute access.
from bwa_mem3_bench.commands import _bless_golden as bless_golden_module
from bwa_mem3_bench.commands._bless_golden import _parse_s3_bams, bless_golden
from bwa_mem3_bench.release_allowances import (
    DEFAULT_ALLOWANCES_PATH,
    ReleaseAllowance,
    allowance_for,
    canonical_golden_sha,
    load_allowances,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_shipped_allowances_file_loads_and_authorizes_backfill() -> None:
    # The shipped file records the historical golden sign-offs. v0.2.0 is the
    # first golden (--force, no entry); v0.2.1 and v0.2.2 are authorized here.
    entries = load_allowances(DEFAULT_ALLOWANCES_PATH)
    assert all(isinstance(e, ReleaseAllowance) for e in entries)
    v021 = "89bd589db9fcb56279912fa6b23e0831f4916a62"
    v022 = "bffae5a09267877fe514c458d4956b717bcefb8f"
    assert allowance_for(entries, v021) is not None
    assert allowance_for(entries, v022) is not None
    # An un-signed-off SHA (e.g. v0.2.0, the force-blessed first golden) is not.
    assert allowance_for(entries, "44cbaec301d1fafe2d66ca9085547c5aedf25373") is None


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
    bless_golden_mod = importlib.import_module("bwa_mem3_bench.commands._bless_golden")
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


def test_parse_s3_bams_selects_rep1_and_rewrites_to_golden() -> None:
    sha = "44cbaec"
    ls = "\n".join(
        [
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-2/aligned.bam",  # skipped (rep-2)
            f"2026 50 runs/{sha}/wes-5M/c6a/rep-1/compare/vs-baseline.json",  # skipped
            f"2026 100 runs/{sha}/wgs-5M/c8g/rep-1/aligned.bam",
        ]
    )
    pairs = _parse_s3_bams(ls, bucket="B", fg_labs_sha=sha)
    assert pairs == [
        (
            f"s3://B/runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"s3://B/golden/fg-labs-{sha}/wes-5M/c6a/aligned.bam",
        ),
        (
            f"s3://B/runs/{sha}/wgs-5M/c8g/rep-1/aligned.bam",
            f"s3://B/golden/fg-labs-{sha}/wgs-5M/c8g/aligned.bam",
        ),
    ]


def test_bless_from_s3_still_enforces_allowance(tmp_path: Path) -> None:
    empty = _write(tmp_path / "empty.yaml", "allowances: []\n")
    with pytest.raises(ValueError, match="refusing to bless"):
        bless_golden(fg_labs_sha="deadbeef", allowances_path=empty, from_s3=True, dry_run=True)


def test_parse_s3_bams_notes_extra_reps_for_blessed_cells(capsys: pytest.CaptureFixture) -> None:
    sha = "44cbaec"
    ls = "\n".join(
        [
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-2/aligned.bam",
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-3/aligned.bam",
        ]
    )
    pairs = _parse_s3_bams(ls, bucket="B", fg_labs_sha=sha)
    assert len(pairs) == 1  # only rep-1 blessed
    note = capsys.readouterr().err
    assert "blessing rep-1 only for wes-5M/c6a" in note
    assert "ignoring 2 additional rep(s)" in note


def test_parse_s3_bams_dry_run_suppresses_extra_rep_note(
    capsys: pytest.CaptureFixture,
) -> None:
    sha = "44cbaec"
    ls = "\n".join(
        [
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-2/aligned.bam",
        ]
    )
    _parse_s3_bams(ls, bucket="B", fg_labs_sha=sha, dry_run=True)
    assert capsys.readouterr().err == ""


def test_bless_from_s3_authorized_lists_and_copies(tmp_path: Path) -> None:
    sha = "deadbeef"
    authorized = _write(
        tmp_path / "a.yaml",
        f"""
allowances:
  - to_sha: {sha}
    pr: fg-labs/bwa-mem3#1
    date: 2026-06-07
    summary: "intentional"
    expected_drift_pct: 0.1
""".strip(),
    )
    ls_stdout = "\n".join(
        [
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"2026 100 runs/{sha}/wgs-5M/c8g/rep-1/aligned.bam",
        ]
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=ls_stdout, stderr="")
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=completed) as mock_ls,
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
    ):
        bless_golden(
            fg_labs_sha=sha, bucket="B", allowances_path=authorized, from_s3=True, dry_run=True
        )
    mock_ls.assert_called_once()
    copied = [call.args[0] for call in mock_cp.call_args_list]
    assert copied == [
        [
            "aws",
            "s3",
            "cp",
            f"s3://B/runs/{sha}/wes-5M/c6a/rep-1/aligned.bam",
            f"s3://B/golden/fg-labs-{sha}/wes-5M/c6a/aligned.bam",
        ],
        [
            "aws",
            "s3",
            "cp",
            f"s3://B/runs/{sha}/wgs-5M/c8g/rep-1/aligned.bam",
            f"s3://B/golden/fg-labs-{sha}/wgs-5M/c8g/aligned.bam",
        ],
    ]


def test_bless_from_s3_surfaces_aws_error(tmp_path: Path) -> None:
    sha = "deadbeef"
    authorized = _write(
        tmp_path / "a.yaml",
        f"""
allowances:
  - to_sha: {sha}
    pr: fg-labs/bwa-mem3#1
    date: 2026-06-07
    summary: "intentional"
    expected_drift_pct: 0.1
""".strip(),
    )
    failed = subprocess.CompletedProcess(
        args=[], returncode=255, stdout="", stderr="fatal error: An error occurred (AccessDenied)"
    )
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=failed),
        pytest.raises(RuntimeError, match="AccessDenied"),
    ):
        bless_golden(
            fg_labs_sha=sha, bucket="B", allowances_path=authorized, from_s3=True, dry_run=True
        )


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


# --------------------------------------------------------------------------- #
# SHA aliases: one blessed golden, referenced by multiple code-identical SHAs
# (e.g. the benched release-please branch head and its squash-merged tag).
# --------------------------------------------------------------------------- #


def _write_aliased(path: Path) -> Path:
    return _write(
        path,
        """
allowances:
  - to_sha: 9dd30dd0e5e477ddfd33bec752179978ac9f5a1d
    pr: fg-labs/bwa-mem3-bench#25
    date: 2026-07-03
    summary: v0.5.0 — golden lives here; the release tag is a code-identical alias.
    expected_drift_pct: 0.1
    aliases:
      - b2fea467b776751e665a022c0f01319e7a92155b
""",
    )


def test_aliases_parse(tmp_path: Path) -> None:
    entries = load_allowances(_write_aliased(tmp_path / "a.yaml"))
    assert len(entries) == 1
    assert entries[0].aliases == ("b2fea467b776751e665a022c0f01319e7a92155b",)


def test_aliases_default_empty_when_absent(tmp_path: Path) -> None:
    # An entry with no `aliases` key must default to an empty tuple (the common
    # single-SHA case). Uses a temp file so it does not depend on whether the
    # shipped registry happens to contain an aliased entry.
    p = _write(
        tmp_path / "a.yaml",
        """
allowances:
  - to_sha: 44cbaec
    pr: fg-labs/bwa-mem3#123
    date: 2026-06-08
    summary: no aliases here
    expected_drift_pct: 0.0
""",
    )
    entries = load_allowances(p)
    assert entries[0].aliases == ()


def test_canonical_golden_sha_resolves_alias_to_to_sha(tmp_path: Path) -> None:
    entries = load_allowances(_write_aliased(tmp_path / "a.yaml"))
    to_sha = "9dd30dd0e5e477ddfd33bec752179978ac9f5a1d"
    tag = "b2fea467b776751e665a022c0f01319e7a92155b"
    # The release-tag SHA resolves to where the golden physically lives.
    assert canonical_golden_sha(entries, tag) == to_sha
    # A prefix of the alias resolves too (CLI accepts short SHAs).
    assert canonical_golden_sha(entries, "b2fea46") == to_sha
    # The canonical SHA resolves to itself.
    assert canonical_golden_sha(entries, to_sha) == to_sha
    assert canonical_golden_sha(entries, "9dd30dd0") == to_sha


def test_canonical_golden_sha_passthrough_for_unaliased(tmp_path: Path) -> None:
    entries = load_allowances(_write_aliased(tmp_path / "a.yaml"))
    # A SHA that matches no entry is returned unchanged (golden lives at itself).
    assert canonical_golden_sha(entries, "deadbeefdeadbeef") == "deadbeefdeadbeef"
    assert canonical_golden_sha([], "anything") == "anything"


def test_allowance_for_matches_an_alias(tmp_path: Path) -> None:
    entries = load_allowances(_write_aliased(tmp_path / "a.yaml"))
    # bless authorization recognizes the alias, not just the canonical to_sha.
    a = allowance_for(entries, "b2fea467b776751e665a022c0f01319e7a92155b")
    assert a is not None and a.to_sha == "9dd30dd0e5e477ddfd33bec752179978ac9f5a1d"


def test_aliases_scalar_string_rejected(tmp_path: Path) -> None:
    # A YAML author who forgets the list dash writes `aliases: b2fea...` (a bare
    # scalar). Without a guard, `tuple(str(a) for a in "b2fea...")` iterates the
    # string character-by-character and silently produces a bogus alias tuple,
    # breaking alias matching with no error signal. Fail fast instead.
    p = _write(
        tmp_path / "a.yaml",
        """
allowances:
  - to_sha: 44cbaec
    pr: fg-labs/bwa-mem3#123
    date: 2026-06-08
    summary: scalar aliases (missing list dash)
    expected_drift_pct: 0.0
    aliases: b2fea467b776751e665a022c0f01319e7a92155b
""",
    )
    with pytest.raises(ValueError, match=r"aliases.*must be a list"):
        load_allowances(p)
