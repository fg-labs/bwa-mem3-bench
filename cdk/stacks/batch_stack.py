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


@dataclass(frozen=True)
class ArenaArchSpec:
    logical_id: str      # camelcase for CDK construct ids
    arch_key: str        # matches workflow/rules/arena.smk's ARENA_QUEUES key (c7i, c8g)
    instance_type: str   # SAME instance type as the regular spot ArchSpec for this arch —
                          # the arena measures the SAME hardware, just on-demand instead of spot


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
    # c8g64: Graviton4 at 64 vCPU, used ONLY by the thread-scaling ladder
    # (`--target thread_scaling`), never by the cross-arch sweep — it is absent
    # from `full_archs` in config/archs.yaml.
    #
    # A 16xlarge rather than a per-thread-count right-size because strong-scaling
    # efficiency is only meaningful on fixed hardware (different instance sizes
    # get different shares of memory bandwidth and L3, which is what bounds
    # bwa-mem's scaling), and because hg38 needs ~16.5 GB resident while c8g is
    # 2 GiB/vCPU — so c8g.4xlarge is already the smallest c8g that can run the
    # aligner at all and nothing below 16 threads could be right-sized anyway.
    #
    # Graviton4 has no SMT (ThreadsPerCore=1), so 64 vCPU is 64 physical cores
    # and the scaling curve has no hyperthreading knee at 32.
    ArchSpec("C8g64", "c8g64", "c8g.16xlarge", "linux/arm64"),
)


# On-demand queues for the "arena" release-history comparison
# (workflow/rules/arena.smk, config/defaults.yaml's `arena.archs`). Every arm
# in that job runs INTERLEAVED on one host, so a spot reclaim mid-job would
# corrupt every arm's timing at once — the same reasoning that keeps
# `CoordinatorCe` below on-demand. Scoped to c7i + c8g only (not every arch in
# `ARCHS`): the arena is a narrow, correctness-anchored progression view
# alongside the cross-arch spot sweep, not a replacement for it, and each
# additional arch roughly doubles the on-demand spend for the same job.
ARENA_ARCHS: tuple[ArenaArchSpec, ...] = (
    ArenaArchSpec("C7iArena", "c7i", "c7i.4xlarge"),
    ArenaArchSpec("C8gArena", "c8g", "c8g.4xlarge"),
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
            # IMDS must be reachable FROM INSIDE the task container, not just
            # from the host: `emit-host-meta` (docker/emit-host-meta.sh, run by
            # `align_fg_labs` in workflow/rules/align.smk) records instance-type /
            # AZ / instance-id so a timing difference between two runs can be
            # attributed to a host rather than guessed at.
            #
            # A container sits one network hop further from IMDS than the host,
            # and the default HttpPutResponseHopLimit of 1 drops the IMDSv2
            # token PUT before it arrives — so the container can never obtain a
            # token no matter how the request is written. 2 is the documented
            # minimum for containerized workloads.
            #
            # Without this the metadata silently degrades to "unknown"; it does
            # not fail the job, which is exactly why the previous IMDSv1 bug
            # went unnoticed for the entire history of the project.
            http_put_response_hop_limit=2,
            # The widened hop limit puts IMDS in reach of anything running in the
            # container, so pair it with REQUIRED: a tokenless IMDSv1 GET could
            # otherwise read the instance role's credentials from in there. Both
            # consumers already speak IMDSv2 — emit-host-meta does the token PUT
            # itself, and the ECS agent on AL2023 uses v2.
            http_tokens=ec2.LaunchTemplateHttpTokens.REQUIRED,
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

        # ── Arena compute environments (on-demand) ───────────────────────────
        # See ARENA_ARCHS above for why these are separate from the spot queues
        # constructed just above, and on-demand rather than spot. Reuses the
        # same launch template (high-throughput gp3 root, IMDSv2 hop-limit 2)
        # as the regular worker fleet — the arena writes the same kind of BAM
        # artifacts and its arms invoke `emit-host-meta`/`emit-host-probe` from
        # inside the container exactly as `align_fg_labs` does.
        self.arena_queues: dict[str, batch.IJobQueue] = {}
        for arena_spec in ARENA_ARCHS:
            arena_ce = batch.ManagedEc2EcsComputeEnvironment(
                self,
                f"ArenaCe{arena_spec.logical_id}",
                vpc=vpc,
                instance_types=[ec2.InstanceType(arena_spec.instance_type)],
                use_optimal_instance_classes=False,
                allocation_strategy=batch.AllocationStrategy.BEST_FIT_PROGRESSIVE,
                spot=False,
                # One arena job at a time per arch is the intended usage (a
                # release bless runs it once) reserving the WHOLE host (see
                # arena.smk's `threads: CONFIG.arena.threads` — 16, matching
                # this *.4xlarge's own vCPU count), so a single instance's
                # worth of headroom is enough. Sized deliberately below the
                # regular fleet's `max_vcpus` so an accidental double-submit
                # can't silently double the on-demand spend; bump by hand
                # alongside `arena.threads` in config/defaults.yaml if that
                # ever changes (kept a plain literal rather than an import
                # from bwa_mem3_bench, which no other CDK stack depends on).
                maxv_cpus=16,
                minv_cpus=0,
                instance_role=instance_role,
                launch_template=launch_template,
                security_groups=[security_group],
            )
            arena_queue_name = f"{project_name}-{arena_spec.arch_key}-arena"
            arena_queue = batch.JobQueue(
                self,
                f"ArenaQueue{arena_spec.logical_id}",
                job_queue_name=arena_queue_name,
                compute_environments=[
                    batch.OrderedComputeEnvironment(compute_environment=arena_ce, order=1)
                ],
            )
            self.arena_queues[arena_spec.arch_key] = arena_queue

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
        for arena_spec in ARENA_ARCHS:
            cdk.CfnOutput(
                self,
                f"ArenaQueueOutput{arena_spec.logical_id}",
                value=f"{project_name}-{arena_spec.arch_key}-arena",
            )
