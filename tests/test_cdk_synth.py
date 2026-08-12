"""Smoke-tests CDK synth: both stacks emit templates with key resources."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_DIR = REPO_ROOT / "cdk"

# 7 per-arch queues + 1 coordinator. The per-arch set is the 6 sweep archs
# (c8g, c7g, c6a, c7i, c7a, m7i) plus c8g64 — the 64-vCPU Graviton4 host the
# thread-scaling ladder runs on, which is intentionally NOT in `full_archs`.
EXPECTED_BATCH_QUEUE_COUNT = 8

# A container is one hop further from IMDS than its host, so the default of 1
# drops the IMDSv2 token PUT; 2 is AWS's documented minimum for containers.
EXPECTED_IMDS_HOP_LIMIT = 2

# The ONLY GitHub Actions subject allowed to assume the image-build role. Written
# out literally rather than imported from the stack: the point of the assertion is
# to pin this exact value, and a test that reads the constant it is checking would
# happily follow it wherever it moved.
EXPECTED_OIDC_SUBJECT = "repo:fg-labs/bwa-mem3-bench:environment:image-build"


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


def test_image_build_role_is_pinned_to_one_repository_and_ref(tmp_path: Path) -> None:
    """The single most dangerous thing in the GitHub Actions build path.

    This repository is PUBLIC and the role can push to ECR. A `sub` condition that
    wildcards the repository (`repo:fg-labs/*`), or a trust policy that checks only
    `aud`, lets ANY repository on GitHub — anyone's, worldwide — assume the role and
    overwrite our images. A fork's pull request carries `...:pull_request` as its
    subject and cannot match this form either.

    The subject is scoped to a GitHub ENVIRONMENT, which trades the ref pin for an
    approval gate: any branch may dispatch, but the environment's required
    reviewer must approve the run before credentials are minted. That gate is
    therefore load-bearing — `test_the_image_build_environment_requires_approval`
    pins it, and without it this subject would let any branch push unattended.

    Asserted on the synthesized template rather than the construct call, because
    what protects the account is the policy CloudFormation actually deploys.
    """
    out_dir = tmp_path / "cdk.out"
    _synth(out_dir)
    template = json.loads((out_dir / "BwaMem3BenchStorage.template.json").read_text())

    roles = [
        r["Properties"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::IAM::Role"
        and r["Properties"].get("RoleName") == "bwa-mem3-bench-image-build-role"
    ]
    assert len(roles) == 1, "expected exactly one image-build role"
    statements = roles[0]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1, f"exactly one trust statement expected, got {statements}"
    statement = statements[0]

    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Effect"] == "Allow"
    assert "Federated" in statement["Principal"], (
        "the role must be assumable only via the OIDC provider, not by a service or account"
    )

    equals = statement["Condition"]["StringEquals"]
    assert equals["token.actions.githubusercontent.com:sub"] == EXPECTED_OIDC_SUBJECT
    assert equals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert "StringLike" not in statement["Condition"], (
        "a StringLike subject condition permits wildcards; this must be an exact match"
    )


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


def test_the_image_build_environment_requires_approval() -> None:
    """The approval gate is the only thing the environment-scoped subject leans on.

    Scoping the trust policy to an environment rather than to `refs/heads/main`
    lets ANY branch dispatch the workflow — which is the point, since it makes the
    credentialed half testable from a PR. What keeps that from also letting an
    unreviewed branch push images the benchmark fleet executes is the
    environment's `required_reviewers` rule. Remove the reviewers and the subject
    silently becomes "any branch, unattended".

    Queried live rather than from config: the gate lives in GitHub's settings, not
    in this repository, so nothing in the tree would show its removal. Skipped
    when `gh` cannot reach the API, so the suite still runs offline.
    """
    probe = subprocess.run(
        [
            "gh",
            "api",
            "/repos/fg-labs/bwa-mem3-bench/environments/image-build",
            "--jq",
            '[.protection_rules[]?|select(.type=="required_reviewers")]|length',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"cannot query the environment: {probe.stderr.strip()[:120]}")
    assert probe.stdout.strip() == "1", (
        "the `image-build` environment has no required_reviewers rule, so the "
        "OIDC subject pinned to it would let any branch push to ECR unattended"
    )
