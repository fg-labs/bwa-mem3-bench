"""Tests for ``bwa_mem3_bench.commands.submit`` helpers."""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import patch

submit_module = importlib.import_module("bwa_mem3_bench.commands.submit")


def _captured_archs(call_args_list: list[Any]) -> str | None:
    """Pull the ``ARCHS`` env var from the most recent ``run_cmd`` call, if any.

    ``run_cmd`` is invoked with the full ``aws batch submit-job`` argv; the
    ``--container-overrides`` JSON blob carries the environment overrides.
    """
    cmd = call_args_list[0].args[0]
    overrides = json.loads(cmd[cmd.index("--container-overrides") + 1])
    for entry in overrides["environment"]:
        if entry["name"] == "ARCHS":
            return str(entry["value"])
    return None


def test_target_all_auto_fills_archs_to_full_archs() -> None:
    """``--target all`` with no ``--archs`` must expand to the full arch sweep.

    Without this the Snakefile silently falls back to ``core_arch`` (a
    single-arch default) and the "full benchmark" only exercises one arch.
    """
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="all")

    archs = _captured_archs(mock_run.call_args_list)
    assert archs is not None, "expected ARCHS env override for target=all"
    assert set(archs.split(",")) == {"c8g", "c7g", "c6a", "c7i", "c7a", "m7i"}


def test_target_baseline_all_auto_fills_archs() -> None:
    """``baseline_all`` has the same footgun shape as ``all`` and gets the same fix."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="baseline_all")

    archs = _captured_archs(mock_run.call_args_list)
    assert archs is not None
    assert "c6a" in archs.split(",")
    assert "c8g" in archs.split(",")


def test_target_smoke_does_not_auto_fill_archs() -> None:
    """``rule smoke`` iterates ``CONFIG.full_archs`` directly, so leave ARCHS unset."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="smoke")

    assert _captured_archs(mock_run.call_args_list) is None


def test_explicit_archs_override_wins_for_full_sweep_target() -> None:
    """Explicit ``--archs`` always takes precedence over the auto-fill."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="all", archs="c6a")

    assert _captured_archs(mock_run.call_args_list) == "c6a"
