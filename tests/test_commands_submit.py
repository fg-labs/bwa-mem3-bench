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
    return _captured_env(call_args_list, "ARCHS")


def _captured_env(call_args_list: list[Any], name: str) -> str | None:
    """Pull an arbitrary env override by name from the captured submit-job command."""
    cmd = call_args_list[0].args[0]
    overrides = json.loads(cmd[cmd.index("--container-overrides") + 1])
    for entry in overrides["environment"]:
        if entry["name"] == name:
            return str(entry["value"])
    return None


def _captured_job_name(call_args_list: list[Any]) -> str:
    """Pull the ``--job-name`` flag value from the captured submit-job command."""
    cmd = call_args_list[0].args[0]
    return str(cmd[cmd.index("--job-name") + 1])


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


def test_make_target_empty_omits_build_variant_env() -> None:
    """Without ``--make-target`` the coordinator must not see ``BUILD_VARIANT``.

    The coordinator entrypoint's derivation runs iff BUILD_VARIANT is non-empty.
    Sending an empty value would still trigger the bash ``-n`` check and
    suffix the SHA with an empty string — so the right behavior is to
    omit the env var entirely on the default path.
    """
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="smoke")

    assert _captured_env(mock_run.call_args_list, "BUILD_VARIANT") is None
    assert _captured_env(mock_run.call_args_list, "IMAGE_TAG") is None
    assert _captured_job_name(mock_run.call_args_list) == "smoke-deadbeef"


def test_make_target_sets_build_variant_and_job_name() -> None:
    """``--make-target lto-build`` propagates BUILD_VARIANT and suffixes the job name.

    BUILD_VARIANT is the single source of truth for the variant suffix;
    the coordinator entrypoint derives both ``IMAGE_TAG`` and the
    snakemake ``fg_labs_sha`` config from it. Sending the composed tag
    directly here (as a previous draft did) would risk drift between the
    two derived values if either changed shape — keep the composition
    in coordinator-entrypoint.sh.
    """
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="smoke", make_target="lto-build")

    assert _captured_env(mock_run.call_args_list, "BUILD_VARIANT") == "lto-build"
    # IMAGE_TAG is NOT set by submit.py — the coordinator derives it.
    assert _captured_env(mock_run.call_args_list, "IMAGE_TAG") is None
    assert _captured_job_name(mock_run.call_args_list) == "smoke-deadbeef-lto-build"


def test_golden_ref_sha_sets_env_override_when_provided() -> None:
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="newsha", target="all", golden_ref_sha="prevsha")
    assert _captured_env(mock_run.call_args_list, "GOLDEN_REF_SHA") == "prevsha"


def test_golden_ref_sha_absent_by_default() -> None:
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="newsha", target="all")
    assert _captured_env(mock_run.call_args_list, "GOLDEN_REF_SHA") is None
