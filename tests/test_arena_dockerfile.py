"""Guards for the arena release-build `RUN` blocks in `docker/Dockerfile.base`.

Real bug caught by CodeRabbit on the PR that added these blocks: `&&` and `||`
have EQUAL precedence in POSIX shell and associate left to right, so

    clone && cd && fetch && checkout && submodule && sed || true && if ...

parses as `((clone && cd && fetch && checkout && submodule && sed) || true) &&
if ...` -- a failed `git fetch`/`git checkout`/`git submodule update` is
absorbed by the trailing `|| true`, which was only ever meant to tolerate a
missing `__rdtsc` redefinition in `sed`. `cd` has already succeeded by the
time any of those fail, so `make`/`install` still run against whatever the
`git clone` left checked out (the default branch) and install it as
`/out/bin/bwa-mem3.<label>` -- a historical release silently mislabeled as a
different commit. The arena treats each of these as a pinned release and
`bless_release` gates on arena output, so this is a correctness bug in the
comparison the arena exists to make, not just a build nicety.
"""

from __future__ import annotations

from pathlib import Path

from bwa_mem3_bench import REPO_ROOT

DOCKERFILE_BASE = Path(REPO_ROOT) / "docker" / "Dockerfile.base"

_VULNERABLE_FORM = "sed -i '/^static inline unsigned long long __rdtsc/,/^}$/d' src/utils.h || true"
_SAFE_FORM = "{ " + _VULNERABLE_FORM + "; }"
# Below this, something is badly wrong with the block-splitting regex itself,
# not with any individual release's block -- arena.smk pins at least this many
# historical releases today.
_MIN_EXPECTED_BLOCKS = 2


def _arena_run_blocks() -> list[str]:
    """Each `RUN git clone ... /build/arena-releases/<label> && ...` block."""
    text = DOCKERFILE_BASE.read_text()
    starts = [
        i
        for i in range(len(text))
        if text.startswith("RUN git clone", i) and (i == 0 or text[i - 1] == "\n")
    ]
    starts = [i for i in starts if "/build/arena-releases/" in text[i : text.index("\n", i)]]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append(text[start:end])
    return blocks


def test_arena_dockerfile_has_a_run_block_per_historical_release() -> None:
    blocks = _arena_run_blocks()
    assert len(blocks) >= _MIN_EXPECTED_BLOCKS, (
        "no arena `RUN git clone .../arena-releases/<label>` blocks found"
    )


def test_every_arena_run_block_isolates_the_sed_fallback() -> None:
    """`|| true` must cover ONLY the `sed` command, never the clone/fetch/
    checkout/submodule chain before it -- see the module docstring."""
    for block in _arena_run_blocks():
        label_line = block.splitlines()[0]
        assert _SAFE_FORM in block, (
            f"{label_line}: `sed ... || true` must be grouped as `{{ sed ... || "
            "true; }}` so an earlier clone/fetch/checkout/submodule failure "
            "cannot be silently absorbed by it"
        )
        assert _VULNERABLE_FORM + " && " not in block.replace(_SAFE_FORM, ""), (
            f"{label_line}: found the ungrouped, unsafe `sed ... || true &&` form"
        )


# ---------------------------------------------------------------------------
# LTO: every bwa-mem3 build must be the binary the release ships.
#
# Since fg-labs/bwa-mem3 #621 (v0.15.1) the default build links with full LTO,
# but ONLY when a probe link with `-fuse-ld=lld` succeeds; without lld it falls
# back to a non-LTO binary and says so in a single make NOTE. The base image
# shipped clang-19 without lld-19, so every bench image from v0.15.1 through the
# v0.17.0 candidate timed a non-LTO binary on both architectures. These guards
# keep lld installed and keep every build asserting the resolved mode.
# ---------------------------------------------------------------------------

DOCKERFILE = Path(REPO_ROOT) / "docker" / "Dockerfile"
# tuned arm64, portable arm64, x86.
_DEFAULT_CANDIDATE_BUILDS = 3
_ARENA_MAKES = (
    'make arch=arm64 -j"$(nproc)" CXX=clang++ CC=clang',
    'make BASELINE_ARCH=avx2 -j"$(nproc)" CXX=clang++ CC=clang',
)


def test_the_base_image_installs_lld_next_to_clang() -> None:
    text = DOCKERFILE_BASE.read_text()
    assert "clang-${LLVM_VERSION} lld-${LLVM_VERSION}" in text, (
        "Dockerfile.base must install lld-${LLVM_VERSION} with clang: without it bwa-mem3's "
        "LTO probe fails and the build silently drops to a non-LTO binary"
    )


def test_the_base_image_defines_require_lto() -> None:
    text = DOCKERFILE_BASE.read_text()
    assert "> /usr/local/bin/require-lto" in text
    assert "print-BWA_LTO_MODE" in text
    assert '"$mode" != full' in text


def test_every_arena_build_asserts_full_lto() -> None:
    for block in _arena_run_blocks():
        label_line = block.splitlines()[0]
        for make in _ARENA_MAKES:
            assert block.count(make) == 1, f"{label_line}: expected one `{make}`"
            after = block.split(make, 1)[1]
            assert after.startswith(" && \\\n            require-lto "), (
                f"{label_line}: `{make}` must be followed by `&& require-lto <same args>`"
            )


def test_every_default_candidate_build_asserts_full_lto() -> None:
    """The three default-target builds in docker/Dockerfile (tuned arm64, portable
    arm64, x86) each end in `require-lto` with the make arguments they built with."""
    text = DOCKERFILE.read_text()
    default_case = text.split('      "" ) \\\n', 1)[1].split("      lto-build ) \\\n", 1)[0]
    makes = default_case.count("make arch=") + default_case.count("make BASELINE_ARCH=")
    assert makes == _DEFAULT_CANDIDATE_BUILDS, (
        f"expected {_DEFAULT_CANDIDATE_BUILDS} default builds, found {makes}"
    )
    assert default_case.count("require-lto ") == _DEFAULT_CANDIDATE_BUILDS, (
        "every default bwa-mem3 build in docker/Dockerfile must end in `require-lto`"
    )
    for args in (
        'arch=arm64 ARM_CPU="${TUNE_ARM_CPU}" ARM_CACHE_LINE="${TUNE_ARM_CACHE_LINE}"',
        "arch=arm64 CXX=clang++ CC=clang",
        'BASELINE_ARCH="${BASELINE_ARCH:-avx2}" CXX=clang++ CC=clang',
    ):
        assert f"require-lto {args}" in default_case, f"missing `require-lto {args}`"
