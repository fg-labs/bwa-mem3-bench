"""Sanity test — the package installs cleanly and exposes __version__."""

import bwa_mem3_bench


def test_version_string() -> None:
    assert bwa_mem3_bench.__version__ == "0.1.0"
