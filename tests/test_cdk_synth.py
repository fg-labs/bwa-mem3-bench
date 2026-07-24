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
