"""Smoke tests for ``docker/coordinator-entrypoint.sh``.

The entrypoint runs inside the coordinator Batch container; the env vars
the AWS Batch overrides set drive its CONFIG_ARGS construction. These
tests stub out ``snakemake`` and ``python`` so the entrypoint runs to
completion and we can inspect the args that would have been passed.

Why test the shell rather than relying on inspection: the BUILD_VARIANT
derivation lives in one bash conditional (3-4 lines). It is easy to
break in subtle ways — e.g. an empty BUILD_VARIANT silently suffixing
the SHA with a trailing hyphen, or an explicit IMAGE_TAG override
getting clobbered by the derivation. Each of those would survive code
review and silently break the bench's S3 namespacing.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "coordinator-entrypoint.sh"


@pytest.fixture
def stubbed_path(tmp_path: Path) -> Iterator[str]:
    """Return a PATH value with stub `snakemake` and `python` shadowing real ones.

    The stubs echo their argv (snakemake → stdout, python → stderr) and exit
    0, so the entrypoint runs end-to-end and we can read CONFIG_ARGS from
    the stub-snakemake line.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "snakemake").write_text('#!/bin/sh\necho "[stub-snakemake] $*"\nexit 0\n')
    (bin_dir / "python").write_text('#!/bin/sh\necho "[stub-python] $*" >&2\nexit 0\n')
    for name in ("snakemake", "python"):
        path = bin_dir / name
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    yield f"{bin_dir}:{os.environ['PATH']}"


def _run_entrypoint(
    env_overrides: dict[str, str], stubbed_path: str, cwd: Path | None = None
) -> str:
    """Run the entrypoint with `env_overrides`; return the stub-snakemake stdout line.

    `cwd` matters for any test about unquoted expansion: a glob in an env var is
    resolved against the process's working directory, so reproducing that needs a
    directory whose contents are known.
    """
    env = {"PATH": stubbed_path, **env_overrides}
    result = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("[stub-snakemake]"):
            return line
    raise AssertionError(f"no stub-snakemake line in stdout:\n{result.stdout}")


def test_default_no_build_variant_passes_bare_sha(stubbed_path: str) -> None:
    """Without BUILD_VARIANT the snakemake config keeps the bare SHA + no image_tag.

    The Snakefile falls back to ``bwa-mem3-bench:{FG_LABS_SHA}`` when
    image_tag config is absent — we rely on that fallback for the
    default path.
    """
    line = _run_entrypoint({"FG_LABS_SHA": "deadbeef"}, stubbed_path)
    assert "fg_labs_sha=deadbeef" in line
    # ``deadbeef-`` (with a trailing hyphen) would be the failure mode if a
    # bogus empty BUILD_VARIANT got concatenated; assert against that explicitly.
    assert "fg_labs_sha=deadbeef-" not in line
    assert "image_tag=" not in line


def test_build_variant_lto_suffixes_sha_and_derives_image_tag(stubbed_path: str) -> None:
    """BUILD_VARIANT=lto-build must produce both the suffixed SHA AND the matching image_tag.

    This is the central invariant: a worker pulling the LTO image must
    write to the LTO S3 namespace, so a same-SHA default-build run does
    not clobber it. Asymmetry between the two values would silently
    introduce that collision.
    """
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "BUILD_VARIANT": "lto-build"},
        stubbed_path,
    )
    assert "fg_labs_sha=deadbeef-lto-build" in line
    assert "image_tag=deadbeef-lto-build" in line


def test_explicit_image_tag_override_wins_over_derived(stubbed_path: str) -> None:
    """An explicit IMAGE_TAG env var overrides the BUILD_VARIANT-derived default.

    Escape hatch for ad-hoc experiments where the caller has a manually-tagged
    image that doesn't follow the ``<sha>-<variant>`` convention.  The SHA
    suffixing still happens (output-namespace separation is not optional),
    only the image_tag is overridden.
    """
    line = _run_entrypoint(
        {
            "FG_LABS_SHA": "deadbeef",
            "BUILD_VARIANT": "lto-build",
            "IMAGE_TAG": "my-custom-tag",
        },
        stubbed_path,
    )
    assert "fg_labs_sha=deadbeef-lto-build" in line
    assert "image_tag=my-custom-tag" in line


def test_image_tag_alone_passes_through_without_sha_suffix(stubbed_path: str) -> None:
    """IMAGE_TAG without BUILD_VARIANT must NOT suffix the SHA.

    Some callers may want to override only the image (e.g. testing a
    locally-built ad-hoc image) without changing the S3 output namespace.
    The previous IMAGE_TAG-only mechanism (pre-BUILD_VARIANT) supported
    this — preserve it.
    """
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "IMAGE_TAG": "some-other-tag"},
        stubbed_path,
    )
    # No BUILD_VARIANT must leave the SHA bare — guarding against the
    # canonical failure mode where a bogus empty BUILD_VARIANT silently
    # suffixes the SHA with a trailing hyphen.
    assert "fg_labs_sha=deadbeef-" not in line
    assert "fg_labs_sha=deadbeef" in line
    assert "image_tag=some-other-tag" in line


def test_s3_bucket_env_propagates_to_config(stubbed_path: str) -> None:
    """BWA_MEM3_BENCH_S3_BUCKET must be threaded into the snakemake config.

    Worker Batch jobs re-parse the Snakefile but their job definitions do not
    carry the coordinator's bucket env, so the golden-listing helper falls back
    to a wrong default bucket (NoSuchBucket) and the whole golden-gated run
    aborts. snakemake `--config` *does* propagate to workers, so threading the
    bucket through config is what makes the worker-side golden lookup resolve
    the right bucket. Regression guard for that propagation.
    """
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "BWA_MEM3_BENCH_S3_BUCKET": "fg-bwa-mem3-bench"},
        stubbed_path,
    )
    assert "s3_bucket=fg-bwa-mem3-bench" in line


def test_s3_bucket_absent_from_config_when_env_unset(stubbed_path: str) -> None:
    """With no bucket env set, no `s3_bucket` config key is emitted — the
    Snakefile then falls back to `aws_config.load()` (correct for local runs)."""
    line = _run_entrypoint({"FG_LABS_SHA": "deadbeef"}, stubbed_path)
    assert "s3_bucket=" not in line


def test_missing_fg_labs_sha_errors(stubbed_path: str) -> None:
    """FG_LABS_SHA is required — the entrypoint must exit non-zero if it's unset."""
    result = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env={"PATH": stubbed_path},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    # The ``${VAR:?msg}`` construct emits "FG_LABS_SHA is required" to stderr.
    assert "FG_LABS_SHA" in result.stderr


def test_docstring_example_matches_implementation() -> None:
    """The entrypoint's header comment documents BUILD_VARIANT semantics; keep them in sync.

    If the implementation diverges from the documented contract (e.g. somebody
    changes the suffix separator from ``-`` to ``_`` without updating the
    comment block), this test makes the inconsistency visible.
    """
    contents = ENTRYPOINT.read_text()
    # The implementation we're vouching for:
    assert 'FG_LABS_SHA="${FG_LABS_SHA}-${BUILD_VARIANT}"' in contents
    # The documented contract: suffix is "-<build_variant>".
    expected_doc = "`-<build_variant>`"
    assert expected_doc in contents, textwrap.dedent(f"""
        Documented suffix in entrypoint header drifted from implementation.
        Expected substring: {expected_doc!r}
        Update both the comment block and this test together.
    """)


def test_forcerun_passes_rules_through_to_snakemake(stubbed_path: str) -> None:
    """FORCERUN reaches snakemake as `--forcerun <rules>`.

    Needed because a rule's outputs already existing in S3 makes snakemake skip
    it, and the thing that changed may be a BINARY inside the image rather than
    any rule's shell text — which no rerun-trigger detects. Re-deriving the
    compare artifacts for an already-aligned SHA is exactly that case, and
    without this the coordinator runs and does nothing.
    """
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "FORCERUN": "compare_vs_baseline compare_vs_golden"},
        stubbed_path,
    )
    assert "--forcerun compare_vs_baseline compare_vs_golden" in line


def test_forcerun_absent_adds_no_flag(stubbed_path: str) -> None:
    """An unset FORCERUN must not emit a bare `--forcerun`, which snakemake
    interprets as "force every rule" — re-running a whole alignment sweep."""
    line = _run_entrypoint({"FG_LABS_SHA": "deadbeef"}, stubbed_path)
    assert "--forcerun" not in line


@pytest.mark.parametrize("blank", ["   ", "\t", " \n "])
def test_forcerun_whitespace_only_adds_no_flag(blank: str, stubbed_path: str) -> None:
    """A whitespace-only FORCERUN is the same accident as an unset one, and must
    not degrade into a bare `--forcerun`.

    `-n` is true for a string of spaces, so the naive guard admits it and the
    unquoted expansion then contributes no words — leaving `--forcerun` alone on
    the command line, i.e. "force EVERY rule". On an already-aligned SHA that is
    a full re-run of the alignment sweep: the exact outcome the rule list exists
    to avoid, triggered by a stray space in a Batch override.
    """
    line = _run_entrypoint({"FG_LABS_SHA": "deadbeef", "FORCERUN": blank}, stubbed_path)
    assert "--forcerun" not in line


def test_forcerun_rules_are_not_glob_expanded(stubbed_path: str, tmp_path: Path) -> None:
    """A `*` in FORCERUN must reach snakemake literally, not as filenames.

    The coordinator's working directory is not empty, so an unquoted expansion
    resolves the pattern against whatever happens to be sitting there and hands
    snakemake paths where it expects rule names. Splitting with `read -ra`
    performs word splitting without pathname expansion, which is the distinction
    that matters here.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "compare_decoy.txt").write_text("")
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "FORCERUN": "compare_*"}, stubbed_path, cwd=workdir
    )
    assert "--forcerun compare_*" in line
    assert "compare_decoy.txt" not in line


def test_forcerun_rules_survive_a_newline_separator(stubbed_path: str) -> None:
    """Rules split across lines must all reach snakemake.

    `read` stops at the first newline, so splitting the raw value would keep
    `compare_vs_baseline` and silently drop `compare_vs_golden` — a forced re-run
    that quietly forces less than it was told to. The unquoted form this replaced
    split on newlines too, since they are in the default IFS.
    """
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "FORCERUN": "compare_vs_baseline\ncompare_vs_golden"},
        stubbed_path,
    )
    assert "--forcerun compare_vs_baseline compare_vs_golden" in line


def test_config_args_are_not_glob_expanded(stubbed_path: str, tmp_path: Path) -> None:
    """The same protection for `--config`, which carries the same kind of value.

    Every CONFIG_ARGS entry comes from a Batch env override, so it is exposed to
    exactly what FORCERUN was. Kept as a separate guard because the two are built
    by different code -- an array append here, a `read -ra` split there -- and a
    refactor of either could quietly reintroduce the string form.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "samples_decoy.txt").write_text("")
    line = _run_entrypoint(
        {"FG_LABS_SHA": "deadbeef", "SAMPLES": "samples_*"}, stubbed_path, cwd=workdir
    )
    assert "samples=samples_*" in line
    assert "samples_decoy.txt" not in line
