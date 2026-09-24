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
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=authorized,
            from_s3=True,
            min_reps=1,
            dry_run=True,
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


def _authorized(tmp_path: Path, sha: str) -> Path:
    return _write(
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


def _run_listing(sha: str, cells: list[tuple[str, str]], reps: int) -> str:
    return "\n".join(
        f"2026 100 runs/{sha}/{sample}/{arch}/rep-{rep}/aligned.bam"
        for sample, arch in cells
        for rep in range(1, reps + 1)
    )


def test_bless_refuses_an_under_replicated_run(tmp_path: Path) -> None:
    """A sweep that ran at one rep (the v0.13.0 failure) is not blessable by default."""
    sha = "deadbeef"
    ls = _run_listing(sha, [("wgs-5M", "c6a"), ("wes-5M", "c6a")], reps=1)
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=ls, stderr="")
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=completed),
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
        pytest.raises(ValueError, match="at most 1 rep"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
            dry_run=True,
        )
    mock_cp.assert_not_called()


def test_bless_refuses_a_run_with_no_replicate_bams(tmp_path: Path) -> None:
    """An empty run is under-replicated too; it must not report a successful no-op bless."""
    sha = "deadbeef"
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=completed),
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
        pytest.raises(ValueError, match="at most 0 rep"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
            dry_run=True,
        )
    mock_cp.assert_not_called()


def test_bless_accepts_single_rep_cells_when_the_sweep_is_replicated(tmp_path: Path) -> None:
    """Single-rep-by-design cells (the ALT arms) do not trip the guard."""
    sha = "deadbeef"
    ls = "\n".join(
        [
            _run_listing(sha, [("wgs-5M", "c6a")], reps=5),
            _run_listing(sha, [("wgs-5M-alt", "c6a")], reps=1),
        ]
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=ls, stderr="")
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=completed),
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
            dry_run=True,
        )
    assert mock_cp.call_count == 2  # noqa: PLR2004


def test_bless_refuses_a_cell_without_a_rep1_bam_from_s3(tmp_path: Path) -> None:
    """A cell with only higher reps would otherwise be silently left out of the golden."""
    sha = "deadbeef"
    ls = "\n".join(
        [
            _run_listing(sha, [("wgs-5M", "c6a")], reps=5),
            f"2026 100 runs/{sha}/wes-5M/c6a/rep-2/aligned.bam",
        ]
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=ls, stderr="")
    with (
        patch.object(bless_golden_module.subprocess, "run", return_value=completed),
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
        pytest.raises(ValueError, match=r"1 run cell\(s\) have no rep-1 aligned.bam: wes-5M/c6a"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
            dry_run=True,
        )
    mock_cp.assert_not_called()


def _local_run(root: Path, sha: str, cell: tuple[str, str], rep_dirs: list[str]) -> None:
    for rep_dir in rep_dirs:
        bam = root / "runs" / sha / cell[0] / cell[1] / rep_dir / "aligned.bam"
        bam.parent.mkdir(parents=True, exist_ok=True)
        bam.touch()


def test_bless_local_ignores_non_numeric_rep_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four real reps plus a ``rep-backup`` is still under-replicated at a minimum of 5."""
    _isolate_runs_root(monkeypatch, tmp_path)
    sha = "deadbeef"
    _local_run(tmp_path, sha, ("wgs-5M", "c6a"), ["rep-1", "rep-2", "rep-3", "rep-4", "rep-backup"])
    with (
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
        pytest.raises(ValueError, match="at most 4 rep"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            min_reps=5,
            dry_run=True,
        )
    mock_cp.assert_not_called()


def test_bless_local_refuses_a_cell_without_a_rep1_bam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_runs_root(monkeypatch, tmp_path)
    sha = "deadbeef"
    _local_run(tmp_path, sha, ("wgs-5M", "c6a"), [f"rep-{r}" for r in range(1, 6)])
    _local_run(tmp_path, sha, ("wes-5M", "c6a"), ["rep-2"])
    with (
        patch.object(bless_golden_module, "run_cmd") as mock_cp,
        pytest.raises(ValueError, match=r"have no rep-1 aligned.bam: wes-5M/c6a"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            min_reps=5,
            dry_run=True,
        )
    mock_cp.assert_not_called()


def test_bless_fails_when_the_golden_is_incomplete_after_copy(tmp_path: Path) -> None:
    """An interrupted copy (the v0.12.0 failure) must fail loudly, naming the gap."""
    sha = "deadbeef"
    cells = [("hic-1M", "c6a"), ("wes-5M", "c6a"), ("wgs-5M", "c6a")]
    run_ls = _run_listing(sha, cells, reps=5)
    # Only the first cell made it into the golden.
    golden_ls = f"2026 100 golden/fg-labs-{sha}/hic-1M/c6a/aligned.bam"

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out = golden_ls if "/golden/" in argv[-1] else run_ls
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")

    golden_mod = importlib.import_module("bwa_mem3_bench.golden")
    with (
        patch.object(bless_golden_module.subprocess, "run", side_effect=fake_run),
        patch.object(golden_mod.subprocess, "run", side_effect=fake_run),
        patch.object(bless_golden_module, "run_cmd"),
        pytest.raises(RuntimeError, match=r"2 cell\(s\) missing: wes-5M/c6a, wgs-5M/c6a"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
        )


def test_bless_passes_when_the_golden_is_complete_after_copy(tmp_path: Path) -> None:
    sha = "deadbeef"
    cells = [("wes-5M", "c6a"), ("wgs-5M", "c6a")]
    run_ls = _run_listing(sha, cells, reps=5)
    golden_ls = "\n".join(f"2026 100 golden/fg-labs-{sha}/{s}/{a}/aligned.bam" for s, a in cells)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out = golden_ls if "/golden/" in argv[-1] else run_ls
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")

    golden_mod = importlib.import_module("bwa_mem3_bench.golden")
    with (
        patch.object(bless_golden_module.subprocess, "run", side_effect=fake_run),
        patch.object(golden_mod.subprocess, "run", side_effect=fake_run),
        patch.object(bless_golden_module, "run_cmd"),
    ):
        bless_golden(
            fg_labs_sha=sha,
            bucket="B",
            allowances_path=_authorized(tmp_path, sha),
            from_s3=True,
            min_reps=5,
        )
