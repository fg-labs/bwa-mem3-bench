"""Sanity test — the package installs cleanly and exposes __version__."""

import bwa_mem3_bench

_SHA_LEN = 40


def test_version_string() -> None:
    assert bwa_mem3_bench.__version__ == "0.1.0"


def test_minibwa_sha_reads_canonical_pin() -> None:
    """minibwa_sha() returns the 40-char hex pin from build-arg-defaults.env,
    matching the vendored submodule commit."""
    sha = bwa_mem3_bench.minibwa_sha()
    assert len(sha) == _SHA_LEN
    assert all(c in "0123456789abcdef" for c in sha)


def test_holodeck_ref_reads_canonical_pin() -> None:
    """holodeck_ref() returns the fg-labs/holodeck git ref pinned in
    build-arg-defaults.env that the image cargo-installs `holodeck` from."""
    ref = bwa_mem3_bench.holodeck_ref()
    assert len(ref) == _SHA_LEN
    assert all(c in "0123456789abcdef" for c in ref)


def test_holodeck_repo_reads_canonical_pin() -> None:
    """holodeck_repo() returns the fg-labs/holodeck repo URL from
    build-arg-defaults.env, so `cli build` never hardcodes a stale repo."""
    assert bwa_mem3_bench.holodeck_repo() == "https://github.com/fg-labs/holodeck"
