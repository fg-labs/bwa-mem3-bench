"""Identity and tagging for the builder base image.

The per-SHA benchmark image is built ``FROM`` a base image carrying everything
``FG_LABS_SHA`` does *not* invalidate: the clang-19 toolchain and its wrappers,
the Rust toolchains, the upstream bwa-mem2 build, and the four cargo-installed
pinned tools (tricord, holodeck, fgumi, tachyon). Those layers are expensive --
four cargo installs plus a full bwa-mem2 ``make multi`` -- and byte-identical
across every fg-labs SHA benchmarked against the same pins.

Splitting them out converts them from *build cache* into an ordinary *image
pull*. Build cache has to be retained locally, per architecture, and is evicted
under any GC pressure; a pulled base layer is shared, content-addressed, and
survives a cache wipe. It also means a per-SHA build compiles only bwa-mem3,
which is the only thing that actually changed.

The base image's tag must be a function of *every* input that can change its
contents. Keying it on ``UPSTREAM_TAG`` alone would be a correctness bug: bumping
``HOLODECK_REF`` while ``UPSTREAM_TAG`` stayed at ``v2.2.1`` would silently reuse
a base image built from the old holodeck, and the resulting benchmark would
attribute the difference to bwa-mem3. Hashing ``Dockerfile.base`` itself covers
the pins that live there as ``ARG`` defaults (``LLVM_VERSION``,
``TRICORD_VERSION``) and the hardcoded Rust toolchain version, so editing the
recipe also forces a new tag.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, _build_arg_default

#: Suffix appended to the benchmark image name to form the base image's ECR
#: repository. The base image lives in its OWN repository rather than sharing
#: the benchmark repo, because the benchmark repo's lifecycle rule keeps only
#: the last 30 tagged images with ``tagStatus: ANY``. A base image is rebuilt
#: rarely and so is always among the oldest tags -- it would be evicted after
#: ~30 SHA pushes, and the failure would surface as a `manifest unknown` on the
#: next cold build rather than at the moment of deletion.
BASE_REPO_SUFFIX = "-base"

#: Build-arg pins, read from ``docker/build-arg-defaults.env``, whose values are
#: baked into the base image. Anything NOT listed here either belongs to the
#: per-SHA layer (``FG_LABS_*``), to the runtime stage (``SAMTOOLS_VERSION``,
#: ``BWA_VERSION``, ``BWAMETH_VERSION``), or is a label rather than a build
#: input (``MINIBWA_SHA``).
BASE_PIN_NAMES = (
    "UPSTREAM_REPO",
    "UPSTREAM_TAG",
    "HOLODECK_REPO",
    "HOLODECK_REF",
    "FGUMI_REPO",
    "FGUMI_REF",
    "TACHYON_VERSION",
)

#: Length of the hex digest kept in the tag. Twelve hex characters is the same
#: order of collision resistance as an abbreviated git SHA and keeps the tag
#: readable next to its `UPSTREAM_TAG` prefix.
_DIGEST_CHARS = 12


def base_dockerfile() -> Path:
    """Return the path to the base image's Dockerfile.

    :return: absolute path to ``docker/Dockerfile.base``.
    """
    return REPO_ROOT / "docker" / "Dockerfile.base"


def base_pins() -> dict[str, str]:
    """Resolve every pin baked into the base image from the canonical env file.

    :return: mapping of build-arg name to its pinned value.
    """
    return {name: _build_arg_default(name) for name in BASE_PIN_NAMES}


def base_image_tag(
    *,
    pins: Mapping[str, str] | None = None,
    dockerfile: Path | None = None,
) -> str:
    """Compute the content-addressed tag for the base image.

    The tag is ``<upstream-tag>-<digest>``: a human-readable prefix so the tag
    means something at a glance in ECR, plus a digest over the full pin set and
    the Dockerfile that makes it precise. Any change to either produces a
    different tag, so a stale base can never be silently reused.

    :param pins: build-arg pins to key on. Defaults to :func:`base_pins`.
    :param dockerfile: recipe to hash. Defaults to :func:`base_dockerfile`.
    :return: the tag, e.g. ``v2.2.1-9f2c1a4b7e03``.
    :raises FileNotFoundError: if the Dockerfile does not exist.
    """
    resolved_pins = dict(pins) if pins is not None else base_pins()
    resolved_dockerfile = dockerfile if dockerfile is not None else base_dockerfile()

    digest = hashlib.sha256()
    digest.update(resolved_dockerfile.read_bytes())
    # Sorted so the digest depends on the pin VALUES, never on dict ordering.
    for name in sorted(resolved_pins):
        digest.update(f"\n{name}={resolved_pins[name]}".encode())

    return f"{resolved_pins['UPSTREAM_TAG']}-{digest.hexdigest()[:_DIGEST_CHARS]}"


def base_image_uri(image_name: str, *, tag: str | None = None) -> str:
    """Return the full base image reference for a given benchmark image name.

    :param image_name: the benchmark image name, sans ``:<tag>``. An ECR URI
        (``<acct>.dkr.ecr.<region>.amazonaws.com/bwa-mem3-bench``) yields the
        sibling repository ``...amazonaws.com/bwa-mem3-bench-base``.
    :param tag: override the computed tag. Defaults to :func:`base_image_tag`.
    :return: e.g. ``<acct>.dkr.ecr.<region>.amazonaws.com/bwa-mem3-bench-base:v2.2.1-9f2c1a4b7e03``.
    """
    return f"{image_name}{BASE_REPO_SUFFIX}:{tag or base_image_tag()}"
