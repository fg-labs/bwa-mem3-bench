"""Unit tests for the data_sources staging map."""

from bwa_mem3_bench import data_sources
from bwa_mem3_bench.data_sources import DataSource, sample_sources


def test_hic_entry_is_paired_with_two_sources() -> None:
    src = sample_sources("test-bucket")["hic-1M"]
    assert src.source_r2 is not None
    assert src.source_bam is None
    assert src.dest_prefix == "data/hic/hg002-1M/"
    assert src.source_r1.name == "HG002.HiC-1M_1.fq.gz"
    assert src.source_r2.name == "HG002.HiC-1M_2.fq.gz"


def test_sbx_entry_is_single_end_from_bam() -> None:
    src = sample_sources("test-bucket")["sbx-1M"]
    assert src.source_r2 is None
    assert src.source_bam is not None
    assert src.source_bam.name == "HG002.bam"
    assert src.subsample_frac == 0.001166  # noqa: PLR2004
    assert src.dest_prefix == "data/sbx/hg002-1M/"


def test_datasource_allows_single_end_without_r2() -> None:
    ds = DataSource(
        sample="x",
        source_r1=data_sources.STAGE_ROOT / "x.fq.gz",
        dest_prefix="data/x/",
        source_bam=data_sources.SBX_ROOT / "x.bam",
        subsample_frac=0.001,
    )
    assert ds.source_r2 is None
