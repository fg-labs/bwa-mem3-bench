"""Unit tests for the upload-data SE/PE staging branches."""

import gzip
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from bwa_mem3_bench import data_sources

# `bwa_mem3_bench.commands.__init__` re-exports the `upload_data` *function* under
# the same name as the submodule, shadowing it in the package namespace.  Use
# importlib to get the actual module object so monkeypatch can find `run_cmd`.
from bwa_mem3_bench.commands import _upload_data as _upload_data_mod
from bwa_mem3_bench.data_sources import DataSource, sample_sources

# Ensure _upload_sample is accessible for the tests.
_upload_sample = _upload_data_mod._upload_sample  # type: ignore[attr-defined]


def _capture_run_cmd(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        _upload_data_mod, "run_cmd", lambda cmd, dry_run=False: calls.append(list(cmd))
    )
    return calls


def test_sbx_se_converts_bam_then_uploads_single_fastq(monkeypatch, tmp_path) -> None:
    # Redirect STAGE_ROOT so the staged-file existence check + mkdir hit tmp.
    monkeypatch.setattr(data_sources, "STAGE_ROOT", tmp_path)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("sbx-1M", "test-bucket", dry_run=True)

    # A samtools BAM->FASTQ conversion runs, then exactly one r1 upload, no r2.
    convert = [c for c in calls if c[0] == "bash" and "samtools view" in c[2]]
    assert convert, f"expected a samtools conversion call; got {calls}"
    assert "samtools fastq -0" in convert[0][2]
    assert "-s 42.001166" in convert[0][2]
    uploads = [c for c in calls if c[:3] == ["aws", "s3", "cp"]]
    assert len(uploads) == 1
    assert uploads[0][-1].endswith("data/sbx/hg002-1M/r1.fq.gz")
    assert not any("r2.fq.gz" in part for c in calls for part in c)


def test_sbx_conversion_pipeline_uses_pipefail(monkeypatch, tmp_path) -> None:
    # A failing `samtools view` must abort the pipe rather than upload a
    # truncated FASTQ, so the bash command must set pipefail first.
    monkeypatch.setattr(data_sources, "STAGE_ROOT", tmp_path)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("sbx-1M", "test-bucket", dry_run=True)

    convert = [c for c in calls if c[0] == "bash" and "samtools view" in c[2]]
    assert convert, f"expected a samtools conversion call; got {calls}"
    assert convert[0][2].startswith("set -o pipefail;")


def test_downsample_pipeline_uses_pipefail(monkeypatch, tmp_path) -> None:
    # The gunzip | mawk | gzip downsample pipe has the same masked-failure
    # risk as the BAM conversion and must also set pipefail. Redirect the
    # vendor root to tmp so the downsampled-file existence check never short-
    # circuits the conversion on a host that already has staged outputs.
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("meth-twist-emseq-trim-5M", "test-bucket", dry_run=True)

    pipes = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert pipes, f"expected a gunzip downsample call; got {calls}"
    assert all(c[2].startswith("set -o pipefail;") for c in pipes)


def test_hic_pe_uploads_two_fastqs_no_conversion(monkeypatch) -> None:
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("hic-1M", "test-bucket", dry_run=True)

    assert not any("samtools" in part for c in calls for part in c)
    uploads = [c for c in calls if c[:3] == ["aws", "s3", "cp"]]
    assert [u[-1].rsplit("/", 1)[-1] for u in uploads] == ["r1.fq.gz", "r2.fq.gz"]


def _write_fastq(path: Path, n_records: int, read_len: int = 20) -> list[list[str]]:
    """Write ``n_records`` distinct FASTQ records; return them as 4-line lists."""
    records = []
    for i in range(n_records):
        seq = "".join("ACGT"[(i + j) % 4] for j in range(read_len))
        qual = "".join(chr(33 + (i + j) % 40) for j in range(read_len))
        records.append([f"@read{i} 1:N:0:ACGT", seq, "+", qual])
    with gzip.open(path, "wt") as fh:
        fh.writelines(line + "\n" for rec in records for line in rec)
    return records


def _read_fastq(path: Path) -> list[list[str]]:
    """Read a gzipped FASTQ back as 4-line records."""
    with gzip.open(path, "rt") as fh:
        lines = fh.read().splitlines()
    return [lines[i : i + 4] for i in range(0, len(lines), 4)]


def _run_transform(tmp_path: Path, every_nth: int, trim_5p: int) -> tuple[list, list]:
    """Run the staging transform on 10 generated records; return (input, output)."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=10)
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, every_nth, trim_5p)
    subprocess.run(["bash", "-c", cmd], check=True)
    return records, _read_fastq(dst)


@pytest.mark.parametrize("every_nth", [1, 3])
def test_transform_trims_seq_and_qual_after_downsampling(tmp_path: Path, every_nth: int) -> None:
    """Trim applies to the kept records' SEQ and QUAL only."""
    records, out = _run_transform(tmp_path, every_nth=every_nth, trim_5p=7)
    kept = records[::every_nth]
    assert len(out) == len(kept)
    for (hdr, seq, plus, qual), got in zip(kept, out, strict=True):
        # Header and `+` untouched; SEQ and QUAL lose exactly their first 7 chars.
        assert got == [hdr, seq[7:], plus, qual[7:]]
        assert len(got[1]) == len(got[3])


def test_transform_without_trim_matches_the_legacy_downsample(tmp_path: Path) -> None:
    """With no trim the output is the legacy every-Nth downsample."""
    records, out = _run_transform(tmp_path, every_nth=3, trim_5p=0)
    assert out == records[::3]  # records 1, 4, 7, 10 -- what the old mawk kept


def test_transform_is_a_plain_copy_with_no_downsample_and_no_trim(tmp_path: Path) -> None:
    """N=1 and no trim copies the input unchanged."""
    records, out = _run_transform(tmp_path, every_nth=1, trim_5p=0)
    assert out == records


def test_trimmed_meth_sources_match_their_untrimmed_siblings() -> None:
    """The trimmed sources differ from their siblings only by trim and prefix."""
    srcs = sample_sources("test-bucket")
    for trimmed, untrimmed, prefix in (
        (
            "meth-twist-emseq-trim-5M",
            "meth-twist-emseq-5M",
            "data/meth/twist-emseq/downsampled-5M-trim7/",
        ),
        ("smoke-meth-trim", "smoke-meth", "data/smoke-meth/10K-trim7/"),
    ):
        new, old = srcs[trimmed], srcs[untrimmed]
        assert new.trim_5p == 7  # noqa: PLR2004
        assert old.trim_5p == 0
        assert (new.source_r1, new.source_r2) == (old.source_r1, old.source_r2)
        assert new.downsample_every_nth == old.downsample_every_nth
        assert new.dest_prefix == prefix


def test_trimmed_sample_stages_to_its_own_file(monkeypatch, tmp_path) -> None:
    """The trimmed sample must never pick up the untrimmed sample's staged file."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=True)

    uploads = [c for c in calls if c[:3] == ["aws", "s3", "cp"]]
    assert [Path(u[3]).name for u in uploads] == [
        "twist-emseq_1.ds1500.trim7.fastq.gz",
        "twist-emseq_2.ds1500.trim7.fastq.gz",
    ]
    assert uploads[0][-1].endswith("data/smoke-meth/10K-trim7/r1.fq.gz")
    assert uploads[1][-1].endswith("data/smoke-meth/10K-trim7/r2.fq.gz")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("downsample_every_nth", 0),
        ("downsample_every_nth", -3),
        ("subsample_frac", 0.0),
        ("subsample_frac", 1.5),
    ],
)
def test_out_of_range_sampling_is_rejected(field: str, value: float) -> None:
    """A non-positive interval or a fraction outside (0, 1] is a configuration error."""
    with pytest.raises(ValueError, match=field):
        DataSource(sample="x", source_r1=Path("x.fastq.gz"), dest_prefix="d/", **{field: value})


def test_negative_trim_is_rejected() -> None:
    """A negative trim is a configuration error."""
    with pytest.raises(ValueError, match="trim_5p"):
        DataSource(sample="x", source_r1=Path("x.fastq.gz"), dest_prefix="d/", trim_5p=-1)


def test_a_failed_transform_leaves_no_staged_file(tmp_path: Path) -> None:
    """A retry skips the transform when the staged file exists, so a failure must
    not leave a partial one behind to be uploaded."""
    src = tmp_path / "in.fastq.gz"
    src.write_bytes(b"not gzip")
    dst = tmp_path / "out.fastq.gz"
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    result = subprocess.run(["bash", "-c", cmd], check=False)
    assert result.returncode != 0
    assert not dst.exists()


def test_a_failed_transform_removes_its_partial_file(tmp_path: Path) -> None:
    """A failed transform keeps no `.partial` behind to fill the disk."""
    src = tmp_path / "in.fastq.gz"
    src.write_bytes(b"not gzip")
    dst = tmp_path / "out.fastq.gz"
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    result = subprocess.run(["bash", "-c", cmd], check=False)
    assert result.returncode != 0
    assert set(tmp_path.iterdir()) == {src}


def test_a_retry_is_not_affected_by_a_stale_partial_file(tmp_path: Path) -> None:
    """A `.partial` left by a killed run is neither reused nor published."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=6)
    stale = tmp_path / "out.fastq.gz.stale.partial"
    stale.write_bytes(b"stale, truncated output")
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 2, 7)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert _read_fastq(dst) == [[h, q[7:], p, u[7:]] for h, q, p, u in records[::2]]
    # The run leaves only its published output behind (plus the untouched stale file).
    assert set(tmp_path.iterdir()) == {src, dst, stale}


def test_concurrent_transforms_use_distinct_partial_files(tmp_path: Path) -> None:
    """Two stagings of the same file must not write one shared `.partial`: one
    could publish it while the other is still writing."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=2_000)
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    procs = [subprocess.Popen(["bash", "-c", cmd]) for _ in range(4)]
    assert all(proc.wait() == 0 for proc in procs)
    assert _read_fastq(dst) == [[h, q[7:], p, u[7:]] for h, q, p, u in records]
    assert set(tmp_path.iterdir()) == {src, dst}
    assert "mktemp" in cmd


@pytest.mark.parametrize("content", [None, b"truncated, not gzip"])
def test_a_missing_or_corrupt_staged_file_is_regenerated(
    monkeypatch, tmp_path, content: bytes | None
) -> None:
    """Only a staged file that decompresses whole is reused; anything else is rebuilt."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    staged = tmp_path / "data/raw/vendor/twist-emseq_1.ds1500.trim7.fastq.gz"
    staged.parent.mkdir(parents=True)
    if content is not None:
        staged.write_bytes(content)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    transforms = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert len(transforms) == 2  # noqa: PLR2004 -- both mates rebuilt


@pytest.mark.parametrize("r2_content", [None, b"truncated, not gzip"])
def test_only_the_incomplete_mate_is_regenerated(
    monkeypatch, tmp_path, r2_content: bytes | None
) -> None:
    """Each mate is checked on its own: a valid R1 is reused while R2 is rebuilt."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    _write_fastq(vendor / "twist-emseq_1.ds1500.trim7.fastq.gz", n_records=2)
    if r2_content is not None:
        (vendor / "twist-emseq_2.ds1500.trim7.fastq.gz").write_bytes(r2_content)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    transforms = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert len(transforms) == 1
    assert "twist-emseq_2.fastq.gz" in transforms[0][2]
    assert "twist-emseq_1.fastq.gz" not in transforms[0][2]


def test_a_complete_staged_file_is_reused(monkeypatch, tmp_path) -> None:
    """A staged file that is valid gzip is uploaded without re-running the transform."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    for mate in (1, 2):
        _write_fastq(vendor / f"twist-emseq_{mate}.ds1500.trim7.fastq.gz", n_records=2)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    assert not [c for c in calls if c[0] == "bash"]
    assert len([c for c in calls if c[:3] == ["aws", "s3", "cp"]]) == 2  # noqa: PLR2004


def test_a_staged_file_older_than_its_source_is_regenerated(monkeypatch, tmp_path) -> None:
    """A source replaced at the same path invalidates its staged file, which still
    decompresses whole."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    for mate in (1, 2):
        _write_fastq(vendor / f"twist-emseq_{mate}.ds1500.trim7.fastq.gz", n_records=2)
        _write_fastq(vendor / f"twist-emseq_{mate}.fastq.gz", n_records=2)
        os.utime(vendor / f"twist-emseq_{mate}.ds1500.trim7.fastq.gz", ns=(10**18, 10**18))
    # R2's source is replaced after its staged file was built; R1's is not.
    os.utime(vendor / "twist-emseq_1.fastq.gz", ns=(10**18 - 1, 10**18 - 1))
    os.utime(vendor / "twist-emseq_2.fastq.gz", ns=(10**18 + 1, 10**18 + 1))
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    transforms = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert len(transforms) == 1
    assert "twist-emseq_2.fastq.gz" in transforms[0][2]


def test_both_mates_are_staged_before_either_is_uploaded(monkeypatch, tmp_path) -> None:
    """A failed R2 transform must not leave a new R1 in S3 beside the old R2."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    assert [c[0] for c in calls] == ["bash", "bash", "aws", "aws"]


@pytest.mark.parametrize("read_len", [0, 3, 7])
def test_a_read_left_empty_by_the_trim_fails_the_transform(tmp_path: Path, read_len: int) -> None:
    """A kept read with no bases after the trim would be staged with an empty SEQ."""
    src = tmp_path / "in.fastq.gz"
    _write_fastq(src, n_records=3)
    with gzip.open(src, "at") as fh:
        fh.write(f"@short\n{'A' * read_len}\n+\n{'I' * read_len}\n")
    dst = tmp_path / "out.fastq.gz"
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    result = subprocess.run(["bash", "-c", cmd], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "no bases left" in result.stderr
    assert set(tmp_path.iterdir()) == {src}


def test_a_read_one_base_longer_than_the_trim_is_kept(tmp_path: Path) -> None:
    """The bound is exclusive: one base left after the trim is a valid read."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=2, read_len=8)
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert _read_fastq(dst) == [[h, q[7:], p, u[7:]] for h, q, p, u in records]


def test_a_read_dropped_by_the_downsample_is_not_checked(tmp_path: Path) -> None:
    """Only the kept records must survive the trim."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=1)
    with gzip.open(src, "at") as fh:
        fh.write("@short\nACG\n+\nIII\n")
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 2, 7)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert _read_fastq(dst) == [[h, q[7:], p, u[7:]] for h, q, p, u in records]


def test_a_failed_publish_removes_the_partial_file_and_keeps_its_status(tmp_path: Path) -> None:
    """If the rename fails, the finished temp file must not be left to fill the disk."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_mv = bin_dir / "mv"
    fake_mv.write_text("#!/bin/sh\nexit 7\n")
    fake_mv.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    src = work / "in.fastq.gz"
    _write_fastq(src, n_records=2)
    cmd = _upload_data_mod.fastq_transform_cmd(src, work / "out.fastq.gz", 1, 7)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", cmd], check=False, env=env)
    assert result.returncode == 7  # noqa: PLR2004 -- the fake mv's status
    assert set(work.iterdir()) == {src}


def test_a_staged_file_with_an_empty_read_is_regenerated(monkeypatch, tmp_path) -> None:
    """A valid-gzip staged file holding an empty read (as an earlier transform
    could write) is rebuilt, not uploaded."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    _write_fastq(vendor / "twist-emseq_1.ds1500.trim7.fastq.gz", n_records=2)
    _write_fastq(vendor / "twist-emseq_2.ds1500.trim7.fastq.gz", n_records=2, read_len=0)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    transforms = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert len(transforms) == 1
    assert "twist-emseq_2.fastq.gz" in transforms[0][2]


def test_an_untrimmed_transform_passes_an_empty_read_through(tmp_path: Path) -> None:
    """The check guards the trim only; an untrimmed copy keeps its input as is."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=2, read_len=0)
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 0)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert _read_fastq(dst) == records


def test_an_untrimmed_staged_file_with_an_empty_read_is_reused(monkeypatch, tmp_path) -> None:
    """Rebuilding an untrimmed stage would reproduce the same empty read."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    for mate in (1, 2):
        _write_fastq(vendor / f"twist-emseq_{mate}.ds1500.fastq.gz", n_records=2, read_len=0)
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth", "test-bucket", dry_run=False)

    assert not [c for c in calls if c[0] == "bash"]


def _append_raw(path: Path, text: str) -> None:
    """Append ``text`` verbatim to the gzipped FASTQ at ``path``."""
    with gzip.open(path, "at") as fh:
        fh.write(text)


_MALFORMED_TAILS = {
    "incomplete": "@cut\nACGTACGTAC\n+\n",  # the stream ends before the QUAL line
    "qual_mismatch": "@odd\nACGTACGTAC\n+\nIIIIIIIII\n",  # 10 bases, 9 qualities
}


@pytest.mark.parametrize("kind", sorted(_MALFORMED_TAILS))
def test_a_malformed_kept_record_fails_the_trimmed_transform(tmp_path: Path, kind: str) -> None:
    """A gzip-valid source can still hold a record that is cut short or whose SEQ
    and QUAL disagree; the trim must fail rather than stage it."""
    src = tmp_path / "in.fastq.gz"
    _write_fastq(src, n_records=2)
    _append_raw(src, _MALFORMED_TAILS[kind])
    dst = tmp_path / "out.fastq.gz"
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    result = subprocess.run(["bash", "-c", cmd], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "record 3" in result.stderr
    assert set(tmp_path.iterdir()) == {src}


def test_a_malformed_record_dropped_by_the_downsample_is_not_checked(tmp_path: Path) -> None:
    """As with the empty-read check, only the kept records must be well formed."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    records = _write_fastq(src, n_records=1)
    _append_raw(src, _MALFORMED_TAILS["qual_mismatch"] + _MALFORMED_TAILS["incomplete"])
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 3, 7)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert _read_fastq(dst) == [[h, q[7:], p, u[7:]] for h, q, p, u in records]


def test_an_untrimmed_transform_passes_a_malformed_record_through(tmp_path: Path) -> None:
    """The check guards the trim only; an untrimmed copy keeps its input as is."""
    src = tmp_path / "in.fastq.gz"
    dst = tmp_path / "out.fastq.gz"
    _write_fastq(src, n_records=1)
    _append_raw(src, _MALFORMED_TAILS["qual_mismatch"])
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 0)
    subprocess.run(["bash", "-c", cmd], check=True)
    assert gzip.decompress(dst.read_bytes()) == gzip.decompress(src.read_bytes())


@pytest.mark.parametrize("kind", sorted(_MALFORMED_TAILS))
def test_a_trimmed_staged_file_with_a_malformed_record_is_regenerated(
    monkeypatch, tmp_path, kind: str
) -> None:
    """The reuse check applies the transform's record checks to a staged file."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    _write_fastq(vendor / "twist-emseq_1.ds1500.trim7.fastq.gz", n_records=2)
    r2 = vendor / "twist-emseq_2.ds1500.trim7.fastq.gz"
    _write_fastq(r2, n_records=2)
    _append_raw(r2, _MALFORMED_TAILS[kind])
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    transforms = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert len(transforms) == 1
    assert "twist-emseq_2.fastq.gz" in transforms[0][2]


def test_an_untrimmed_staged_file_with_a_malformed_record_is_reused(monkeypatch, tmp_path) -> None:
    """Rebuilding an untrimmed stage would copy the same record back."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    vendor = tmp_path / "data/raw/vendor"
    vendor.mkdir(parents=True)
    for mate in (1, 2):
        staged = vendor / f"twist-emseq_{mate}.ds1500.fastq.gz"
        _write_fastq(staged, n_records=2)
        _append_raw(staged, _MALFORMED_TAILS["qual_mismatch"])
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("smoke-meth", "test-bucket", dry_run=False)

    assert not [c for c in calls if c[0] == "bash"]


@pytest.mark.parametrize(
    ("sig", "status"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_a_signalled_transform_removes_its_partial_file(
    tmp_path: Path, sig: signal.Signals, status: int
) -> None:
    """A cancelled staging (Ctrl-C, a closed terminal, a scheduler's SIGTERM)
    must not leave its `.partial` behind for retries to pile up beside."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gunzip = bin_dir / "gunzip"  # stalls, so the run is caught mid-transform
    fake_gunzip.write_text("#!/bin/sh\nexec sleep 60\n")
    fake_gunzip.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    src = work / "in.fastq.gz"
    _write_fastq(src, n_records=2)
    cmd = _upload_data_mod.fastq_transform_cmd(src, work / "out.fastq.gz", 1, 7)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.Popen(["bash", "-c", cmd], start_new_session=True, env=env)
    try:
        deadline = time.monotonic() + 10
        while not list(work.glob("out.fastq.gz.partial.*")):
            assert time.monotonic() < deadline, "the transform never created its .partial"
            time.sleep(0.02)
        os.killpg(proc.pid, sig)
        assert proc.wait(timeout=10) == status
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert set(work.iterdir()) == {src}


def test_a_failed_transform_keeps_the_previous_stage(tmp_path: Path) -> None:
    """A rebuild that fails must leave the existing staged file byte-for-byte."""
    src = tmp_path / "in.fastq.gz"
    src.write_bytes(b"not gzip")
    dst = tmp_path / "out.fastq.gz"
    _write_fastq(dst, n_records=2)
    before = dst.read_bytes()
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    assert subprocess.run(["bash", "-c", cmd], check=False).returncode != 0
    assert dst.read_bytes() == before
    assert set(tmp_path.iterdir()) == {src, dst}


def test_a_failed_publish_keeps_the_previous_stage(tmp_path: Path) -> None:
    """If the rename fails, the existing staged file is neither replaced nor removed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_mv = bin_dir / "mv"
    fake_mv.write_text("#!/bin/sh\nexit 7\n")
    fake_mv.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    src = work / "in.fastq.gz"
    _write_fastq(src, n_records=2)
    dst = work / "out.fastq.gz"
    _write_fastq(dst, n_records=5)
    before = dst.read_bytes()
    cmd = _upload_data_mod.fastq_transform_cmd(src, dst, 1, 7)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    assert subprocess.run(["bash", "-c", cmd], check=False, env=env).returncode == 7  # noqa: PLR2004
    assert dst.read_bytes() == before
    assert set(work.iterdir()) == {src, dst}


def test_a_failed_r2_transform_uploads_nothing(monkeypatch, tmp_path) -> None:
    """The failure must propagate before either mate reaches S3."""
    monkeypatch.setattr(data_sources, "SCRATCH_ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], dry_run: bool = False) -> None:  # noqa: ARG001
        calls.append(list(cmd))
        if cmd[0] == "bash" and "twist-emseq_2.fastq.gz" in cmd[2]:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(_upload_data_mod, "run_cmd", fake_run_cmd)

    with pytest.raises(subprocess.CalledProcessError):
        _upload_sample("smoke-meth-trim", "test-bucket", dry_run=False)

    assert [c[0] for c in calls] == ["bash", "bash"]  # R1 staged, R2 attempted, no upload
