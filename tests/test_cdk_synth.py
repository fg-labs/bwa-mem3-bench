"""Smoke-tests CDK synth: both stacks emit templates with key resources."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_DIR = REPO_ROOT / "cdk"

# 7 per-arch queues + 1 coordinator. The per-arch set is the 6 sweep archs
# (c8g, c7g, c6a, c7i, c7a, m7i) plus c8g64 — the 64-vCPU Graviton4 host the
# thread-scaling ladder runs on, which is intentionally NOT in `full_archs`.
EXPECTED_BATCH_QUEUE_COUNT = 8

# A container is one hop further from IMDS than its host, so the default of 1
# drops the IMDSv2 token PUT; 2 is AWS's documented minimum for containers.
EXPECTED_IMDS_HOP_LIMIT = 2


def _synth(out_dir: Path) -> None:
    """Invoke `python app.py` from the cdk dir with CDK_OUTDIR override."""
    env = {**os.environ, "CDK_OUTDIR": str(out_dir)}
    subprocess.run(
        ["python", "app.py"],
        cwd=CDK_DIR,
        check=True,
        env=env,
    )


def test_cdk_synth_produces_both_stacks(tmp_path: Path) -> None:
    out_dir = tmp_path / "cdk.out"
    _synth(out_dir)
    templates = sorted(out_dir.glob("*.template.json"))
    names = {t.name for t in templates}
    assert "BwaMem3BenchStorage.template.json" in names
    assert "BwaMem3BenchBatch.template.json" in names


def test_storage_stack_has_bucket_and_ecr(tmp_path: Path) -> None:
    out_dir = tmp_path / "cdk.out"
    _synth(out_dir)
    template = json.loads((out_dir / "BwaMem3BenchStorage.template.json").read_text())
    resource_types = {r["Type"] for r in template["Resources"].values()}
    assert "AWS::S3::Bucket" in resource_types
    assert "AWS::ECR::Repository" in resource_types


def test_batch_stack_has_a_queue_per_arch(tmp_path: Path) -> None:
    """One worker queue per configured arch (incl. the c8g64 scaling host) + coordinator."""
    out_dir = tmp_path / "cdk.out"
    _synth(out_dir)
    template = json.loads((out_dir / "BwaMem3BenchBatch.template.json").read_text())
    queues = [r for r in template["Resources"].values() if r["Type"] == "AWS::Batch::JobQueue"]
    assert len(queues) == EXPECTED_BATCH_QUEUE_COUNT


def test_launch_template_reaches_imds_from_the_container_with_v2_required(tmp_path: Path) -> None:
    """Workers need IMDS from inside the container, and only via IMDSv2.

    The hop limit of 2 is what lets `emit-host-meta` obtain a token at all (at
    the default of 1 the token PUT is dropped before it arrives, so host
    attribution silently degrades to "unknown"). Requiring tokens is what keeps
    that widened reach from also exposing the instance role's credentials to an
    unauthenticated IMDSv1 GET from inside the container.
    """
    out_dir = tmp_path / "cdk.out"
    _synth(out_dir)
    template = json.loads((out_dir / "BwaMem3BenchBatch.template.json").read_text())
    templates = [
        r for r in template["Resources"].values() if r["Type"] == "AWS::EC2::LaunchTemplate"
    ]
    assert len(templates) == 1, "expected exactly one worker launch template"
    metadata = templates[0]["Properties"]["LaunchTemplateData"]["MetadataOptions"]
    assert metadata["HttpPutResponseHopLimit"] == EXPECTED_IMDS_HOP_LIMIT
    assert metadata["HttpTokens"] == "required"
