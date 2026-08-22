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
