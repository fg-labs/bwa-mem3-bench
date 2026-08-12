"""Storage stack — S3 bucket + ECR repo + shared IAM roles."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

#: GitHub's OIDC issuer.
#:
#: The IAM identity provider for it is an ACCOUNT-LEVEL SINGLETON, and it cannot
#: be given a project-specific name: `CreateOpenIDConnectProvider` takes only a
#: URL, a client-id list and thumbprints, and the ARN is derived from the URL
#: (`.../oidc-provider/token.actions.githubusercontent.com`). So there is exactly
#: one of these per account no matter who creates it, and every project wanting
#: GitHub OIDC must REFERENCE it rather than declare its own.
#:
#: This stack creates it (see `github_oidc_provider` below) because the account
#: had none and nobody can add one by hand -- `iam:CreateOpenIDConnectProvider`
#: is denied to the human IAM users here, so CloudFormation, running as the CDK
#: exec role, is the only path. It carries RemovalPolicy.RETAIN precisely because
#: it is shared: destroying this project's storage stack must not delete the
#: trust anchor other projects' roles depend on.
GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"

#: Audience for the OIDC token. `sts.amazonaws.com` is what
#: `aws-actions/configure-aws-credentials` requests by default.
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"

#: The only subject allowed to assume the image-build role.
#:
#: Scoped to a GitHub ENVIRONMENT rather than to `ref:refs/heads/main`. Both pin
#: the role to this one repository; the difference is what else they allow.
#:
#: A ref pin means only a dispatch from `main` can mint credentials, so the
#: credentialed half of the workflow -- ECR login, the builds, the push, the
#: join -- cannot be exercised from a PR branch at all: STS refuses, and the
#: change has to be merged before it can be tested. An environment pin lets a
#: dispatch from ANY branch reach the role, but only after the environment's
#: required reviewer approves that specific run, so an unreviewed branch still
#: cannot push an image the benchmark fleet executes.
#:
#: The approval gate is therefore load-bearing, not decoration: without required
#: reviewers on the `image-build` environment this string would let any branch
#: push to ECR unattended. This repository is PUBLIC -- but a fork's pull request
#: carries `...:pull_request` as its subject and cannot match either form, and
#: pushing a branch here needs write access.
GITHUB_IMAGE_BUILD_ENVIRONMENT = "image-build"
GITHUB_IMAGE_BUILD_SUBJECT = f"repo:fg-labs/bwa-mem3-bench:environment:{GITHUB_IMAGE_BUILD_ENVIRONMENT}"


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
                        "Keep last 90 tagged images (~30 fg-labs SHAs of "
                        "history at 3 tags each, plus tier-suffixed variants)"
                    ),
                    tag_status=ecr.TagStatus.ANY,
                    # 90, not 30, because the CI build publishes THREE tags per
                    # SHA: the manifest list plus one per-architecture image
                    # (`<sha>-amd64`, `<sha>-arm64`). The per-arch tags cannot be
                    # removed after the join -- the manifest list references
                    # those images, and rule 1 above reaps untagged images after
                    # 7 days, so untagging them would break every image a week
                    # later. `tagStatus: ANY` counts images, so 30 would have cut
                    # real history from ~30 SHAs to ~10.
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

        # ── GitHub OIDC identity provider (account-level, shared) ────────────
        # Declared here because the account had none and no human can add one:
        # `iam:CreateOpenIDConnectProvider` is denied to the IAM users in this
        # account, so CloudFormation running as the CDK exec role is the only
        # available path. L1 (`CfnOIDCProvider`) rather than the L2 construct on
        # purpose -- the L2 provisions a Lambda-backed custom resource, and the
        # native CloudFormation type needs no such machinery.
        #
        # RETAIN because this resource is SHARED and unnameable: there is exactly
        # one GitHub OIDC provider per account (the ARN is derived from the issuer
        # URL), so any other project in this account must reference this one. A
        # `cdk destroy` of this project's storage stack must therefore not delete
        # it and break their roles' trust anchor.
        #
        # No ThumbprintList: IAM validates well-known OIDC issuers against its own
        # trust store and derives the thumbprint itself, so a hardcoded one here
        # would just be a rotation liability.
        self.github_oidc_provider = iam.CfnOIDCProvider(
            self,
            "GitHubOidcProvider",
            url=f"https://{GITHUB_OIDC_ISSUER}",
            client_id_list=[GITHUB_OIDC_AUDIENCE],
        )
        self.github_oidc_provider.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        # ── Image-build role (GitHub Actions, OIDC-federated) ────────────────
        # Assumed by `.github/workflows/build-image.yml` via
        # sts:AssumeRoleWithWebIdentity. No access keys exist for it: GitHub
        # mints a short-lived signed token describing the job, AWS verifies that
        # token against GitHub's published keys and returns ~1-hour credentials.
        # Nothing is stored in repository secrets, so there is nothing to leak
        # or rotate, and revocation is unilateral from this side.
        #
        # This is a SEPARATE role from `job_role` on purpose. Pushing to ECR
        # needs write permissions that the shared worker role must never hold:
        # every benchmark worker assumes `job_role`, whose boundary is pull-only,
        # so granting push there would let any rule's shell overwrite a released
        # image. The boundary below caps this role at ECR push plus read-only S3
        # even if an inline policy is added later.
        self.image_build_boundary = iam.ManagedPolicy(
            self,
            "BenchImageBuildBoundary",
            managed_policy_name=f"{project_name}-image-build-boundary",
            statements=[
                iam.PolicyStatement(
                    # GetAuthorizationToken is account-level and cannot be
                    # resource-scoped; the layer and manifest actions are scoped
                    # to the two repositories below.
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=[
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:CompleteLayerUpload",
                        "ecr:InitiateLayerUpload",
                        "ecr:PutImage",
                        "ecr:UploadLayerPart",
                        # Pull as well as push: the per-SHA build resolves
                        # `FROM ${BASE_IMAGE}` out of the base repository, and
                        # the manifest-list join reads back the per-arch images
                        # it is joining.
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    resources=[
                        self.ecr_repo.repository_arn,
                        self.base_ecr_repo.repository_arn,
                    ],
                ),
                iam.PolicyStatement(
                    actions=["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
                    resources=[self.bucket.bucket_arn, f"{self.bucket.bucket_arn}/*"],
                ),
            ],
        )

        self.image_build_role = iam.Role(
            self,
            "BenchImageBuildRole",
            role_name=f"{project_name}-image-build-role",
            # StringEquals, never StringLike: a wildcard here is the difference
            # between "one branch of one repo" and "any repository on GitHub".
            # Both claims are checked -- `aud` alone would authenticate the
            # issuer while saying nothing about WHO is asking.
            assumed_by=iam.WebIdentityPrincipal(
                self.github_oidc_provider.attr_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_ISSUER}:sub": GITHUB_IMAGE_BUILD_SUBJECT,
                        f"{GITHUB_OIDC_ISSUER}:aud": GITHUB_OIDC_AUDIENCE,
                    }
                },
            ),
            description=f"{project_name} GitHub Actions image builds",
            permissions_boundary=self.image_build_boundary,
            # Bounds the ROLE SESSION only, and an hour covers a cold base build
            # on a 4-vCPU hosted runner.
            #
            # It is NOT the lifetime of what a leaked build can use: the ECR
            # authorization token that `docker login` obtains is independently
            # valid for 12 hours and keeps working after this session expires.
            # So this value limits the window for re-assuming the role, not the
            # window for pushing images. Keeping credentials off the shell's
            # argv and out of interpolated strings is what actually bounds that
            # -- see the env-var handling in .github/workflows/build-image.yml.
            max_session_duration=cdk.Duration.hours(1),
        )
        self.ecr_repo.grant_pull_push(self.image_build_role)
        self.base_ecr_repo.grant_pull_push(self.image_build_role)

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
            "JobRoleArn",
            value=self.job_role.role_arn,
            export_name="BwaMem3BenchJobRoleArn",
        )
        # Consumed by .github/workflows/build-image.yml as `role-to-assume`.
        cdk.CfnOutput(
            self,
            "ImageBuildRoleArn",
            value=self.image_build_role.role_arn,
            export_name="BwaMem3BenchImageBuildRoleArn",
        )
        cdk.CfnOutput(
            self,
            "Region",
            value=self.region,
            export_name="BwaMem3BenchRegion",
        )
