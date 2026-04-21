"""Unit tests for S3 path construction."""

from bwa_mem3_bench.aws.s3_paths import S3Paths

BUCKET = "example-bucket"


def test_reference_path() -> None:
    p = S3Paths(bucket=BUCKET)
    assert p.reference("hs38DH") == f"s3://{BUCKET}/references/hs38DH/"


def test_data_path() -> None:
    p = S3Paths(bucket=BUCKET)
    assert p.data("wgs-60M") == f"s3://{BUCKET}/data/wgs-60M/"


def test_baseline_bam() -> None:
    p = S3Paths(bucket=BUCKET)
    # default rep is 1
    assert p.baseline_bam(tool_version="bwa-mem2-v2.2.1", sample="wgs-60M", arch="c7g") == (
        f"s3://{BUCKET}/baseline/bwa-mem2-v2.2.1/wgs-60M/c7g/rep-1/aligned.bam"
    )
    # explicit rep
    assert p.baseline_bam(tool_version="bwa-mem2-v2.2.1", sample="wgs-60M", arch="c7g", rep=3) == (
        f"s3://{BUCKET}/baseline/bwa-mem2-v2.2.1/wgs-60M/c7g/rep-3/aligned.bam"
    )


def test_golden_bam() -> None:
    p = S3Paths(bucket=BUCKET)
    key = p.golden_bam(sha="abc1234", sample="wgs-60M", arch="c7g")
    assert key == f"s3://{BUCKET}/golden/fg-labs-abc1234/wgs-60M/c7g/aligned.bam"


def test_run_dir_and_artifacts() -> None:
    p = S3Paths(bucket=BUCKET)
    base = p.run_dir(sha="abc1234", sample="wes-30M", arch="c6a", rep=3)
    assert base == f"s3://{BUCKET}/runs/abc1234/wes-30M/c6a/rep-3/"
    assert p.run_aligned_bam("abc1234", "wes-30M", "c6a", 3) == base + "aligned.bam"
    assert p.run_timing("abc1234", "wes-30M", "c6a", 3) == base + "benchmarks/timing.tsv"
    assert p.run_meta("abc1234", "wes-30M", "c6a", 3) == base + "benchmarks/meta.json"
    assert p.run_compare_baseline("abc1234", "wes-30M", "c6a", 3) == (
        base + "compare/vs-baseline.json"
    )
    assert p.run_compare_golden("abc1234", "wes-30M", "c6a", 3) == (base + "compare/vs-golden.json")
