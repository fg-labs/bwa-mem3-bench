"""Batch stack — five worker compute environments + queues, plus coordinator."""

from __future__ import annotations

from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_batch as batch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from constructs import Construct


@dataclass(frozen=True)
class ArchSpec:
    logical_id: str      # camelcase for CDK construct ids
    arch_key: str        # short arch key, matches config/archs.yaml (c7g, c6a, …)
    instance_type: str   # e.g. c7g.4xlarge
    platform: str        # linux/amd64 or linux/arm64


ARCHS: tuple[ArchSpec, ...] = (
    ArchSpec("C8g", "c8g", "c8g.4xlarge", "linux/arm64"),
    ArchSpec("C7g", "c7g", "c7g.4xlarge", "linux/arm64"),
    ArchSpec("C6a", "c6a", "c6a.4xlarge", "linux/amd64"),
    ArchSpec("C7i", "c7i", "c7i.4xlarge", "linux/amd64"),
    ArchSpec("C7a", "c7a", "c7a.4xlarge", "linux/amd64"),
    # m7i.4xlarge: general-purpose Sapphire Rapids, 16 vCPU / 64 GB. Same CPU
    # microarchitecture as c7i (AVX-512BW) so m7i is the apples-to-apples
    # counterpart to c7i for meth (which needs ~50 GB resident for the doubled
    # .bwameth.c2t FMI and doesn't fit on c7i.4xlarge's 32 GB host). m7i also
    # runs non-meth samples so meth-vs-non-meth wall times are comparable on
    # identical hardware. Replaces earlier r7i.4xlarge choice — r7i was noisier
    # (σ/μ up to 55%) and the extra 128 GB was unused.
    ArchSpec("M7i", "m7i", "m7i.4xlarge", "linux/amd64"),
)


class BatchStack(cdk.Stack):
    """Provisions five spot compute environments + queues (one per arch) plus a coordinator.

    Queue names are prefixed with the project name so they cannot collide with
    any other Batch project (e.g. `bwa-mem3-bench-c8g` not bare `bench-c8g`).
    The coordinator queue/job-def are named `<project>-coordinator` and run
    snakemake orchestration inside a container rather than on the developer's laptop.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_name: str,
        job_role: iam.IRole,
        execution_role: iam.IRole,
        instance_profile: iam.CfnInstanceProfile,
        bucket_name: str,
        ecr_repo_uri: str,
        cost_center: str | None = None,
        max_vcpus: int = 256,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name

        vpc = ec2.Vpc(
            self,
            "BenchVpc",
            max_azs=3,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        security_group = ec2.SecurityGroup(
            self,
            "BenchSecurityGroup",
            vpc=vpc,
            allow_all_outbound=True,
            description=f"{project_name} Batch compute environment security group",
        )

        # ── Launch template with high EBS throughput for BAM-heavy I/O ──────
        # Worker instances write and read large BAM files; gp3 at 500 MB/s
        # avoids I/O becoming the bottleneck. Do NOT attach to the coordinator
        # (no heavy I/O; coordinator only submits child jobs).
        launch_template = ec2.LaunchTemplate(
            self,
            "BenchLaunchTemplate",
            launch_template_name=f"{project_name}-launch-template",
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=300,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        throughput=500,  # MB/s — gp3 baseline is 125; boost for BAM I/O
                        iops=3000,
                        delete_on_termination=True,
                    ),
                )
            ],
        )
        cdk.Tags.of(launch_template).add("Project", project_name)
        if cost_center:
            cdk.Tags.of(launch_template).add("Cost Center", cost_center)

        # instance_role in ManagedEc2EcsComputeEnvironment accepts an IRole that
        # already has the AmazonEC2ContainerServiceforEC2Role managed policy attached.
        # We pass the instance_role from the storage stack; the CfnInstanceProfile
        # is used when constructing L1 compute environments — for L2 we need the role.
        # Extract the role name from the profile to reconstruct an IRole reference.
        instance_role = iam.Role.from_role_arn(
            self,
            "ImportedInstanceRole",
            f"arn:aws:iam::{self.account}:role/{project_name}-instance-role",
        )

        self.queues: dict[str, batch.IJobQueue] = {}
        for spec in ARCHS:
            compute_env = batch.ManagedEc2EcsComputeEnvironment(
                self,
                f"BenchCe{spec.logical_id}",
                vpc=vpc,
                instance_types=[ec2.InstanceType(spec.instance_type)],
                # Without this, Batch appends "optimal" (m/c/r) classes which
                # conflict with ARM-only environments.
                use_optimal_instance_classes=False,
                allocation_strategy=batch.AllocationStrategy.SPOT_PRICE_CAPACITY_OPTIMIZED,
                spot=True,
                maxv_cpus=max_vcpus,
                minv_cpus=0,
                instance_role=instance_role,
                launch_template=launch_template,
                security_groups=[security_group],
            )
            queue_name = f"{project_name}-{spec.arch_key}"
            queue = batch.JobQueue(
                self,
                f"BenchQueue{spec.logical_id}",
                job_queue_name=queue_name,
                compute_environments=[
                    batch.OrderedComputeEnvironment(compute_environment=compute_env, order=1)
                ],
            )
            self.queues[spec.arch_key] = queue

        # ── Coordinator compute environment ──────────────────────────────────
        # Runs snakemake orchestration inside a container. On-demand (not spot):
        # the coordinator is the single long-lived orchestrator for the whole
        # benchmark, so a spot reclaim mid-run kills snakemake and forces a
        # resume. At c6a.large/xlarge on-demand (~$0.08-0.15/hr) the premium over
        # spot is ~20-40¢ per multi-hour sweep — negligible against the worker
        # fleet, and well worth the reliability. Multi-instance-type
        # (c6a.large + c6a.xlarge) improves on-demand availability without
        # overprovisioning. No launch template — coordinator has no heavy I/O.
        coordinator_ce = batch.ManagedEc2EcsComputeEnvironment(
            self,
            "CoordinatorCe",
            vpc=vpc,
            instance_types=[ec2.InstanceType("c6a.large"), ec2.InstanceType("c6a.xlarge")],
            use_optimal_instance_classes=False,
            allocation_strategy=batch.AllocationStrategy.BEST_FIT_PROGRESSIVE,
            spot=False,
            maxv_cpus=8,
            minv_cpus=0,
            instance_role=instance_role,
            security_groups=[security_group],
        )

        coordinator_queue_name = f"{project_name}-coordinator"
        coordinator_queue = batch.JobQueue(
            self,
            "CoordinatorQueue",
            job_queue_name=coordinator_queue_name,
            compute_environments=[
                batch.OrderedComputeEnvironment(
                    compute_environment=coordinator_ce, order=1
                )
            ],
        )

        # ── Coordinator job definition ────────────────────────────────────────
        # Command is overridden at submit time. Environment variables baked in
        # here let snakemake-executor-plugin-aws-batch find the region, queue,
        # and role without any config file changes between runs.
        # Image defaults to :latest; override image_tag per submit via
        #   aws batch submit-job --container-overrides '{"environment":[...]}'
        batch.CfnJobDefinition(
            self,
            "CoordinatorJobDef",
            job_definition_name=coordinator_queue_name,
            type="container",
            platform_capabilities=["EC2"],
            timeout=batch.CfnJobDefinition.TimeoutProperty(
                attempt_duration_seconds=43200,  # 12 h — benchmarks can take many hours
            ),
            container_properties=batch.CfnJobDefinition.ContainerPropertiesProperty(
                image=f"{ecr_repo_uri}:latest",
                job_role_arn=job_role.role_arn,
                # Separate execution role for ECR pull + CloudWatch Logs.
                execution_role_arn=execution_role.role_arn,
                vcpus=2,
                # c6a.large has 4 GiB total; ECS agent reserves ~300 MB. Leave
                # some headroom for the instance OS / Docker overhead — 3 GiB
                # is plenty for snakemake orchestration.
                memory=3072,
                # Overridden at submit time; this placeholder avoids a CDK validation error.
                command=["/usr/local/bin/coordinator-entrypoint.sh"],
                environment=[
                    batch.CfnJobDefinition.EnvironmentProperty(
                        name="SNAKEMAKE_AWS_BATCH_REGION", value=self.region
                    ),
                    batch.CfnJobDefinition.EnvironmentProperty(
                        name="SNAKEMAKE_AWS_BATCH_JOB_ROLE", value=job_role.role_arn
                    ),
                    batch.CfnJobDefinition.EnvironmentProperty(
                        name="SNAKEMAKE_AWS_BATCH_JOB_QUEUE",
                        value=(
                            f"arn:aws:batch:{self.region}:{self.account}"
                            f":job-queue/{project_name}-c8g"
                        ),
                    ),
                    batch.CfnJobDefinition.EnvironmentProperty(
                        name="BWA_MEM3_BENCH_S3_BUCKET", value=bucket_name
                    ),
                    batch.CfnJobDefinition.EnvironmentProperty(
                        name="BWA_MEM3_BENCH_ECR_REPO", value=ecr_repo_uri
                    ),
                    *(
                        [
                            batch.CfnJobDefinition.EnvironmentProperty(
                                name="BWA_MEM3_BENCH_COST_CENTER", value=cost_center
                            )
                        ]
                        if cost_center
                        else []
                    ),
                ],
            ),
        )

        # ── EventBridge → SNS notifications ─────────────────────────────────
        # Fires when the coordinator job reaches SUCCEEDED or FAILED.
        # Subscribe via: aws sns subscribe --topic-arn <arn> --protocol email ...
        notification_topic = sns.Topic(
            self,
            "BenchNotificationTopic",
            topic_name=f"{project_name}-notifications",
        )

        events.Rule(
            self,
            "CoordinatorCompletionRule",
            event_pattern=events.EventPattern(
                source=["aws.batch"],
                detail_type=["Batch Job State Change"],
                detail={
                    "status": ["SUCCEEDED", "FAILED"],
                    "jobQueue": [coordinator_queue.job_queue_arn],
                },
            ),
            targets=[targets.SnsTopic(notification_topic)],
        )

        # ── CloudFormation outputs ───────────────────────────────────────────
        cdk.CfnOutput(self, "BucketName", value=bucket_name)
        cdk.CfnOutput(self, "CoordinatorQueueName", value=coordinator_queue_name)
        cdk.CfnOutput(
            self,
            "CoordinatorJobDefinitionName",
            value=coordinator_queue_name,
        )
        cdk.CfnOutput(
            self,
            "NotificationTopicArn",
            value=notification_topic.topic_arn,
        )
        for spec in ARCHS:
            cdk.CfnOutput(
                self,
                f"Queue{spec.logical_id}",
                value=f"{project_name}-{spec.arch_key}",
            )
