"""Unit tests for the upload-data SE/PE staging branches."""

from bwa_mem3_bench import data_sources

# `bwa_mem3_bench.commands.__init__` re-exports the `upload_data` *function* under
# the same name as the submodule, shadowing it in the package namespace.  Use
# importlib to get the actual module object so monkeypatch can find `run_cmd`.
from bwa_mem3_bench.commands import _upload_data as _upload_data_mod

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

    _upload_sample("meth-twist-emseq-5M", "test-bucket", dry_run=True)

    pipes = [c for c in calls if c[0] == "bash" and "gunzip" in c[2]]
    assert pipes, f"expected a gunzip downsample call; got {calls}"
    assert all(c[2].startswith("set -o pipefail;") for c in pipes)


def test_hic_pe_uploads_two_fastqs_no_conversion(monkeypatch) -> None:
    calls = _capture_run_cmd(monkeypatch)

    _upload_sample("hic-1M", "test-bucket", dry_run=True)

    assert not any("samtools" in part for c in calls for part in c)
    uploads = [c for c in calls if c[:3] == ["aws", "s3", "cp"]]
    assert [u[-1].rsplit("/", 1)[-1] for u in uploads] == ["r1.fq.gz", "r2.fq.gz"]
