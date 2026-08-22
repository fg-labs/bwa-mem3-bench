"""Tests for ``bwa_mem3_bench.commands._submit`` helpers."""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import patch

import pytest

from bwa_mem3_bench.commands import _submit as submit_module
from bwa_mem3_bench.release_allowances import ReleaseAllowance


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


def test_target_bless_release_auto_fills_archs() -> None:
    """``bless_release`` is the full candidate-release matrix (all + fast +
    accuracy); its inputs iterate ARCHS, so — like ``all`` — it must expand to
    the full arch sweep when no ``--archs`` is given, or it silently shrinks to
    the single ``core_arch`` default."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(
            fg_labs_sha="deadbeef", target="bless_release", golden_ref_sha="prevsha"
        )

    archs = _captured_archs(mock_run.call_args_list)
    assert archs is not None, "expected ARCHS env override for target=bless_release"
    assert set(archs.split(",")) == {"c8g", "c7g", "c6a", "c7i", "c7a", "m7i"}


def test_bless_release_without_golden_ref_sha_raises_clear_cli_error() -> None:
    """A release bless with no --golden-ref-sha does not fail loudly on its own:
    Gate #2 (vs-golden) simply never gets requested, and the run "succeeds"
    with zero vs-golden data anywhere. This must be caught before the coordinator
    is ever submitted, not discovered after a multi-hour run finishes."""
    with (
        patch.object(submit_module, "run_cmd") as mock_run,
        pytest.raises(ValueError, match="bless_release requires --golden-ref-sha"),
    ):
        submit_module.submit(fg_labs_sha="deadbeef", target="bless_release")
    mock_run.assert_not_called()


def test_bless_release_with_golden_ref_sha_succeeds() -> None:
    """The happy path: --golden-ref-sha present, no error, submit proceeds."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(
            fg_labs_sha="deadbeef", target="bless_release", golden_ref_sha="prevsha"
        )
    mock_run.assert_called_once()


def test_other_targets_do_not_require_golden_ref_sha() -> None:
    """Only `bless_release` claims to be a release gate; `all` (and everything
    else) stays golden-optional -- an exploratory `all` run without a pinned
    golden is a normal, common workflow, not a footgun."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="all")
    mock_run.assert_called_once()


def test_target_smoke_does_not_auto_fill_archs() -> None:
    """``rule smoke`` iterates ``CONFIG.full_archs`` directly, so leave ARCHS unset."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="smoke")

    assert _captured_archs(mock_run.call_args_list) is None


def test_target_minibwa_auto_fills_archs() -> None:
    """``minibwa`` / ``minibwa_smoke`` iterate MINIBWA_ARCHS (= ARCHS minus m7i),
    so they must auto-fill the full sweep; m7i is filtered out in the Snakefile."""
    for target in ("minibwa", "minibwa_smoke"):
        with patch.object(submit_module, "run_cmd") as mock_run:
            submit_module.submit(fg_labs_sha="deadbeef", target=target)
        archs = _captured_archs(mock_run.call_args_list)
        assert archs is not None, f"expected ARCHS env override for target={target}"
        assert {"c6a", "c7i", "c7a", "c7g", "c8g"} <= set(archs.split(","))


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


def test_golden_ref_sha_resolves_alias_to_canonical() -> None:
    """A pinned golden SHA that is an *alias* (e.g. a squash-merged release tag)
    is resolved to the canonical to_sha its BAMs live under before being sent to
    the coordinator, so vs-golden finds the golden without a BAM re-copy."""
    entry = ReleaseAllowance(
        to_sha="9dd30dd0e5e477ddfd33bec752179978ac9f5a1d",
        pr="x",
        date="x",
        summary="x",
        expected_drift_pct=0.1,
        aliases=("b2fea467b776751e665a022c0f01319e7a92155b",),
    )
    with (
        patch.object(submit_module, "run_cmd") as mock_run,
        patch.object(submit_module, "load_allowances", return_value=[entry]),
    ):
        submit_module.submit(
            fg_labs_sha="newsha",
            target="all",
            golden_ref_sha="b2fea467b776751e665a022c0f01319e7a92155b",
        )
    assert (
        _captured_env(mock_run.call_args_list, "GOLDEN_REF_SHA")
        == "9dd30dd0e5e477ddfd33bec752179978ac9f5a1d"
    )


def test_golden_ref_sha_unresolvable_raises_clear_cli_error() -> None:
    """An unresolvable ``--golden-ref-sha`` (here: an ambiguous prefix matching
    two allowances) surfaces as one clear CLI error naming the SHA and the
    allowances file, not a raw traceback from deep in the resolver — ``submit``
    is a ``defopt`` entrypoint, so an uncaught ValueError/OSError would print a
    traceback. The failure short-circuits before any submit-job is issued."""
    ambiguous = [
        ReleaseAllowance(to_sha="44cbaec0", pr="x", date="x", summary="x", expected_drift_pct=0.1),
        ReleaseAllowance(to_sha="44cbaec1", pr="x", date="x", summary="x", expected_drift_pct=0.1),
    ]
    with (
        patch.object(submit_module, "run_cmd") as mock_run,
        patch.object(submit_module, "load_allowances", return_value=ambiguous),
        pytest.raises(ValueError, match="could not resolve --golden-ref-sha"),
    ):
        submit_module.submit(fg_labs_sha="newsha", target="all", golden_ref_sha="44cbaec")
    mock_run.assert_not_called()


def test_golden_ref_sha_unreadable_allowances_file_raises_clear_cli_error() -> None:
    """A missing/unreadable allowances file (OSError from ``load_allowances``) is
    wrapped in the same clear CLI error as an ambiguous SHA — both arms of the
    resolver's ``except (OSError, ValueError)`` re-raise, not just the ValueError
    one. Short-circuits before any submit-job is issued."""
    with (
        patch.object(submit_module, "run_cmd") as mock_run,
        patch.object(
            submit_module, "load_allowances", side_effect=FileNotFoundError("no such file")
        ),
        pytest.raises(ValueError, match="could not resolve --golden-ref-sha"),
    ):
        submit_module.submit(fg_labs_sha="newsha", target="all", golden_ref_sha="prevsha")
    mock_run.assert_not_called()


def test_dotted_default_job_name_is_sanitized() -> None:
    """AWS Batch job names allow only ``[A-Za-z0-9_-]``; a dotted identifier
    (e.g. an upstream tag like ``v2.2.1``) otherwise makes ``submit-job`` fail
    with exit 254. The default name derived from ``fg_labs_sha`` must be
    sanitized before it reaches the command."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="v2.2.1", target="baseline_all")
    name = _captured_job_name(mock_run.call_args_list)
    assert "." not in name
    assert name == "baseline_all-v2-2-1"
    assert all(c.isalnum() or c in "-_" for c in name)


def test_explicit_job_name_with_disallowed_chars_is_sanitized() -> None:
    """An explicit ``job_name`` with disallowed characters is sanitized too."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(
            fg_labs_sha="deadbeef", target="baseline_all", job_name="my.baseline/run"
        )
    name = _captured_job_name(mock_run.call_args_list)
    assert all(c.isalnum() or c in "-_" for c in name)
    assert name == "my-baseline-run"


def test_bless_baseline_runs_in_given_image_sha_with_valid_job_name() -> None:
    """``bless-baseline`` must run the baseline in a real, pushed image SHA (workers
    pull ``<ECR>:<fg_labs_sha>``) and produce a dot-free job name. The previous
    behaviour passed the upstream tag as the SHA, which is neither a valid image
    tag nor a valid job name."""
    bless_module = importlib.import_module("bwa_mem3_bench.commands._bless_baseline")
    with patch.object(submit_module, "run_cmd") as mock_run:
        bless_module.bless_baseline(fg_labs_sha="a887e36cabc")
    name = _captured_job_name(mock_run.call_args_list)
    assert name == "baseline_all-a887e36cabc"
    assert "." not in name
    assert _captured_env(mock_run.call_args_list, "FG_LABS_SHA") == "a887e36cabc"
    assert _captured_env(mock_run.call_args_list, "TARGET") == "baseline_all"
    assert _captured_env(mock_run.call_args_list, "REPS") == "5"


def test_forcerun_reaches_the_coordinator_env() -> None:
    """`--forcerun` must arrive as a FORCERUN container env override.

    Without it a re-run against an already-aligned SHA is a no-op: every output
    already exists in S3, and snakemake's rerun-triggers watch a rule's own
    definition rather than the binaries inside the image — so a compare-bams
    change alone triggers nothing.
    """
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(
            fg_labs_sha="deadbeef",
            target="all",
            forcerun="compare_vs_baseline compare_vs_golden",
        )

    assert (
        _captured_env(mock_run.call_args_list, "FORCERUN")
        == "compare_vs_baseline compare_vs_golden"
    )


def test_forcerun_omitted_sets_no_env() -> None:
    """An absent FORCERUN must not reach the entrypoint at all — an empty value
    there would expand to a bare `--forcerun`, which forces every rule and
    re-runs the whole alignment sweep."""
    with patch.object(submit_module, "run_cmd") as mock_run:
        submit_module.submit(fg_labs_sha="deadbeef", target="all")

    assert _captured_env(mock_run.call_args_list, "FORCERUN") is None
