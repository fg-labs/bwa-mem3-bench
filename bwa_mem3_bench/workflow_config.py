"""Load samples + archs + defaults from YAML into typed records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Sample:
    name: str
    baseline_tool: str
    reference: str
    source: str
    fg_labs_flags: list[str] = field(default_factory=list)
    compare_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Arch:
    name: str
    instance_type: str
    batch_queue: str
    simd: str
    platform: str
    # fg-labs/bwa-mem3 BASELINE_ARCH build-arg for this arch's image. Empty
    # string means "no override" (use the upstream default). See
    # config/archs.yaml for rationale.
    baseline_arch: str = ""


@dataclass(frozen=True)
class WorkflowConfig:
    samples: dict[str, Sample]
    archs: dict[str, Arch]
    core_arch: str
    full_archs: list[str]
    region: str
    bucket: str
    ecr_repo: str
    upstream_tag: str
    bwameth_version: str
    bwa_version: str
    threads: int
    reps_default: int
    reps_baseline: int
    references: dict[str, dict[str, str]]
    runs_prefix: str
    baseline_prefix: str
    golden_prefix: str
    data_prefix: str


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r") as fh:
        result: dict[str, Any] = yaml.safe_load(fh)
        return result


def load_config(config_dir: Path) -> WorkflowConfig:
    """Load and validate the three-file config into a `WorkflowConfig`."""
    samples_raw = _read_yaml(config_dir / "samples.yaml")["samples"]
    archs_raw = _read_yaml(config_dir / "archs.yaml")
    defaults = _read_yaml(config_dir / "defaults.yaml")

    samples = {}
    for name, data in samples_raw.items():
        source = data["source"]
        if source.startswith("s3://"):
            raise ValueError(
                f"sample {name!r} `source` must be a bucket-relative key prefix "
                f"(e.g. `data/wgs/HG00096/`), not a full S3 URI; got {source!r}. "
                f"The S3 bucket comes from defaults.yaml or BWA_MEM3_BENCH_S3_BUCKET."
            )
        samples[name] = Sample(
            name=name,
            baseline_tool=data["baseline_tool"],
            reference=data["reference"],
            source=source,
            fg_labs_flags=list(data.get("fg_labs_flags", [])),
            compare_options=dict(data.get("compare_options", {})),
        )

    archs = {
        name: Arch(
            name=name,
            instance_type=data["instance_type"],
            batch_queue=data["batch_queue"],
            simd=data["simd"],
            platform=data["platform"],
            baseline_arch=str(data.get("baseline_arch", "")),
        )
        for name, data in archs_raw["archs"].items()
    }

    return WorkflowConfig(
        samples=samples,
        archs=archs,
        core_arch=archs_raw["core_arch"],
        full_archs=list(archs_raw["full_archs"]),
        region=defaults["region"],
        bucket=defaults["bucket"],
        ecr_repo=defaults["ecr_repo"],
        upstream_tag=defaults["upstream_tag"],
        bwameth_version=defaults["bwameth_version"],
        bwa_version=defaults["bwa_version"],
        threads=int(defaults["threads"]),
        reps_default=int(defaults["reps_default"]),
        reps_baseline=int(defaults["reps_baseline"]),
        references=defaults["references"],
        runs_prefix=defaults["runs_prefix"],
        baseline_prefix=defaults["baseline_prefix"],
        golden_prefix=defaults["golden_prefix"],
        data_prefix=defaults["data_prefix"],
    )
