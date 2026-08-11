"""Storage stack — S3 bucket + ECR repo + shared IAM roles."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct


class StorageStack(cdk.Stack):
    """Provisions:
    - One S3 bucket with lifecycle rules.
    - One ECR repo for the multi-arch benchmark image.
    - Job execution IAM role with read on bucket + read on ECR.
    - Separate ECS execution role for ECR pull + CloudWatch Logs.
    - EC2 instance role + instance profile for Batch compute environments.
    - Permission boundary managed policy scoping job-role permissions.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_name: str,
        bucket_name: str | None = None,
        ecr_name: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Per Fulcrum convention, every S3 bucket starts with `fg-`.
        # ECR repo name mirrors the project name (no `fg-` prefix needed).
        bucket_name = bucket_name or f"fg-{project_name}"
        ecr_name = ecr_name or project_name
        self.project_name = project_name

        self.bucket = s3.Bucket(
            self,
            "BenchBucket",
            bucket_name=bucket_name,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="runs-aligned-bam-lifecycle",
                    prefix="runs/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=cdk.Duration.days(30),
                        ),
                    ],
                    expiration=cdk.Duration.days(180),
                    tag_filters={"artifact": "aligned-bam"},
                ),
            ],
        )

        self.ecr_repo = ecr.Repository(
            self,
            "BenchEcr",
            repository_name=ecr_name,
            # `:latest` (overwritten on every push) is referenced by the coordinator
            # job definition, which requires the tag to be mutable. `:<sha>` tags
            # remain de-facto immutable because commit SHAs don't repeat.
            image_tag_mutability=ecr.TagMutability.MUTABLE,
            # Multi-arch manifest list pushes leave ~2 untagged sub-manifests
            # in ECR per release (one per platform — linux/amd64 + linux/arm64).
            # Reap those aggressively (rule 1) so they don't crowd out the
            # tagged retention budget (rule 2). Rule priority is stable: rule 1
            # matches untagged images first; rule 2 only sees what's left.
            lifecycle_rules=[
                ecr.LifecycleRule(
                    rule_priority=1,
                    description=(
                        "Expire untagged sub-manifests after 7 days "
                        "(per-platform pieces of multi-arch lists)"
                    ),
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=cdk.Duration.days(7),
                ),
                ecr.LifecycleRule(
                    rule_priority=2,
                    description=(
                        "Keep last 90 tagged images (~30 fg-labs SHAs of history: "
                        "each SHA can carry the manifest-list tag plus one "
                        "per-arch tag per platform, since Batch image builds push "
                        "<sha>-amd64 / <sha>-arm64 before they are joined)"
                    ),
                    tag_status=ecr.TagStatus.ANY,
                    max_image_count=90,
                ),
            ],
        )

        # Builder base image (docker/Dockerfile.base) — the toolchain, the
        # upstream bwa-mem2 build and the pinned cargo tools that `docker/
        # Dockerfile` starts FROM. It lives in its OWN repository rather than
        # sharing the one above, because that repo's rule 2 keeps only the last
        # 30 tagged images with `tagStatus: ANY`. A base image is rebuilt only
        # when a pin moves, so it is always among the oldest tags there and
        # would be evicted after ~30 SHA pushes — surfacing as `manifest
        # unknown` on the next cold build rather than at the moment of
        # deletion, long after the cause.
        #
        # Tags are content-addressed (`<upstream-tag>-<digest>`, see
        # bwa_mem3_bench/base_image.py), so a pin bump publishes a NEW tag and
        # leaves the old one intact — older SHA images stay rebuildable. That
        # is why retention here is generous and age-based rather than a tight
        # count: the whole value of the split is that these layers persist.
        self.base_ecr_repo = ecr.Repository(
            self,
            "BenchBaseEcr",
            repository_name=f"{ecr_name}-base",
            # Content-addressed tags never need overwriting; making them
            # immutable turns an accidental re-push of a changed recipe under
            # an existing tag into an error instead of a silent swap under
            # every image that already builds FROM it.
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    rule_priority=1,
                    description=(
                        "Expire untagged sub-manifests after 7 days "
                        "(per-platform pieces of multi-arch lists)"
                    ),
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=cdk.Duration.days(7),
                ),
                ecr.LifecycleRule(
                    rule_priority=2,
                    description="Keep tagged base images for a year",
                    tag_status=ecr.TagStatus.ANY,
                    max_image_age=cdk.Duration.days(365),
                ),
            ],
        )

        # ── Permission boundary ──────────────────────────────────────────────
        # Scopes the job role to only the permissions it actually needs,
        # preventing privilege escalation even if an inline policy is added.
        self.job_boundary = iam.ManagedPolicy(
            self,
            "BenchJobBoundary",
            managed_policy_name=f"{project_name}-job-boundary",
            statements=[
                iam.PolicyStatement(
                    actions=[
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:ListBucket",
                        "s3:DeleteObject",
                        "s3:GetBucketLocation",
                    ],
                    resources=[self.bucket.bucket_arn, f"{self.bucket.bucket_arn}/*"],
                ),
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/batch/*"
                    ],
                ),
                iam.PolicyStatement(
                    actions=[
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=[
                        "batch:SubmitJob",
                        "batch:DescribeJobs",
                        "batch:DescribeJobQueues",
                        "batch:DescribeJobDefinitions",
                        "batch:DescribeComputeEnvironments",
                        "batch:RegisterJobDefinition",
                        "batch:DeregisterJobDefinition",
                        "batch:TerminateJob",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[
                        f"arn:aws:iam::{self.account}:role/{project_name}-job-role"
                    ],
                ),
            ],
        )

        self.job_role = iam.Role(
            self,
            "BenchJobRole",
            role_name=f"{project_name}-job-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"{project_name} Batch task role",
            permissions_boundary=self.job_boundary,
        )
        # Bucket-wide read/write. Snakemake's storage plugin places the workflow
        # source archive at the bucket root (s3://<bucket>/snakemake-workflow-sources.*)
        # and rule outputs under their declared paths; granular per-prefix grants
        # are fragile — mirrors fgumi-benchmarks' simple `grant_read_write`.
        self.bucket.grant_read_write(self.job_role)
        self.ecr_repo.grant_pull(self.job_role)

        # ── Image-build role ────────────────────────────────────────────────
        # Batch image-build jobs (docker/batch-image-build.sh) need to PUSH to
        # ECR. The worker job role above deliberately cannot: its boundary grants
        # pull only, and every benchmark worker assumes it, so adding push there
        # would let any rule's shell overwrite a released image. A separate role
        # with a separate boundary keeps the push capability scoped to the jobs
        # that build images.
        #
        # It needs read-only S3 (fetch the build context) rather than the
        # read/write the worker role has -- a build job has no reason to write
        # benchmark outputs.
        self.image_build_boundary = iam.ManagedPolicy(
            self,
            "BenchImageBuildBoundary",
            managed_policy_name=f"{project_name}-image-build-boundary",
            statements=[
                iam.PolicyStatement(
                    actions=["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
                    resources=[self.bucket.bucket_arn, f"{self.bucket.bucket_arn}/*"],
                ),
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/batch/*"
                    ],
                ),
                iam.PolicyStatement(
                    actions=[
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                        "ecr:InitiateLayerUpload",
                        "ecr:UploadLayerPart",
                        "ecr:CompleteLayerUpload",
                        "ecr:PutImage",
                    ],
                    resources=["*"],
                ),
            ],
        )

        self.image_build_role = iam.Role(
            self,
            "BenchImageBuildRole",
            role_name=f"{project_name}-image-build-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"{project_name} Batch image-build task role (ECR push)",
            permissions_boundary=self.image_build_boundary,
        )
        self.bucket.grant_read(self.image_build_role)
        self.ecr_repo.grant_pull_push(self.image_build_role)
        self.base_ecr_repo.grant_pull_push(self.image_build_role)

        # The coordinator Batch job passes this role to its child jobs. AWS requires
        # an explicit iam:PassRole grant scoped to the role ARN being passed.
        self.job_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[self.job_role.role_arn],
            )
        )

        # Snakemake running inside the coordinator registers + submits Batch jobs.
        # Logs are scoped to the Batch log group (not wildcard *).
        self.job_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "batch:SubmitJob",
                    "batch:DescribeJobs",
                    "batch:DescribeJobQueues",
                    "batch:DescribeJobDefinitions",
                    "batch:DescribeComputeEnvironments",
                    "batch:RegisterJobDefinition",
                    "batch:DeregisterJobDefinition",
                    "batch:TerminateJob",
                ],
                resources=["*"],
            )
        )
        self.job_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/batch/*"
                ],
            )
        )

        # ── ECS execution role ───────────────────────────────────────────────
        # Separate from the job (task) role. Used by the ECS agent to pull
        # the container image from ECR and push logs to CloudWatch.
        self.execution_role = iam.Role(
            self,
            "BenchExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"{project_name} Batch ECS execution role",
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[self.ecr_repo.repository_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/batch/job:*"
                ],
            )
        )

        # ── EC2 instance role + profile ──────────────────────────────────────
        # Required by Batch compute environments; without an explicit instance
        # role CDK falls back to a default that may not have ECR pull rights.
        self.instance_role = iam.Role(
            self,
            "EcsInstanceRole",
            role_name=f"{project_name}-instance-role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonEC2ContainerServiceforEC2Role"
                ),
            ],
        )
        self.instance_profile = iam.CfnInstanceProfile(
            self,
            "EcsInstanceProfile",
            instance_profile_name=f"{project_name}-instance-profile",
            roles=[self.instance_role.role_name],
        )

        # ── CloudFormation outputs ───────────────────────────────────────────
        cdk.CfnOutput(
            self,
            "BucketName",
            value=self.bucket.bucket_name,
            export_name="BwaMem3BenchBucketName",
        )
        cdk.CfnOutput(
            self,
            "EcrRepositoryUri",
            value=self.ecr_repo.repository_uri,
            export_name="BwaMem3BenchEcrUri",
        )
        cdk.CfnOutput(
            self,
            "BaseEcrRepositoryUri",
            value=self.base_ecr_repo.repository_uri,
            export_name="BwaMem3BenchBaseEcrUri",
        )
        cdk.CfnOutput(
            self,
            "ImageBuildRoleArn",
            value=self.image_build_role.role_arn,
            export_name="BwaMem3BenchImageBuildRoleArn",
        )
        cdk.CfnOutput(
            self,
            "JobRoleArn",
            value=self.job_role.role_arn,
            export_name="BwaMem3BenchJobRoleArn",
        )
        cdk.CfnOutput(
            self,
            "Region",
            value=self.region,
            export_name="BwaMem3BenchRegion",
        )
