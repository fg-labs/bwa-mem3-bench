"""`build` — build the multi-arch Docker image for a given fg-labs SHA."""

from __future__ import annotations

import subprocess

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.commands._run import run_cmd


def _ecr_login(image_name: str, *, dry_run: bool) -> None:
    """Authenticate Docker to ECR when `image_name` is an ECR URI."""
    if ".dkr.ecr." not in image_name:
        return  # not an ECR URI
    registry = image_name.split("/", maxsplit=1)[0]  # "<acct>.dkr.ecr.<region>.amazonaws.com"
    region = registry.split(".")[3]
    login_cmd = (
        f"aws ecr get-login-password --region {region} | "
        f"docker login --username AWS --password-stdin {registry}"
    )
    if dry_run:
        print(f"[dry-run] {login_cmd}")
        return
    subprocess.run(["bash", "-c", login_cmd], check=True)


def build(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    upstream_tag: str = "v2.2.1",
    platforms: str = "linux/amd64,linux/arm64",
    image_name: str = "bwa-mem3-bench",
    baseline_arch: str = "",
    push: bool = False,
    load: bool = False,
    also_tag_latest: bool = True,
    dry_run: bool = False,
) -> None:
    """Build the bwa-mem3-bench Docker image for a given fg-labs SHA.

    :param fg_labs_sha: fg-labs/bwa-mem3 commit SHA.
    :param upstream_tag: upstream bwa-mem2 tag to bake in (default v2.2.1).
    :param platforms: comma-separated platforms for buildx.
    :param image_name: image name, sans `:<tag>`. Use an ECR URI to tag for
        push, e.g. `<account-id>.dkr.ecr.<region>.amazonaws.com/bwa-mem3-bench`.
    :param baseline_arch: fg-labs/bwa-mem3 ``BASELINE_ARCH`` build-arg
        (e.g. ``avx2``, ``avx512bw``). Empty string means "no override" — the
        upstream default is used and the image stays portable across x86
        SIMD tiers. When set, the image is host-locked to that tier or
        higher and the SHA tag is suffixed (e.g. ``<sha>-avx512bw``); the
        portable ``:latest`` tag is NOT updated to avoid clobbering it with
        a host-locked variant.
    :param push: push to ECR after build (mutually exclusive with --load).
    :param load: load into local docker (single-arch only).
    :param also_tag_latest: when pushing, also tag + push as `:latest`. The
        coordinator Batch job definition references `:latest`, so every new
        image needs it. Default True; set False to only push the SHA tag.
        Forced False when ``baseline_arch`` is set.
    :param dry_run: print the command without executing.
    """
    if push and load:
        raise ValueError("--push and --load are mutually exclusive")

    if push:
        _ecr_login(image_name, dry_run=dry_run)

    tag_suffix = f"-{baseline_arch}" if baseline_arch else ""
    sha_tag = f"{fg_labs_sha}{tag_suffix}"

    cmd = [
        "docker",
        "buildx",
        "build",
        "--file",
        "docker/Dockerfile",
        "--platform",
        platforms,
        # OCI attestation manifests confuse the AWS ECS agent, which then pulls the
        # wrong-arch variant onto Graviton nodes. Plain manifest lists are fine.
        "--provenance=false",
        "--build-arg",
        "UPSTREAM_REPO=https://github.com/bwa-mem2/bwa-mem2",
        "--build-arg",
        f"UPSTREAM_TAG={upstream_tag}",
        "--build-arg",
        "FG_LABS_REPO=https://github.com/fg-labs/bwa-mem3",
        "--build-arg",
        f"FG_LABS_SHA={fg_labs_sha}",
        "--build-arg",
        f"BASELINE_ARCH={baseline_arch}",
        "--build-arg",
        "SAMTOOLS_VERSION=1.22.1",
        "--build-arg",
        "BWA_VERSION=0.7.19",
        "--build-arg",
        "BWAMETH_VERSION=0.2.7",
        "--tag",
        f"{image_name}:{sha_tag}",
    ]
    # Only update :latest for portable (no-baseline_arch) builds. A host-locked
    # avx512bw image tagged :latest would crash all c6a workers on next pull.
    if push and also_tag_latest and not baseline_arch:
        cmd.extend(["--tag", f"{image_name}:latest"])
    if push:
        cmd.append("--push")
    if load:
        cmd.append("--load")
    cmd.append(".")

    run_cmd(cmd, dry_run=dry_run, cwd=REPO_ROOT)
