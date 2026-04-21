"""CDK app entrypoint for bwa-mem3-bench."""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import Tags

from stacks import BatchStack, StorageStack


PROJECT_NAME = "bwa-mem3-bench"


def main() -> None:
    outdir = os.environ.get("CDK_OUTDIR", "cdk.out")
    app = cdk.App(outdir=outdir)
    env = cdk.Environment(region="us-east-1")

    # Optional cost-center tag applied to every resource and propagated to the
    # coordinator job definition so spawned worker jobs inherit it. Pass via
    #   cdk deploy -c cost_center="Your Cost Center"
    # or set CDK_CONTEXT_JSON='{"cost_center":"..."}' before invoking the app.
    cost_center = app.node.try_get_context("cost_center")

    storage = StorageStack(
        app,
        "BwaMem3BenchStorage",
        env=env,
        project_name=PROJECT_NAME,
        description=f"{PROJECT_NAME} S3 bucket + ECR repo + job role",
    )

    batch = BatchStack(
        app,
        "BwaMem3BenchBatch",
        env=env,
        project_name=PROJECT_NAME,
        job_role=storage.job_role,
        execution_role=storage.execution_role,
        instance_profile=storage.instance_profile,
        bucket_name=storage.bucket.bucket_name,
        ecr_repo_uri=storage.ecr_repo.repository_uri,
        cost_center=cost_center,
        description=f"{PROJECT_NAME} five spot compute envs + queues + coordinator",
    )

    for stack in (storage, batch):
        Tags.of(stack).add("Project", PROJECT_NAME)
        if cost_center:
            Tags.of(stack).add("Cost Center", cost_center)

    app.synth()


if __name__ == "__main__":
    main()
