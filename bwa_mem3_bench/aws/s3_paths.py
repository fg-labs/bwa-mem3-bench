"""S3 key construction for bwa-mem3-bench artifacts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class S3Paths:
    """Builds consistent S3 URIs under a single bucket."""

    bucket: str

    def _prefix(self, path: str) -> str:
        return f"s3://{self.bucket}/{path}"

    def reference(self, name: str) -> str:
        return self._prefix(f"references/{name}/")

    def data(self, sample: str) -> str:
        return self._prefix(f"data/{sample}/")

    def baseline_bam(self, tool_version: str, sample: str, arch: str, rep: int = 1) -> str:
        return self._prefix(f"baseline/{tool_version}/{sample}/{arch}/rep-{rep}/aligned.bam")

    def baseline_timing(self, tool_version: str, sample: str, arch: str, rep: int) -> str:
        return self._prefix(
            f"baseline/{tool_version}/{sample}/{arch}/rep-{rep}/benchmarks/timing.tsv"
        )

    def golden_bam(self, sha: str, sample: str, arch: str) -> str:
        return self._prefix(f"golden/fg-labs-{sha}/{sample}/{arch}/aligned.bam")

    def run_dir(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self._prefix(f"runs/{sha}/{sample}/{arch}/rep-{rep}/")

    def run_aligned_bam(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self.run_dir(sha, sample, arch, rep) + "aligned.bam"

    def run_timing(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self.run_dir(sha, sample, arch, rep) + "benchmarks/timing.tsv"

    def run_meta(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self.run_dir(sha, sample, arch, rep) + "benchmarks/meta.json"

    def run_compare_baseline(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self.run_dir(sha, sample, arch, rep) + "compare/vs-baseline.json"

    def run_compare_golden(self, sha: str, sample: str, arch: str, rep: int) -> str:
        return self.run_dir(sha, sample, arch, rep) + "compare/vs-golden.json"
