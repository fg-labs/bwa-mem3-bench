"""Load AWS infrastructure config from `cdk/outputs.json` or env vars."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from bwa_mem3_bench import REPO_ROOT

_OUTPUTS_JSON = REPO_ROOT / "cdk" / "outputs.json"


@dataclass(frozen=True)
class AwsConfig:
    region: str
    bucket: str
    ecr_repo_uri: str
    job_role_arn: str
    coordinator_queue: str
    coordinator_job_definition: str
    worker_queues: tuple[str, ...]


def _read_outputs() -> dict[str, dict[str, str]]:
    if not _OUTPUTS_JSON.exists():
        return {}
    raw: dict[str, Any] = json.loads(_OUTPUTS_JSON.read_text())
    return {k: dict(v) for k, v in raw.items()}


def load() -> AwsConfig:
    """Load from `cdk/outputs.json`; fall back to env vars where present."""
    outputs = _read_outputs()
    storage = outputs.get("BwaMem3BenchStorage", {})
    batch = outputs.get("BwaMem3BenchBatch", {})

    def _get(key: str, env: str, default: str = "") -> str:
        return str(storage.get(key) or batch.get(key) or os.environ.get(env, default))

    region = _get("Region", "AWS_REGION", "us-east-1")
    bucket = _get("BucketName", "BWA_MEM3_BENCH_S3_BUCKET", "bwa-mem3-bench")
    ecr_repo_uri = _get("EcrRepositoryUri", "BWA_MEM3_BENCH_ECR_REPO")
    job_role_arn = _get("JobRoleArn", "SNAKEMAKE_AWS_BATCH_JOB_ROLE")
    coordinator_queue = _get(
        "CoordinatorQueueName",
        "BWA_MEM3_BENCH_COORDINATOR_QUEUE",
        "bwa-mem3-bench-coordinator",
    )
    coordinator_job_def = _get(
        "CoordinatorJobDefinitionName",
        "BWA_MEM3_BENCH_COORDINATOR_JOB_DEF",
        "bwa-mem3-bench-coordinator",
    )

    project_name = "bwa-mem3-bench"
    # Must stay in sync with cdk/stacks/batch_stack.py ARCHS tuple — kill-all
    # and watch iterate this to enumerate project-owned Batch queues.
    archs = ("c8g", "c7g", "c6a", "c7i", "c7a", "m7i")
    worker_queues = tuple(
        batch.get(f"Queue{arch[0].upper()}{arch[1:].upper()}") or f"{project_name}-{arch}"
        for arch in archs
    )

    return AwsConfig(
        region=region,
        bucket=bucket,
        ecr_repo_uri=ecr_repo_uri,
        job_role_arn=job_role_arn,
        coordinator_queue=coordinator_queue,
        coordinator_job_definition=coordinator_job_def,
        worker_queues=worker_queues,
    )
