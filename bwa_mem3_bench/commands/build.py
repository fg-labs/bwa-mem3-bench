"""`build` — build the multi-arch Docker image for a given fg-labs SHA."""

from __future__ import annotations

import platform
import subprocess
import sys

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench import minibwa_sha as _pinned_minibwa_sha
from bwa_mem3_bench.base_image import base_image_tag, base_image_uri, base_pins
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


#: The buildx driver `docker/buildkitd.toml` binds to. A `--config` is only read
#: by a builder that runs its own buildkitd, which is what this driver does; the
#: `docker` driver shares the daemon's built-in builder and ignores the file.
GC_BOUND_BUILDX_DRIVER = "docker-container"

#: The builder `pixi run docker-builder-create` makes, i.e. the only one this repo
#: knows was created WITH `--config docker/buildkitd.toml`. The driver alone is not
#: sufficient: a hand-rolled `docker-container` builder has the right driver and no
#: config, so its cache is unbounded exactly like the default builder's.
GC_BOUND_BUILDX_BUILDER = "bwa-mem3-bench-builder"

#: Seconds to wait for the advisory driver probe. An unresponsive daemon must not
#: stall a build on a check that only prints a warning.
_BUILDER_PROBE_TIMEOUT_S = 5


def _native_platform() -> str:
    """Return the OCI platform string for the machine running the build.

    Resolved from `platform.machine()` rather than by shelling out to
    `docker version`, so it stays usable under --dry-run and in tests.

    :return: e.g. `linux/arm64` on Apple Silicon, `linux/amd64` on an x86 host.
    :raises ValueError: if the machine type is empty or not a known architecture.
    """
    machine = platform.machine().lower()
    # `platform.machine()` is documented to return an EMPTY STRING when the value
    # cannot be determined, and it returns plenty of arches buildx has no business
    # building for (i386, ppc64le). Forwarding either produces `--platform linux/`
    # or `--platform linux/ppc64le`, and buildx's own error names neither the
    # empty value nor where it came from.
    if machine not in _OCI_ARCH_BY_MACHINE:
        raise ValueError(
            f"cannot map this host's architecture to an OCI platform: "
            f"platform.machine() returned {platform.machine()!r}. "
            f"Known values: {sorted(_OCI_ARCH_BY_MACHINE)}. "
            "Pass --platforms explicitly to build for a specific platform."
        )
    return f"linux/{_OCI_ARCH_BY_MACHINE[machine]}"


def _warn_if_builder_is_not_gc_bounded(*, dry_run: bool) -> None:
    """Warn when the active buildx builder ignores `docker/buildkitd.toml`.

    That config is the only thing bounding this repo's build cache, and it binds
    ONLY to a `docker-container` builder created with `--config`. Every other
    builder -- the daemon's default, anything selected via `BUILDX_BUILDER` --
    silently ignores it, so the cache grows without bound exactly as it did
    before the config existed. The failure has no symptom until the disk is full,
    which is how it reached ~95 GB unnoticed, so it is worth a line on stderr.

    Advisory only: a wrong builder is not a reason to refuse a build, and the
    driver probe needs a live daemon that `--dry-run` should not require.

    :param dry_run: skip the probe entirely.
    """
    if dry_run:
        return
    try:
        probe = subprocess.run(
            ["docker", "buildx", "inspect", "--format", "{{.Name}} {{.Driver}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_BUILDER_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        # An unresponsive or absent docker must not stall the build on a check
        # that only ever prints a warning.
        return
    # A failed probe means no daemon or no such builder; the build itself is
    # about to report that far better than a warning would.
    if probe.returncode != 0:
        return

    name, _, driver = probe.stdout.strip().partition(" ")
    if not driver:
        return

    if driver != GC_BOUND_BUILDX_DRIVER:
        reason = (
            f"the active buildx builder {name!r} uses the {driver!r} driver, and "
            f"only the {GC_BOUND_BUILDX_DRIVER!r} driver reads a --config"
        )
    elif name != GC_BOUND_BUILDX_BUILDER:
        # Right driver, unknown provenance. buildx does not report which config
        # file a builder was created with, so a `docker-container` builder that
        # is not the one `docker-builder-create` makes cannot be assumed to carry
        # the ceiling -- and silently assuming it would defeat the check.
        reason = (
            f"the active buildx builder {name!r} has the right driver but is not "
            f"{GC_BOUND_BUILDX_BUILDER!r}, so it may have been created without "
            "`--config docker/buildkitd.toml`"
        )
    else:
        return

    print(
        f"warning: {reason}. docker/buildkitd.toml's cache ceiling may NOT apply, "
        "leaving this build's layers unbounded. Create or select the project "
        "builder with `pixi run docker-builder-create`.",
        file=sys.stderr,
    )


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


def _image_in_local_daemon(image: str) -> bool:
    """Return whether `image` is present in the local Docker daemon's image store.

    :param image: full image reference.
    :return: True if `docker image inspect` finds it.
    """
    return (
        subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _base_image_is_resolvable(base_image: str) -> bool:
    """Return whether `base_image` can actually be fetched right now.

    Used only on the failure path, to decide whether a failed build is plausibly
    a base-resolution failure at all. A compile error inside the build is far
    more common than an unresolvable base, and appending the base-image
    diagnosis to a broken C++ compile sends the reader somewhere useless.

    Parsing buildx's output would be the obvious test and is not available:
    `run_cmd` streams rather than captures, precisely so a long build shows
    progress. So this asks the registry instead. It costs a round-trip, but only
    after a build has ALREADY failed -- never on the happy path, which is what
    the original "no pre-flight" reasoning was protecting.

    :param base_image: full image reference.
    :return: True if present locally or resolvable in its registry.
    """
    if _image_in_local_daemon(base_image):
        return True
    try:
        probe = subprocess.run(
            ["docker", "manifest", "inspect", base_image],
            capture_output=True,
            check=False,
            timeout=_BUILDER_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        # Unknown. Prefer offering the diagnosis over withholding it: the caller
        # is already looking at a failure, and a hint that turns out not to apply
        # is cheaper than a missing one that did.
        return False
    return probe.returncode == 0


def _explain_base_image_failure(base_image: str) -> str:
    """Return guidance for a build that failed while resolving BASE_IMAGE.

    buildx's own error for this is `pull access denied ... insufficient_scope`,
    which points at authentication and not at the actual cause. There are two
    distinct causes and they need opposite fixes, so name which one applies.

    :param base_image: the base image reference the build was given.
    :return: a message naming the cause and the remedy.
    """
    if _image_in_local_daemon(base_image):
        return (
            f"\n\nThe build could not resolve BASE_IMAGE={base_image}, but that image "
            "IS in your local Docker daemon.\n"
            "The active buildx builder almost certainly uses the `docker-container` "
            "driver, which runs in its own container with its own image store and so "
            "cannot see locally-loaded images -- it tries to PULL them instead.\n"
            "Either:\n"
            "  - push the base so any builder can fetch it:  "
            "pixi run build-docker-base --image-name <ecr-uri>\n"
            "  - or build on a `docker`-driver builder, which shares the daemon's "
            "store:  BUILDX_BUILDER=desktop-linux pixi run ...\n"
            "Note the GC ceiling in docker/buildkitd.toml only binds to the "
            "`docker-container` driver, so the second option trades the cache bound "
            "for local convenience."
        )
    return (
        f"\n\nThe build could not resolve BASE_IMAGE={base_image}, and it is not in "
        "your local Docker daemon either.\n"
        "The base image tag is content-addressed over docker/Dockerfile.base and the "
        "pins in docker/build-arg-defaults.env, so editing either publishes a NEW tag "
        "that has to be built and pushed:\n"
        "  pixi run build-docker-base --image-name <ecr-uri>"
    )


def build(  # noqa: PLR0913
    *,
    fg_labs_sha: str,
    minibwa_sha: str | None = None,
    base_image: str | None = None,
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
    :param base_image: full reference of the builder base image to build FROM.
        Defaults to the sibling ECR repository at the content-addressed tag
        derived from ``docker/Dockerfile.base`` and the pins in
        ``docker/build-arg-defaults.env`` (see ``bwa_mem3_bench.base_image``).
        The base must already exist in the registry -- build and push it with
        ``cli build-base --push`` after any pin bump or edit to
        ``Dockerfile.base``, which both change the computed tag.
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
    # Short-circuited, so `_native_platform()` runs ONLY when no override was
    # given. Evaluating it eagerly would make an unmappable host arch raise even
    # when `--platforms` was passed -- which is precisely the escape hatch that
    # error message recommends, so it has to still work on such a host.
    resolved_platforms = platforms or (FLEET_PLATFORMS if push else _native_platform())

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

    # The base image carries every build input FG_LABS_SHA does not invalidate.
    # Its tag is content-addressed over Dockerfile.base plus the pins, so a pin
    # bump or a recipe edit produces a new tag instead of silently reusing a
    # stale toolchain -- but that also means the matching base must have been
    # pushed. `cli build-base --push` does that.
    resolved_base_image = base_image or base_image_uri(image_name)

    # minibwa is vendored as a git submodule (private repo, can't clone in
    # the Dockerfile). Confirm it has been populated before invoking buildx.
    # Skipped on --dry-run so the print path works on a fresh clone.
    minibwa_makefile = REPO_ROOT / "vendor" / "minibwa" / "Makefile"
    if not dry_run and not minibwa_makefile.is_file():
        raise FileNotFoundError(
            f"vendored minibwa source missing at {minibwa_makefile.parent}; "
            "run `git submodule update --init` (requires lh3/minibwa access)."
        )

    # Authenticate whenever ECR is involved AT ALL, not only when pushing. The
    # base image lives in a private ECR repository and buildx must PULL it to
    # satisfy `FROM ${BASE_IMAGE}` before any of this build runs -- so a plain
    # `--load` build against an ECR base fails on an expired credential even
    # though it publishes nothing. `_ecr_login` is a no-op for non-ECR names.
    #
    # Deduped by REGISTRY, not by reference: the base is a sibling repository in
    # the same registry, so one login covers both and a second would just be
    # another `aws ecr get-login-password` round-trip.
    for registry in dict.fromkeys(
        r.split("/", maxsplit=1)[0] for r in (image_name, resolved_base_image)
    ):
        _ecr_login(registry, dry_run=dry_run)

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
        f"BASE_IMAGE={resolved_base_image}",
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

    _warn_if_builder_is_not_gc_bounded(dry_run=dry_run)

    try:
        run_cmd(cmd, dry_run=dry_run, cwd=REPO_ROOT)
    except subprocess.CalledProcessError as error:
        # Re-raised rather than pre-flighted: a pre-flight would need a registry
        # round-trip on every build to tell "missing" from "unreachable", and
        # would still be wrong whenever the active builder's driver disagreed
        # with what the probe could see. Diagnosing after the fact costs nothing
        # on the happy path and inspects the state that actually failed.
        #
        # Only when the failure LOOKS like base resolution, though: a failed
        # minibwa or bwa-mem3 compile is far more common, and telling someone
        # their BASE_IMAGE could not resolve when their C++ did not compile sends
        # them to the wrong place entirely.
        if not _base_image_is_resolvable(resolved_base_image):
            raise RuntimeError(
                str(error) + _explain_base_image_failure(resolved_base_image)
            ) from error
        raise


def build_base(
    *,
    platforms: str | None = None,
    image_name: str = "bwa-mem3-bench",
    push: bool = False,
    load: bool = False,
    dry_run: bool = False,
) -> None:
    """Build the builder base image `docker/Dockerfile` starts FROM.

    The base carries the clang-19 toolchain, the Rust toolchains, the upstream
    bwa-mem2 build and the four cargo-installed pinned tools -- everything
    ``FG_LABS_SHA`` does not invalidate. Rebuild and push it after bumping any
    pin in ``docker/build-arg-defaults.env`` or editing ``Dockerfile.base``;
    both change the content-addressed tag, so the old base stays valid for
    older builds rather than being overwritten.

    :param platforms: comma-separated platforms for buildx. Same defaults as
        :func:`build` -- the whole fleet when pushing, the host's own
        architecture otherwise.
    :param image_name: benchmark image name, sans `:<tag>`. The base is built
        for the sibling repository (``<image_name>-base``), which must exist;
        the CDK storage stack provisions it.
    :param push: push to ECR after build (mutually exclusive with --load).
    :param load: load into local docker (single-arch only).
    :param dry_run: print the command without executing.
    :raises ValueError: if both ``push`` and ``load`` are set.
    """
    if push and load:
        raise ValueError("--push and --load are mutually exclusive")

    # Short-circuited for the same reason as in `build` -- an explicit
    # `--platforms` must still work on a host whose arch cannot be mapped, since
    # that is the escape hatch `_native_platform()`'s error recommends.
    resolved_platforms = platforms or (FLEET_PLATFORMS if push else _native_platform())

    # Same manifest-list limitation as `build` -- see the comment there.
    if load and "," in resolved_platforms:
        raise ValueError(
            f"--load cannot export multiple platforms ({resolved_platforms}): the local "
            "docker image store holds no manifest lists. Build a single platform, or "
            "use --push to publish a manifest list to a registry."
        )

    target = base_image_uri(image_name, tag=base_image_tag())

    if push:
        _ecr_login(image_name, dry_run=dry_run)

    cmd = [
        "docker",
        "buildx",
        "build",
        "--file",
        "docker/Dockerfile.base",
        "--platform",
        resolved_platforms,
        # Same rationale as the per-SHA build: OCI attestation manifests confuse
        # the AWS ECS agent into pulling the wrong-arch variant on Graviton.
        "--provenance=false",
    ]
    for name, value in sorted(base_pins().items()):
        cmd.extend(["--build-arg", f"{name}={value}"])
    cmd.extend(["--tag", target])
    if push:
        cmd.append("--push")
    if load:
        cmd.append("--load")
    cmd.append(".")

    # Warned here as well as in `build`, and it matters MORE here: the base is the
    # expensive half (a full upstream bwa-mem2 build plus four cargo installs), so
    # an unbounded builder deposits far more cache per invocation.
    _warn_if_builder_is_not_gc_bounded(dry_run=dry_run)

    run_cmd(cmd, dry_run=dry_run, cwd=REPO_ROOT)
