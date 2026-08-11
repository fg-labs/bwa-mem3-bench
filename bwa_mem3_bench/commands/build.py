"""`build` — build the multi-arch Docker image for a given fg-labs SHA."""

from __future__ import annotations

import platform
import subprocess

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench import fgumi_ref as _pinned_fgumi_ref
from bwa_mem3_bench import fgumi_repo as _pinned_fgumi_repo
from bwa_mem3_bench import holodeck_ref as _pinned_holodeck_ref
from bwa_mem3_bench import holodeck_repo as _pinned_holodeck_repo
from bwa_mem3_bench import minibwa_sha as _pinned_minibwa_sha
from bwa_mem3_bench import tachyon_version as _pinned_tachyon_version
from bwa_mem3_bench.commands._run import run_cmd

#: Platforms an image must carry to run on the whole bench fleet: x86 (c6a/c7i/c7a)
#: and Graviton (c7g/c8g). Only meaningful for images that get pushed to ECR.
FLEET_PLATFORMS = "linux/amd64,linux/arm64"

#: `platform.machine()` values mapped onto the OCI architecture names buildx wants.
_OCI_ARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _native_platform() -> str:
    """Return the OCI platform string for the machine running the build.

    Resolved from `platform.machine()` rather than by shelling out to
    `docker version`, so it stays usable under --dry-run and in tests.

    :return: e.g. `linux/arm64` on Apple Silicon, `linux/amd64` on an x86 host.
    """
    machine = platform.machine().lower()
    return f"linux/{_OCI_ARCH_BY_MACHINE.get(machine, machine)}"


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
    minibwa_sha: str | None = None,
    holodeck_ref: str | None = None,
    upstream_tag: str = "v2.2.1",
    platforms: str | None = None,
    image_name: str = "bwa-mem3-bench",
    baseline_arch: str = "",
    make_target: str = "",
    push: bool = False,
    load: bool = False,
    also_tag_latest: bool = True,
    dry_run: bool = False,
) -> None:
    """Build the bwa-mem3-bench Docker image for a given fg-labs SHA.

    :param fg_labs_sha: fg-labs/bwa-mem3 commit SHA.
    :param minibwa_sha: lh3/minibwa commit SHA recorded as the image's
        ``MINIBWA_SHA`` build-arg label. Defaults to the canonical pin in
        ``docker/build-arg-defaults.env`` (which must match the vendored
        ``vendor/minibwa`` submodule commit — the real source of truth).
        Pass explicitly only to override the label.
    :param holodeck_ref: fg-labs/holodeck git ref the image cargo-installs
        ``holodeck`` from (the truth simulator + ``holodeck eval``). Defaults to
        the canonical pin in ``docker/build-arg-defaults.env``. Pass explicitly
        to build against a different holodeck commit.
    :param upstream_tag: upstream bwa-mem2 tag to bake in (default v2.2.1).
    :param platforms: comma-separated platforms for buildx. Defaults to the
        whole fleet (`linux/amd64,linux/arm64`) when ``push`` is set, and to
        the host's own architecture otherwise. Building both architectures is
        only useful for an image that will actually be pulled by both x86 and
        Graviton workers; for a local build it doubles the layers deposited in
        the build cache and, on Apple Silicon, runs the amd64 half under QEMU
        emulation for no benefit. Pass explicitly to override either default.
    :param image_name: image name, sans `:<tag>`. Use an ECR URI to tag for
        push, e.g. `<account-id>.dkr.ecr.<region>.amazonaws.com/bwa-mem3-bench`.
    :param baseline_arch: fg-labs/bwa-mem3 ``BASELINE_ARCH`` build-arg
        (e.g. ``avx2``, ``avx512bw``). Empty string means "no override" — the
        upstream default is used and the image stays portable across x86
        SIMD tiers. When set, the image is host-locked to that tier or
        higher and the SHA tag is suffixed (e.g. ``<sha>-avx512bw``); the
        portable ``:latest`` tag is NOT updated to avoid clobbering it with
        a host-locked variant. Currently a no-op for the workflow because
        every arch in ``config/archs.yaml`` is parked at
        ``baseline_arch=""`` — empirical data showed the avx512bw variant
        is not a perf win on this workload (see the fg-labs/bwa-mem3 AVX-512
        baseline-build Phase C benchmarking). The flag is preserved for
        re-enablement once upstream lands a fix.
    :param make_target: fg-labs/bwa-mem3 Makefile target to invoke when
        building. Empty (default) runs ``make`` (target ``all``) and installs
        ``bwa-mem3``. ``lto-build`` runs ``make lto-build`` and installs the
        resulting ``bwa-mem3.lto`` under the canonical path. The SHA tag is
        suffixed (e.g. ``<sha>-lto-build``) and ``:latest`` is NOT updated.
        Used for A/B'ing build-flag perf experiments (LTO, future PGO)
        against the same fg-labs SHA without re-tagging branches upstream.
        The matching ``submit --make-target ...`` propagates the suffix to
        worker image tags.
    :param push: push to ECR after build (mutually exclusive with --load).
    :param load: load into local docker (single-arch only).
    :param also_tag_latest: when pushing, also tag + push as `:latest`. The
        coordinator Batch job definition references `:latest`, so every new
        image needs it. Default True; set False to only push the SHA tag.
        Forced False when ``baseline_arch`` or ``make_target`` is set — a
        host-locked or build-variant image tagged ``:latest`` would silently
        become the default for any submit that didn't specify the matching
        arg.
    :param dry_run: print the command without executing.
    """
    if push and load:
        raise ValueError("--push and --load are mutually exclusive")

    # A multi-arch build is only worth its cost when the result is pushed as a
    # manifest list for the fleet to pull. A local build that is neither pushed
    # nor loaded leaves its layers in the build cache and nowhere else, so
    # defaulting to both architectures there is pure cache growth.
    default_platforms = FLEET_PLATFORMS if push else _native_platform()
    resolved_platforms = platforms if platforms else default_platforms

    # `--load` exports through the docker exporter, which writes into the local
    # daemon's image store -- and that store has no concept of a manifest list. A
    # multi-platform build with --load therefore dies inside buildx with "docker
    # exporter does not currently support exporting manifest lists", after doing
    # all the compiling. Checked here so the failure is instant and says what to
    # do instead. Checked AFTER resolution so an explicit --platforms is caught
    # too, not just a default.
    if load and "," in resolved_platforms:
        raise ValueError(
            f"--load cannot export multiple platforms ({resolved_platforms}): the local "
            "docker image store holds no manifest lists. Build a single platform, or "
            "use --push to publish a manifest list to a registry."
        )

    make_target = make_target.strip()
    _supported_make_targets = {"", "lto-build"}
    if make_target not in _supported_make_targets:
        raise ValueError(
            f"--make-target must be one of {sorted(_supported_make_targets)}, got {make_target!r}"
        )

    # Default the MINIBWA_SHA label to the canonical pin (build-arg-defaults.env)
    # so a plain `build` matches the vendored submodule without an explicit flag.
    resolved_minibwa_sha = minibwa_sha or _pinned_minibwa_sha()

    # Default the holodeck git ref to the canonical pin (build-arg-defaults.env);
    # the Dockerfile cargo-installs `holodeck` from this ref of the public repo.
    resolved_holodeck_ref = holodeck_ref or _pinned_holodeck_ref()
    # The repo URL is always read from the same pin source so a change to
    # build-arg-defaults.env can't leave build() sending a stale repository.
    resolved_holodeck_repo = _pinned_holodeck_repo()

    # fgumi supplies `fgumi compare bams` for the --compat identity check. Same
    # pin-from-one-source treatment as holodeck; no CLI override, because the
    # comparison tool's version is part of a release's evidence and should move
    # deliberately via build-arg-defaults.env rather than per invocation.
    resolved_fgumi_ref = _pinned_fgumi_ref()
    resolved_fgumi_repo = _pinned_fgumi_repo()
    resolved_tachyon_version = _pinned_tachyon_version()

    # minibwa is vendored as a git submodule (private repo, can't clone in
    # the Dockerfile). Confirm it has been populated before invoking buildx.
    # Skipped on --dry-run so the print path works on a fresh clone.
    minibwa_makefile = REPO_ROOT / "vendor" / "minibwa" / "Makefile"
    if not dry_run and not minibwa_makefile.is_file():
        raise FileNotFoundError(
            f"vendored minibwa source missing at {minibwa_makefile.parent}; "
            "run `git submodule update --init` (requires lh3/minibwa access)."
        )

    if push:
        _ecr_login(image_name, dry_run=dry_run)

    # Suffix order is baseline_arch first, make_target second, so
    # e.g. `--baseline-arch=avx512bw --make-target=lto-build` produces
    # `<sha>-avx512bw-lto-build` (matches the order the args appear in
    # the conceptual build pipeline: arch selection then build flags).
    suffix_parts: list[str] = []
    if baseline_arch:
        suffix_parts.append(baseline_arch)
    if make_target:
        suffix_parts.append(make_target)
    tag_suffix = "-" + "-".join(suffix_parts) if suffix_parts else ""
    sha_tag = f"{fg_labs_sha}{tag_suffix}"

    cmd = [
        "docker",
        "buildx",
        "build",
        "--file",
        "docker/Dockerfile",
        "--platform",
        resolved_platforms,
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
        f"FG_LABS_MAKE_TARGET={make_target}",
        "--build-arg",
        "SAMTOOLS_VERSION=1.23.1",
        "--build-arg",
        "BWA_VERSION=0.7.19",
        "--build-arg",
        "BWAMETH_VERSION=0.2.7",
        "--build-arg",
        f"MINIBWA_SHA={resolved_minibwa_sha}",
        "--build-arg",
        f"HOLODECK_REPO={resolved_holodeck_repo}",
        "--build-arg",
        f"HOLODECK_REF={resolved_holodeck_ref}",
        "--build-arg",
        f"FGUMI_REPO={resolved_fgumi_repo}",
        "--build-arg",
        f"FGUMI_REF={resolved_fgumi_ref}",
        "--build-arg",
        f"TACHYON_VERSION={resolved_tachyon_version}",
        "--tag",
        f"{image_name}:{sha_tag}",
    ]
    # Only update :latest for vanilla portable builds. A host-locked avx512bw
    # image tagged :latest would crash all c6a workers on next pull, and an
    # LTO (or other build-flag variant) image tagged :latest would silently
    # become the new "default" for any submit that didn't specify make_target.
    if push and also_tag_latest and not baseline_arch and not make_target:
        cmd.extend(["--tag", f"{image_name}:latest"])
    if push:
        cmd.append("--push")
    if load:
        cmd.append("--load")
    cmd.append(".")

    run_cmd(cmd, dry_run=dry_run, cwd=REPO_ROOT)
