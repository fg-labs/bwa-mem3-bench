"""Guards for per-trial host attribution.

Host metadata answers "which machine ran this alignment", which is what lets a
timing difference between two runs be attributed to a host rather than guessed
at. It has failed silently twice, in two different ways:

1. The IMDS call was tokenless IMDSv1. AL2023 enforces IMDSv2, and ``curl -s``
   ignores the HTTP status and exits 0, so the ``|| echo local`` fallback never
   fired and an EMPTY STRING was written. Every meta.json written before
   2026-07-24 has ``instance_type: ""``.

2. Once that was fixed, the values were real but described the WRONG MACHINE:
   ``emit_meta`` was a snakemake ``localrule``, so it ran on the coordinator. A
   job on the m7i queue reported ``instance_type: c6a.large`` (the coordinator's
   type) and two different m7i workers reported the same ``instance_id``.

Both failures are silent by construction — metadata degrades rather than failing
the job. These tests are the only thing standing between us and a third round.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT

# Arbitrary fixture values for the off-EC2 smoke; named so the assertions
# below are not magic literals.
FIXTURE_REP = 3

# Exit code the script uses for a malformed argument list.
USAGE_EXIT = 2

# A deterministically dead IMDS endpoint: port 1 refuses immediately, so the
# degraded path is exercised in milliseconds and does not depend on whether the
# machine running the tests happens to sit on EC2 (where the real link-local
# address would answer and the `unknown` assertions below would fail).
DEAD_IMDS_ENV = {"AWS_EC2_METADATA_SERVICE_ENDPOINT": "http://127.0.0.1:1"}

SCRIPT_REPO_PATH = "docker/emit-host-meta.sh"
INSTALLED_SCRIPT = "/usr/local/bin/emit-host-meta"
SCRIPT = Path(REPO_ROOT) / "docker" / "emit-host-meta.sh"
DOCKERFILE = Path(REPO_ROOT) / "docker" / "Dockerfile"
SNAKEFILE = Path(REPO_ROOT) / "workflow" / "Snakefile"
ALIGN_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "align.smk"


def test_script_uses_imdsv2_token() -> None:
    """A tokenless GET silently yields empty fields on AL2023."""
    text = SCRIPT.read_text()
    assert "api/token" in text and "-X PUT" in text, (
        "emit-host-meta must request an IMDSv2 session token; a tokenless "
        "IMDSv1 GET returns an error that curl -s swallows, writing empty "
        "strings for every field."
    )
    assert "X-aws-ec2-metadata-token:" in text, (
        "emit-host-meta must send the IMDSv2 token header on metadata GETs."
    )


def _curl_invocations(text: str) -> list[str]:
    """Every `curl ...` command in the script, line continuations joined.

    Comment lines are dropped first — the script's own commentary discusses curl's
    exit-status behaviour at length and those sentences are not invocations. Line
    continuations are then joined so a flag written on the next line still counts
    as part of the command it belongs to.
    """
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    joined = re.sub(r"\\\n\s*", " ", code)
    return re.findall(r"\bcurl\b[^\n]*", joined)


def _requests_failure_on_http_error(invocation: str) -> bool:
    """True if this curl invocation passes -f/--fail (bundled short flags count)."""
    return any(
        token == "--fail" or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", token)
        for token in invocation.split()
    )


def test_script_fails_curl_on_http_error() -> None:
    """`curl -s` exits 0 on a 4xx, so fallbacks never fire without `-f`.

    Checked per invocation rather than by rejecting the literal string `curl -s`:
    that spelling is only one of many silent-but-not-failing forms (`-sS`,
    `--silent`), all of which swallow the HTTP status the same way.
    """
    invocations = _curl_invocations(SCRIPT.read_text())
    assert invocations, "expected emit-host-meta to call curl at all"
    for invocation in invocations:
        assert _requests_failure_on_http_error(invocation), (
            f"curl invocation without -f/--fail: {invocation!r}. curl then exits 0 "
            "on an HTTP error and the `|| echo unknown` fallback never fires, "
            "which is exactly how the original IMDSv1 bug wrote empty strings "
            "undetected."
        )


def test_script_captures_instance_id() -> None:
    """instance_id is what distinguishes 'shared a host' from 'same AZ'."""
    assert "instance-id" in SCRIPT.read_text(), (
        "emit-host-meta must capture instance-id; without it two reps that "
        "contended on one host are indistinguishable from two in the same AZ."
    )


def test_align_rule_writes_its_own_host_metadata() -> None:
    """The align rule must emit meta.json itself, on the machine doing the work."""
    text = ALIGN_SMK.read_text()
    assert "emit-host-meta" in text, (
        "align.smk does not invoke emit-host-meta; host identity must be "
        "captured in the same shell body as the aligner."
    )
    assert "benchmarks/meta.json" in text, "align.smk must declare meta.json as an output"


def test_no_separate_meta_rule_exists() -> None:
    """A standalone metadata rule records the wrong host, wherever it runs.

    As a localrule it records the coordinator. Removed from localrules it
    becomes its own Batch job on some other arbitrary instance in the queue.
    Neither is the machine that ran the alignment.
    """
    assert not (Path(REPO_ROOT) / "workflow" / "rules" / "benchmark.smk").exists(), (
        "workflow/rules/benchmark.smk is back; host metadata must be written by "
        "the align rules, not by a separate rule."
    )
    # Look for a rule DEFINITION or a localrules entry, not the bare word — the
    # Snakefile explains this history in prose and must be allowed to.
    smk_files = [SNAKEFILE, *(Path(REPO_ROOT) / "workflow" / "rules").glob("*.smk")]
    for path in smk_files:
        text = path.read_text()
        assert not re.search(r"^\s*rule\s+emit_meta\s*:", text, re.MULTILINE), (
            f"{path.name} defines a standalone emit_meta rule; it records the "
            "wrong host wherever it runs."
        )
        assert not re.search(r"^\s+emit_meta\s*,\s*$", text, re.MULTILINE), (
            f"{path.name} lists emit_meta in localrules; that is what made it "
            "record the coordinator's identity instead of the worker's."
        )


def test_dockerfile_installs_the_script() -> None:
    """The rule shells out to `emit-host-meta`; it has to be on PATH, executable.

    Asserts the actual instructions, not the bare word — which a mere mention in
    a comment would satisfy while the rule still died with "command not found".
    """
    text = DOCKERFILE.read_text()
    assert re.search(
        rf"^\s*COPY\s+{re.escape(SCRIPT_REPO_PATH)}\s+{re.escape(INSTALLED_SCRIPT)}\s*$",
        text,
        re.MULTILINE,
    ), f"Dockerfile does not COPY {SCRIPT_REPO_PATH} to {INSTALLED_SCRIPT}"
    assert re.search(rf"chmod\s+\+x\s+{re.escape(INSTALLED_SCRIPT)}\b", text), (
        f"Dockerfile does not chmod +x {INSTALLED_SCRIPT}; the rule would get "
        "'permission denied' instead of host metadata."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_script_degrades_cleanly_off_ec2() -> None:
    """With no IMDS it must emit valid JSON with `unknown`, not hang or fail.

    Metadata is diagnostic, never load-bearing: a metadata failure must not take
    down an alignment that otherwise succeeded.
    """
    out = subprocess.run(
        [str(SCRIPT), "deadbeef", "wgs-5M", "m7i", str(FIXTURE_REP)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, **DEAD_IMDS_ENV},
    )
    payload = json.loads(out.stdout)
    assert payload["fg_labs_sha"] == "deadbeef"
    assert payload["sample"] == "wgs-5M"
    assert payload["arch"] == "m7i"
    assert payload["rep"] == FIXTURE_REP
    # With IMDS unreachable these must be the sentinel — never empty, which is
    # what the original bug produced and what read as "no data".
    for key in ("instance_type", "availability_zone", "instance_id"):
        assert payload[key] == "unknown", f"{key} should be 'unknown' without IMDS"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_script_cannot_abort_the_alignment() -> None:
    """A hostile environment still yields exit 0 and one valid record.

    The align rule invokes this inside a `set -e` shell body, before any
    alignment work: a non-zero exit would kill the job outright. Stripping PATH
    removes curl, python3 and uname at once — every external command the script
    depends on — which is the strongest available proxy for "the environment did
    not cooperate".
    """
    out = subprocess.run(
        [str(SCRIPT), "deadbeef", "wgs-5M", "m7i", str(FIXTURE_REP)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"PATH": "/nonexistent", **DEAD_IMDS_ENV},
    )
    payload = json.loads(out.stdout)  # exactly one record, not a half-written one
    assert payload["rep"] == FIXTURE_REP
    for key in ("instance_type", "availability_zone", "instance_id", "kernel"):
        assert payload[key] == "unknown", f"{key} should degrade to 'unknown'"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
@pytest.mark.parametrize(
    "args",
    [
        ["deadbeef", "wgs-5M", "m7i"],  # missing rep
        ["deadbeef", "wgs-5M", "m7i", "3", "extra"],
        ["deadbeef", "wgs-5M", "m7i", "not-a-number"],  # rep is a JSON number
    ],
)
def test_script_rejects_a_malformed_argument_list(args: list[str]) -> None:
    """Bad ARGUMENTS are a workflow bug and must fail loudly, unlike a bad env.

    The degrade-to-unknown contract exists for environments the worker cannot
    control. Swallowing a wrong call as well would hide a broken rule behind a
    file full of sentinels — the failure mode this whole module exists to stop.
    """
    out = subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # a non-zero exit is the point of this test
        env={**os.environ, **DEAD_IMDS_ENV},
    )
    assert out.returncode == USAGE_EXIT, f"expected a usage failure, got {out.returncode}"
    assert out.stdout == "", "a usage error must not write a metadata record"
    assert "usage:" in out.stderr
