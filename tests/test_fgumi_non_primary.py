"""`fgumi compare bams` must see a difference confined to a non-primary record.

Both `--compat` arms are gated on `fgumi compare bams`, and the byte-identity
claim they make covers *every* record — secondary and supplementary included,
not only the primaries `compare-bams` scores. Nothing in this repo asserted
that: the claim rested on a comment in the Dockerfile. A `FGUMI_REF` bump that
regressed non-primary comparison would quietly weaken every compat gate while
every arm kept reporting success.

Synthetic fixtures rather than corpus BAMs, and for the secondary case that is
forced rather than convenient. `--compat` and `--meth` are mutually exclusive
and no rule passes `-a`, so **no compat sample can contain a secondary record**
— measured on the heaviest case, `hic-1M-compat` carries 381,418 supplementary
records and zero secondary ones. The corpus cannot exercise this at all.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pysam
import pytest

from bwa_mem3_bench import fgumi_ref

REPO_ROOT = Path(__file__).resolve().parent.parent

# Flags. 0x1 PAIRED, 0x40 READ1, 0x100 SECONDARY, 0x800 SUPPLEMENTARY.
PRIMARY_R1 = 0x1 | 0x40
SUPPLEMENTARY_R1 = PRIMARY_R1 | 0x800
SECONDARY_R1 = PRIMARY_R1 | 0x100


def installed_fgumi_ref(install_root: Path) -> str | None:
    """The git rev `cargo install --root <install_root>` actually built, if any.

    `cargo install` records its source in `.crates.toml` next to `bin/`, e.g.
    `"fgumi 0.5.0 (git+https://…/fgumi?rev=<sha>#<sha>)" = ["fgumi"]`. That is
    the only place the REV survives: `fgumi --version` prints `fgumi 0.5.0`, a
    release number identical across every rev of that release, so running the
    binary cannot tell a stale build from a current one.

    The match is anchored on fgumi's own table key rather than taken from the
    first `rev=` in the file. `.crates.toml` describes the whole install ROOT,
    so any other git-installed package contributes its own `rev=` to it, and
    the table is ordered by package name — a name sorting before `fgumi` would
    otherwise supply the answer.

    Returns None when the file is absent or records no git rev — e.g. a build
    installed from a path or a registry rather than from the pin.
    """
    manifest = install_root / ".crates.toml"
    if not manifest.is_file():
        return None
    match = re.search(r'^"fgumi [^"]*\brev=([0-9a-f]{7,40})', manifest.read_text(), re.MULTILINE)
    return match.group(1) if match else None


def _fgumi_binary() -> str:
    """Path to the *pinned* `fgumi`, or skip.

    Only the repo-local install counts. There is deliberately no PATH fallback:
    a developer's own `fgumi` is an arbitrary version, and this module exists to
    pin the behaviour of the exact ref `docker/build-arg-defaults.env` names —
    running it against a different build would assert nothing about what the
    image does while still reporting green. Skipping says so; passing against
    an unpinned binary would not.

    Existence is not enough, for the same reason. `.fgumi/` is a build artifact
    that outlives the pin: a `FGUMI_REF` bump leaves the previous binary sitting
    there until someone re-runs the install, and in CI it is a restored cache.
    So the recorded rev is checked against the pin and a mismatch skips — the
    stale binary would otherwise assert the compat gates' semantics against the
    wrong build of fgumi, silently and in the green direction.
    """
    install_root = REPO_ROOT / ".fgumi"
    if not (install_root / "bin" / "fgumi").is_file():
        pytest.skip("pinned fgumi not built — run `pixi run install-fgumi`")
    installed = installed_fgumi_ref(install_root)
    pinned = fgumi_ref()
    if installed != pinned:
        pytest.skip(
            f"fgumi in .fgumi/ was built from {installed or 'an unrecorded source'}, "
            f"but the pin is {pinned} — re-run `pixi run install-fgumi`"
        )
    return str(install_root / "bin" / "fgumi")


def _header() -> pysam.AlignmentHeader:
    return pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "unsorted", "GO": "query"},
            "SQ": [{"SN": "chr1", "LN": 1_000_000}],
        }
    )


def _record(
    header: pysam.AlignmentHeader,
    *,
    flag: int,
    pos: int,
    mapq: int,
    cigar: str = "10M",
) -> pysam.AlignedSegment:
    """One alignment record. `query_name` is shared so all records form a single
    template, which is what puts the non-primary records in the same group as
    the primary they belong to."""
    record = pysam.AlignedSegment(header)
    record.query_name = "t1"
    record.flag = flag
    record.reference_id = 0
    record.reference_start = pos
    record.mapping_quality = mapq
    record.cigarstring = cigar
    record.query_sequence = "ACGTACGTAC"
    record.query_qualities = pysam.qualitystring_to_array("IIIIIIIIII")
    return record


def _write_bam(
    path: Path, records: list[pysam.AlignedSegment], header: pysam.AlignmentHeader
) -> Path:
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for record in records:
            out.write(record)
    return path


def _compare(a: Path, b: Path) -> tuple[int, str]:
    """`(exit status, stdout)` of `fgumi compare bams a b`.

    Returns the output as well as the status because status alone does not
    discriminate: fgumi exits 1 for a genuine DIFFER *and* for a malformed
    input, an unreadable file, or a header precondition failure. A test that
    asserts only `== 1` therefore passes when the binary never compared the
    records at all, which is precisely the regression this module guards.
    """
    proc = subprocess.run(
        [_fgumi_binary(), "compare", "bams", str(a), str(b), "--max-diffs", "5"],
        capture_output=True,
        check=False,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_identical_streams_compare_equal(tmp_path: Path) -> None:
    """Control. Without this, a comparator that reported DIFFER unconditionally
    would pass every other test in this module."""
    header = _header()
    records = [
        _record(header, flag=PRIMARY_R1, pos=100, mapq=60),
        _record(header, flag=SUPPLEMENTARY_R1, pos=5000, mapq=60),
        _record(header, flag=SECONDARY_R1, pos=9000, mapq=0),
    ]
    a = _write_bam(tmp_path / "a.bam", records, header)
    b = _write_bam(tmp_path / "b.bam", records, header)
    code, out = _compare(a, b)
    assert code == 0
    assert "IDENTICAL" in out
    assert "Content diffs: 0" in out


def test_a_difference_confined_to_a_supplementary_is_detected(tmp_path: Path) -> None:
    """Primaries identical; only the supplementary's MAPQ moves."""
    header = _header()
    primary = _record(header, flag=PRIMARY_R1, pos=100, mapq=60)
    a = _write_bam(
        tmp_path / "a.bam",
        [primary, _record(header, flag=SUPPLEMENTARY_R1, pos=5000, mapq=60)],
        header,
    )
    b = _write_bam(
        tmp_path / "b.bam",
        [primary, _record(header, flag=SUPPLEMENTARY_R1, pos=5000, mapq=11)],
        header,
    )
    code, out = _compare(a, b)
    assert code == 1
    # Record 1 is the supplementary (record 0 is the primary), and the field
    # named must be the one that actually differs — asserting only `code == 1`
    # would pass if fgumi had failed to read the file at all.
    assert "Record counts: 2 vs 2" in out, "not a count mismatch — the pair was compared"
    assert 'record 1: MAPQ: "60" vs "11"' in out, out


def test_a_difference_confined_to_a_secondary_is_detected(tmp_path: Path) -> None:
    """The half no corpus BAM can cover — see this module's docstring."""
    header = _header()
    primary = _record(header, flag=PRIMARY_R1, pos=100, mapq=60)
    a = _write_bam(
        tmp_path / "a.bam",
        [primary, _record(header, flag=SECONDARY_R1, pos=9000, mapq=0)],
        header,
    )
    b = _write_bam(
        tmp_path / "b.bam",
        [primary, _record(header, flag=SECONDARY_R1, pos=9000, mapq=0, cigar="5M5S")],
        header,
    )
    code, out = _compare(a, b)
    assert code == 1
    assert "Record counts: 2 vs 2" in out, "not a count mismatch — the pair was compared"
    assert 'record 1: CIGAR: "10M" vs "5M5S"' in out, out


def test_a_missing_non_primary_record_is_detected(tmp_path: Path) -> None:
    """Presence, not just content: dropping a supplementary entirely must fail
    the comparison. This is the shape a regression would most plausibly take —
    a comparator that filtered to primaries would see two identical streams."""
    header = _header()
    primary = _record(header, flag=PRIMARY_R1, pos=100, mapq=60)
    supplementary = _record(header, flag=SUPPLEMENTARY_R1, pos=5000, mapq=60)
    a = _write_bam(tmp_path / "a.bam", [primary, supplementary], header)
    b = _write_bam(tmp_path / "b.bam", [primary], header)
    code, out = _compare(a, b)
    assert code == 1
    # A dropped record must be reported as a COUNT difference, not silently
    # tolerated the way a primary-only comparator would.
    assert "Record counts: 2 vs 1" in out, out


def test_installed_ref_is_read_from_the_cargo_install_manifest(tmp_path: Path) -> None:
    """The rev is recovered from `.crates.toml`, which is where cargo records it.

    Written against the real format `cargo install --root` emits, because that
    file is the only thing distinguishing two builds of the same release: the
    binary reports `fgumi 0.5.0` either way.
    """
    ref = "3df1022c47420b1dd23373d0283ecb9fa9cf8bc0"
    (tmp_path / ".crates.toml").write_text(
        '[v1]\n"fgumi 0.5.0 (git+https://github.com/fulcrumgenomics/fgumi'
        f'?rev={ref}#{ref})" = ["fgumi"]\n'
    )
    assert installed_fgumi_ref(tmp_path) == ref


def test_installed_ref_ignores_other_packages_in_the_manifest(tmp_path: Path) -> None:
    """A neighbouring git-installed package must not answer for fgumi.

    `.crates.toml` is per-install-ROOT, not per-package, so any other
    `cargo install --root .fgumi --git ...` lands its own `rev=` in the same
    file — and cargo orders the table by package name, so a name sorting before
    `fgumi` comes first. Reading whichever rev appears first would then compare
    that package's provenance against the fgumi pin: a match skips the whole
    module for no reason, and a coincidental equality would certify a stale
    fgumi as current.
    """
    other, ref = "deadbeef" * 5, "3df1022c47420b1dd23373d0283ecb9fa9cf8bc0"
    (tmp_path / ".crates.toml").write_text(
        "[v1]\n"
        f'"cargo-nextest 0.9.0 (git+https://github.com/x/y?rev={other}#{other})"'
        ' = ["cargo-nextest"]\n'
        f'"fgumi 0.5.0 (git+https://github.com/fulcrumgenomics/fgumi?rev={ref}#{ref})"'
        ' = ["fgumi"]\n'
    )
    assert installed_fgumi_ref(tmp_path) == ref


def test_installed_ref_is_none_when_provenance_is_absent(tmp_path: Path) -> None:
    """No manifest, or one recording a non-git source, must not read as a match.

    Returning the pin (or anything truthy) on a build whose origin is unknown
    would defeat the check: `_fgumi_binary` treats equality as proof the binary
    came from the pinned ref.

    The third case is the same hazard as the neighbouring-package test above,
    read the other way round and strictly worse: there fgumi HAS a rev and a
    neighbour's is preferred, here fgumi has NONE and a neighbour's is the only
    one in the file. An unanchored match cannot return None for a path-installed
    fgumi while any git-installed package shares the root.
    """
    assert installed_fgumi_ref(tmp_path) is None
    (tmp_path / ".crates.toml").write_text('[v1]\n"fgumi 0.5.0 (path+file:///tmp/x)" = ["fgumi"]\n')
    assert installed_fgumi_ref(tmp_path) is None
    other = "deadbeef" * 5
    (tmp_path / ".crates.toml").write_text(
        "[v1]\n"
        f'"cargo-nextest 0.9.0 (git+https://github.com/x/y?rev={other}#{other})"'
        ' = ["cargo-nextest"]\n'
        '"fgumi 0.5.0 (path+file:///tmp/x)" = ["fgumi"]\n'
    )
    assert installed_fgumi_ref(tmp_path) is None


def test_the_built_fgumi_matches_the_pin_or_the_suite_skips() -> None:
    """The guard must hold on whatever is installed right now.

    Not a tautology: it fails if `installed_fgumi_ref` stops recovering the rev
    from a real cargo-install tree (a format change, a bad regex), which would
    otherwise turn every test in this module into a silent skip and take the
    compat arms' non-primary coverage with it.
    """
    install_root = REPO_ROOT / ".fgumi"
    if not (install_root / "bin" / "fgumi").is_file():
        pytest.skip("pinned fgumi not built — run `pixi run install-fgumi`")
    assert installed_fgumi_ref(install_root) == fgumi_ref(), (
        "the installed fgumi does not match docker/build-arg-defaults.env — "
        "re-run `pixi run install-fgumi`"
    )
