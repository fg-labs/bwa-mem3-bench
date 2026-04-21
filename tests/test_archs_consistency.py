"""Ensure config/archs.yaml and cdk/stacks/batch_stack.py agree on the arch list."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Make cdk/ importable so we can import stacks.batch_stack directly.
sys.path.insert(0, str(REPO_ROOT / "cdk"))

from stacks.batch_stack import ARCHS as STACK_ARCHS  # noqa: E402

from bwa_mem3_bench.workflow_config import load_config  # noqa: E402


def test_archs_match_batch_stack() -> None:
    cfg = load_config(REPO_ROOT / "config")
    yaml_archs = set(cfg.archs.keys())
    stack_archs = {spec.arch_key for spec in STACK_ARCHS}
    assert yaml_archs == stack_archs, (
        f"config/archs.yaml has {yaml_archs}; batch_stack ARCHS has {stack_archs}"
    )
