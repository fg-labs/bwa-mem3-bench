"""`build-remote` — build bench images on AWS Batch, on native hardware per arch.

A multi-arch build on an Apple Silicon laptop runs its amd64 half under QEMU. For
a C++ compile that is 5-15x slower than native, and the bench fleet needs both
architectures, so the x86 half is not optional -- it is either emulated or run on
x86 hardware.

The arch-specific Batch queues already provide that hardware: `bwa-mem3-bench-c6a`
is amd64, `bwa-mem3-bench-c7g` / `-c8g` are arm64. Their compute environments
scale to zero, so unlike a pair of persistent EC2 builders they cost nothing
between builds. The ECR push also originates inside AWS, so the image never
crosses a home uplink.

Two Batch jobs cannot be a single `buildx` invocation, so each job pushes a
single-platform image under an arch-suffixed tag and this module joins them into a
manifest list with `docker buildx imagetools create`. That join is metadata only
-- it copies no layers -- so it is cheap to run from anywhere.

Scaling to zero has one cost worth stating: a build job starts with a cold
BuildKit cache. That is affordable only because of the base-image split (see
`bwa_mem3_bench/base_image.py`): a per-SHA build pulls the toolchain as an image
layer, in-region, and compiles just bwa-mem3. Without the split this design would
recompile the whole toolchain on every build.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import time
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench import minibwa_sha as _pinned_minibwa_sha
from bwa_mem3_bench.base_image import base_image_tag, base_image_uri, base_pins
from bwa_mem3_bench.commands._run import run_cmd

#: OCI platform -> the `arch_key` of a Batch queue whose compute environment runs
#: it natively. c6a is the cheapest x86 in the fleet and c8g the current core arm
#: arch; both are build-appropriate (16 vCPU) rather than benchmark-appropriate.
NATIVE_QUEUE_ARCH_BY_PLATFORM = {
    "linux/amd64": "c6a",
    "linux/arm64": "c8g",
}

#: Suffix appended to the image tag for each single-platform push, before the
#: manifest list is assembled under the bare tag.
ARCH_TAG_SUFFIX = {
    "linux/amd64": "amd64",
    "linux/arm64": "arm64",
}

#: Paths excluded from the build-context tarball. Mirrors `.dockerignore`, with
#: `vendor/minibwa` deliberately INCLUDED -- lh3/minibwa is private and cannot be
#: cloned inside the build, so the submodule content has to travel in the context.
CONTEXT_EXCLUDES = (
    ".git",
    ".pixi",
    ".snakemake",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
    "target",
    "data",
    "runs",
    "baseline",
    "golden",
    "local-mirror",
    "benchmark.db",
    "vendor/bwa-mem2",
    "vendor/bwa-mem3",
)

#: Batch job states that mean the job is finished, one way or the other.
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED"})


def _should_exclude(name: str) -> bool:
    """Return whether a tar member path should be left out of the build context.

    :param name: path relative to the repository root.
    :return: True when any path segment matches an exclusion.
    """
    parts = Path(name).parts
    if any(part in CONTEXT_EXCLUDES for part in parts):
        return True
    return any(name == excluded or name.startswith(f"{excluded}/") for excluded in CONTEXT_EXCLUDES)


def build_context_tarball(destination: Path) -> Path:
    """Write a gzipped tarball of the build context to `destination`.

    :param destination: file to write. Parent directories must exist.
    :return: `destination`, for chaining.
    """
    with tarfile.open(destination, "w:gz") as tar:
        for path in sorted(REPO_ROOT.iterdir()):
            name = path.name
            if _should_exclude(name):
                continue
            tar.add(
                path, arcname=name, filter=lambda info: None if _should_exclude(info.name) else info
            )
    return destination


def _submit_build_job(  # noqa: PLR0913
    *,
    job_queue: str,
    job_definition: str,
    job_name: str,
    context_s3_uri: str,
    ecr_repo: str,
    image_tag: str,
    dockerfile: str,
    platform: str,
    region: str,
    build_args: dict[str, str],
    dry_run: bool,
) -> str:
    """Submit one single-platform build job and return its Batch job id.

    :param job_queue: name or ARN of the arch-native queue to submit to.
    :param job_definition: name of the privileged build job definition.
    :param job_name: Batch job name.
    :param context_s3_uri: `s3://...` of the build-context tarball.
    :param ecr_repo: target repository URI, sans `:tag`.
    :param image_tag: arch-suffixed tag to push.
    :param dockerfile: Dockerfile path within the context.
    :param platform: OCI platform to build.
    :param region: AWS region for the ECR login inside the job.
    :param build_args: `--build-arg` pairs to forward.
    :param dry_run: print the command instead of submitting.
    :return: the Batch job id, or a placeholder under `dry_run`.
    """
    environment = {
        "CONTEXT_S3_URI": context_s3_uri,
        "ECR_REPO": ecr_repo,
        "IMAGE_TAG": image_tag,
        "DOCKERFILE": dockerfile,
        "PLATFORM": platform,
        "AWS_REGION": region,
        # One pair per line; the job script splits on newlines so values may
        # contain spaces.
        "BUILD_ARGS": "\n".join(f"{name}={value}" for name, value in sorted(build_args.items())),
    }
    overrides = {
        "environment": [{"name": name, "value": value} for name, value in environment.items()],
        # The dind image has no aws CLI and Batch overrides the entrypoint, so the
        # command bootstraps: install the CLI, fetch the real script from S3 next
        # to the context, run it. Keeping the logic in a reviewable repo file
        # rather than inline here is the point of the two-step.
        "command": [
            "sh",
            "-c",
            "apk add --no-cache aws-cli >/dev/null "
            '&& aws s3 cp "$(dirname "$CONTEXT_S3_URI")/batch-image-build.sh" /tmp/build.sh '
            "&& sh /tmp/build.sh",
        ],
    }
    cmd = [
        "aws",
        "batch",
        "submit-job",
        "--region",
        region,
        "--job-name",
        job_name,
        "--job-queue",
        job_queue,
        "--job-definition",
        job_definition,
        "--container-overrides",
        json.dumps(overrides),
    ]
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return f"dry-run-{ARCH_TAG_SUFFIX[platform]}"

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return str(json.loads(result.stdout)["jobId"])


def _wait_for_jobs(job_ids: dict[str, str], *, region: str, poll_seconds: int = 30) -> None:
    """Block until every job reaches a terminal state, then fail if any failed.

    Batch is polled rather than waited on with `aws batch wait`, which has no
    such waiter. A build job is minutes long, so a 30 s cadence is fine.

    :param job_ids: platform -> Batch job id.
    :param region: AWS region.
    :param poll_seconds: seconds between polls.
    :raises RuntimeError: if any job reaches FAILED.
    """
    pending = dict(job_ids)
    failed: dict[str, str] = {}

    while pending:
        result = subprocess.run(
            ["aws", "batch", "describe-jobs", "--region", region, "--jobs", *pending.values()],
            capture_output=True,
            text=True,
            check=True,
        )
        by_id = {job["jobId"]: job for job in json.loads(result.stdout)["jobs"]}
        for platform, job_id in list(pending.items()):
            status = by_id.get(job_id, {}).get("status", "SUBMITTED")
            if status not in _TERMINAL_STATES:
                continue
            del pending[platform]
            if status == "FAILED":
                reason = by_id[job_id].get("statusReason", "no statusReason")
                failed[platform] = f"{job_id}: {reason}"
            print(f"{platform}: {status} ({job_id})")
        if pending:
            time.sleep(poll_seconds)

    if failed:
        detail = "; ".join(f"{platform} {why}" for platform, why in sorted(failed.items()))
        raise RuntimeError(
            f"build job(s) FAILED: {detail}. Fetch logs with "
            "`pixi run python -m bwa_mem3_bench.cli aws logs <job-id>`."
        )


def _manifest_list_sources(ecr_repo: str, tag: str, platforms: list[str]) -> list[str]:
    """Return the per-arch image references that a manifest list should point at.

    :param ecr_repo: repository URI, sans `:tag`.
    :param tag: the bare tag the manifest list will carry.
    :param platforms: platforms that were built.
    :return: fully-qualified per-arch references.
    """
    return [f"{ecr_repo}:{tag}-{ARCH_TAG_SUFFIX[platform]}" for platform in platforms]


#: Name of the privileged build job definition provisioned by the batch stack.
BUILD_JOB_DEFINITION_SUFFIX = "-image-build"


def build_remote(  # noqa: PLR0913
    *,
    fg_labs_sha: str | None = None,
    base: bool = False,
    platforms: str | None = None,
    image_name: str | None = None,
    wait: bool = True,
    dry_run: bool = False,
) -> None:
    """Build a bench image on AWS Batch, one native job per architecture.

    Submits one single-platform build to each arch's own queue -- amd64 to a c6a
    compute environment, arm64 to c8g -- so neither half is emulated. The queues
    scale to zero, so this costs nothing between builds, and the ECR push happens
    inside AWS rather than over your uplink.

    :param fg_labs_sha: fg-labs/bwa-mem3 commit SHA to build. Required unless
        ``base`` is set.
    :param base: build the builder base image (``docker/Dockerfile.base``)
        instead of a per-SHA image. Needed only after a pin bump or an edit to
        that recipe.
    :param platforms: comma-separated platforms. Defaults to both fleet
        architectures, which is the only setting that produces a usable image;
        narrow it only to reproduce or debug one arch.
    :param image_name: target repository URI, sans ``:<tag>``. Defaults to the
        ECR repo from ``cdk/outputs.json``.
    :param wait: block until both jobs finish and then assemble the manifest
        list. With ``False`` the jobs are submitted and the manifest step is left
        to a later ``--no-... `` invocation or done by hand -- the per-arch tags
        are complete images on their own.
    :param dry_run: print what would happen without submitting or uploading.
    :raises ValueError: if neither or both of ``fg_labs_sha`` and ``base`` are given.
    """
    if bool(fg_labs_sha) == bool(base):
        raise ValueError("pass exactly one of --fg-labs-sha or --base")

    config = aws_config.load()
    resolved_platforms = (platforms or ",".join(NATIVE_QUEUE_ARCH_BY_PLATFORM)).split(",")
    unsupported = [p for p in resolved_platforms if p not in NATIVE_QUEUE_ARCH_BY_PLATFORM]
    if unsupported:
        raise ValueError(
            f"no native Batch queue for {unsupported}; known: "
            f"{sorted(NATIVE_QUEUE_ARCH_BY_PLATFORM)}"
        )

    benchmark_repo = image_name or config.ecr_repo_uri
    if base:
        # The base image lives in its own repository, and its tag is
        # content-addressed -- see base_image.py for why both matter.
        target_repo = f"{benchmark_repo}-base"
        tag = base_image_tag()
        dockerfile = "docker/Dockerfile.base"
        build_args = base_pins()
    else:
        target_repo = benchmark_repo
        tag = str(fg_labs_sha)
        dockerfile = "docker/Dockerfile"
        build_args = {
            "BASE_IMAGE": base_image_uri(benchmark_repo),
            "FG_LABS_REPO": "https://github.com/fg-labs/bwa-mem3",
            "FG_LABS_SHA": str(fg_labs_sha),
            "BASELINE_ARCH": "",
            "FG_LABS_MAKE_TARGET": "",
            "SAMTOOLS_VERSION": "1.23.1",
            "BWA_VERSION": "0.7.19",
            "BWAMETH_VERSION": "0.2.7",
            "MINIBWA_SHA": _pinned_minibwa_sha(),
        }

    # Stage the context and the job script side by side; the job's bootstrap
    # derives the script's location from the context URI's directory.
    prefix = f"s3://{config.bucket}/image-builds/{tag}"
    context_uri = f"{prefix}/context.tar.gz"
    if dry_run:
        print(f"[dry-run] would upload build context to {context_uri}")
    else:
        staging = REPO_ROOT / ".image-build-context.tar.gz"
        try:
            build_context_tarball(staging)
            run_cmd(["aws", "s3", "cp", str(staging), context_uri], dry_run=False)
        finally:
            staging.unlink(missing_ok=True)
        run_cmd(
            ["aws", "s3", "cp", "docker/batch-image-build.sh", f"{prefix}/batch-image-build.sh"],
            dry_run=False,
            cwd=REPO_ROOT,
        )

    project = config.ecr_repo_uri.rsplit("/", maxsplit=1)[-1]
    job_ids: dict[str, str] = {}
    for platform in resolved_platforms:
        arch_key = NATIVE_QUEUE_ARCH_BY_PLATFORM[platform]
        arch_tag = f"{tag}-{ARCH_TAG_SUFFIX[platform]}"
        job_ids[platform] = _submit_build_job(
            job_queue=f"{project}-{arch_key}",
            job_definition=f"{project}{BUILD_JOB_DEFINITION_SUFFIX}",
            job_name=f"build-{arch_tag}".replace(".", "-")[:128],
            context_s3_uri=context_uri,
            ecr_repo=target_repo,
            image_tag=arch_tag,
            dockerfile=dockerfile,
            platform=platform,
            region=config.region,
            build_args=build_args,
            dry_run=dry_run,
        )
        print(f"submitted {platform} -> {project}-{arch_key}: {job_ids[platform]}")

    if not wait:
        print("not waiting; assemble the manifest list once both jobs succeed.")
        return

    if not dry_run:
        _wait_for_jobs(job_ids, region=config.region)

    # Metadata only -- imagetools copies no layers, so this is fast from anywhere.
    run_cmd(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            f"{target_repo}:{tag}",
            *_manifest_list_sources(target_repo, tag, resolved_platforms),
        ],
        dry_run=dry_run,
        cwd=REPO_ROOT,
    )
    print(f"manifest list pushed: {target_repo}:{tag}")
