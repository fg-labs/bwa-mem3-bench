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
