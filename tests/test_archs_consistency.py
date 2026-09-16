"""Ensure config/archs.yaml and cdk/stacks/batch_stack.py agree on the arch list."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Make cdk/ importable so we can import stacks.batch_stack directly.
sys.path.insert(0, str(REPO_ROOT / "cdk"))

from stacks.batch_stack import ARCHS as STACK_ARCHS  # noqa: E402
from stacks.batch_stack import ARENA_ARCHS as STACK_ARENA_ARCHS  # noqa: E402

from bwa_mem3_bench.aws_config import load as load_aws_config  # noqa: E402
from bwa_mem3_bench.workflow_config import load_config  # noqa: E402


def test_archs_match_batch_stack() -> None:
    cfg = load_config(REPO_ROOT / "config")
    yaml_archs = set(cfg.archs.keys())
    stack_archs = {spec.arch_key for spec in STACK_ARCHS}
    assert yaml_archs == stack_archs, (
        f"config/archs.yaml has {yaml_archs}; batch_stack ARCHS has {stack_archs}"
    )


def test_aws_config_arch_tuples_match_batch_stack() -> None:
    """`aws_config`'s `archs` / `arena_archs` are hand-maintained tuples that
    drive `aws kill-all` / `jobs` / `cost`; a queue missing from them keeps
    running (and billing) past a bulk-terminate. Their own comments say "MUST
    stay in sync with ARCHS / ARENA_ARCHS in cdk/stacks/batch_stack.py", so pin
    it -- the same guard `test_archs_match_batch_stack` gives the CDK<->YAML
    pair. `arena_archs` is derived from `arena_queues`, which is what the config
    actually exposes."""
    aws = load_aws_config()
    stack_archs = {spec.arch_key for spec in STACK_ARCHS}
    aws_archs = {q.rsplit("-", 1)[-1] for q in aws.worker_queues}
    assert aws_archs == stack_archs, (
        f"aws_config worker queues cover {aws_archs}; batch_stack ARCHS has {stack_archs}"
    )
    stack_arena = {spec.arch_key for spec in STACK_ARENA_ARCHS}
    aws_arena = {q.rsplit("-arena", 1)[0].rsplit("-", 1)[-1] for q in aws.arena_queues}
    assert aws_arena == stack_arena, (
        f"aws_config arena queues cover {aws_arena}; batch_stack ARENA_ARCHS has {stack_arena}"
    )
