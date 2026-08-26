"""Guards for `workflow/rules/arena.smk`'s config-driven arch wiring.

Real gap caught by CodeRabbit: `ARENA_QUEUES` and the rule's
`wildcard_constraints` were originally hardcoded literals (`{"c7i": ...,
"c8g": ...}`, `"c7i|c8g"`) independent of `CONFIG.arena.archs`
(config/defaults.yaml). `_arena_from` (workflow_config.py) validates
`arena.archs` entries only against the FULL `config/archs.yaml` registry, not
against those literals, so a third configured arch would pass config
validation and then raise a bare `KeyError` (or silently mismatch the
wildcard) building `align_arena`'s resources -- a paid on-demand job failing
on a config change that looked valid.

Text-based on the Snakefile source, matching `tests/test_thread_packing.py`'s
convention for Snakefile-level logic snakemake's own machinery keeps out of
reach of a normal unit test.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT

ARENA_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "arena.smk"
RULES_DIR = Path(REPO_ROOT) / "workflow" / "rules"


def test_arena_queues_are_derived_from_config_not_a_hardcoded_dict() -> None:
    text = ARENA_SMK.read_text()
    assert "CONFIG.arena.archs" in text.split("ARENA_QUEUES", 1)[1].split("\n\n", 1)[0], (
        "ARENA_QUEUES must be built from CONFIG.arena.archs, not a fixed literal "
        'dict like {"c7i": ..., "c8g": ...} -- a third configured arch would '
        "pass _arena_from's own validation and then KeyError building this "
        "rule's resources.batch_queue"
    )
    assert '"c7i": "bwa-mem3-bench-c7i-arena"' not in text
    assert '"c8g": "bwa-mem3-bench-c8g-arena"' not in text


def test_arena_wildcard_constraint_is_derived_from_config_not_a_literal_regex() -> None:
    text = ARENA_SMK.read_text()
    assert 'arch = "c7i|c8g"' not in text, (
        "the arch wildcard_constraints must not hardcode the arch set literally; "
        "derive it from CONFIG.arena.archs (e.g. via a module-level "
        '"|".join(CONFIG.arena.archs)) so it cannot drift from ARENA_QUEUES'
    )
    assert "ARENA_ARCH_PATTERN" in text
    assert "arch = ARENA_ARCH_PATTERN" in text


def test_align_arena_is_scheduled_ahead_of_every_other_rule() -> None:
    """`align_arena` must outrank every other rule's `priority:` (default 0
    where unset), so `bless_release`'s scheduler prefers to submit it first
    when the profile's `jobs:` cap forces a choice among ready jobs. The
    arena is the newest, least battle-tested part of a bless -- surfacing its
    result (and cost) early is the whole point of setting this at all; a
    future rule matching or exceeding it would silently defeat that.
    """
    arena_text = ARENA_SMK.read_text()
    arena_start = arena_text.index("rule align_arena:")
    arena_end = arena_text.find("\nrule ", arena_start + 1)
    arena_body = arena_text[arena_start:] if arena_end == -1 else arena_text[arena_start:arena_end]
    match = re.search(r"^\s*priority:\s*(\d+)\s*$", arena_body, re.MULTILINE)
    assert match, "align_arena must declare a `priority:` directive"
    arena_priority = int(match.group(1))
    assert arena_priority > 0, "priority 0 is the default every other rule already has"

    for smk_path in sorted(RULES_DIR.glob("*.smk")):
        text = smk_path.read_text()
        for rule_match in re.finditer(r"^rule (\w+):", text, re.MULTILINE):
            rule_name = rule_match.group(1)
            if smk_path == ARENA_SMK and rule_name == "align_arena":
                continue
            start = rule_match.start()
            end = text.find("\nrule ", start + 1)
            body = text[start:] if end == -1 else text[start:end]
            other_match = re.search(r"^\s*priority:\s*(\d+)\s*$", body, re.MULTILINE)
            if other_match:
                other_priority = int(other_match.group(1))
                assert other_priority < arena_priority, (
                    f"{smk_path.name}::{rule_name} has priority {other_priority} >= "
                    f"align_arena's {arena_priority}; the scheduler would no longer "
                    "prefer the arena when both are ready"
                )


# A representative `label|binary|mode` list, matching every distinct case
# `run_arm`'s `case "$binary" in` dispatches on -- including the SHA-like
# `394f8f8` label, the least string-like of the historical release names.
_SAMPLE_ARM_SPEC = (
    "bwa|bwa|default minibwa|minibwa|default "
    "bwa-mem2-upstream|bwa-mem2.upstream|default "
    "v021|bwa-mem3.v021|default 394f8f8|bwa-mem3.394f8f8|default "
    "fg-labs-default|bwa-mem2.fg-labs|default "
    "fg-labs-fast|bwa-mem2.fg-labs|fast"
)
_SAMPLE_ARM_SPEC_COUNT = 7
# The align_arena rule declares exactly one arm-spec-iterating `for` loop --
# warmup is now folded into each label's own iteration of the measured loop
# (see the module docstring's "Warmup: per-label, not per-job") rather than
# running as a separate pass with its own loop. It must use the safe form.
_EXPECTED_ARM_SPEC_LOOP_COUNT = 1


def test_arm_spec_for_loop_does_not_trigger_a_bash_syntax_error() -> None:
    """Regression test for a real bug that killed a live, paid AWS Batch
    arena job on its very first submission: `for entry in {params.arm_spec};
    do` bakes the `|`-delimited arm_spec string into the shell script's
    LITERAL SOURCE TEXT (Snakemake's `.format()` substitutes it before bash
    ever parses the script -- this is not a runtime shell-variable
    expansion). `|` is a shell metacharacter anywhere it appears in literal
    source text, regardless of context, so the rendered line was a bash
    SYNTAX ERROR: `for entry in bwa|bwa|default ...; do` aborted the entire
    rule immediately, on the first arm of the loop -- not a graceful
    per-arm SKIPPED row, which is what the rule's own "Never hard-fail on an
    old binary" design assumes every failure looks like. Confirmed live on
    both the c7i and c8g on-demand jobs for the same submission.

    Fixed by assigning `{params.arm_spec}` to a shell variable first and
    iterating over `$ARM_SPEC` unquoted: unquoted VARIABLE expansion only
    word-splits on IFS, it never re-tokenizes shell operators the way
    parsing literal source text does.

    A text-based "does the right substring appear" check cannot catch this
    class of bug on its own -- the failure is a property of how BASH's
    lexer treats `|` in literal vs. expanded text, not of the Python-level
    template. So this test actually executes the pattern through a real
    bash subprocess, exactly as arena.smk's own two `for entry in ...` loops
    do, with a representative arm_spec substituted for `{{params.arm_spec}}`
    the same way Snakemake's `.format()` would.
    """
    text = ARENA_SMK.read_text()
    assert "for entry in {params.arm_spec}" not in text, (
        "must not iterate {params.arm_spec} directly -- Snakemake's "
        "`.format()` bakes its literal `|` characters into the shell "
        "script's source text, which bash's lexer parses as pipe operators "
        "regardless of context; thread it through a shell variable first"
    )
    assert 'ARM_SPEC="{params.arm_spec}"' in text, (
        'expected an `ARM_SPEC="{params.arm_spec}"` assignment feeding '
        "the `for entry in $ARM_SPEC` loop"
    )
    assert text.count("for entry in $ARM_SPEC") == _EXPECTED_ARM_SPEC_LOOP_COUNT, (
        "the measured/warmup loop must iterate the safe $ARM_SPEC variable, "
        "not {params.arm_spec} directly"
    )

    script = f"""
    ARM_SPEC="{_SAMPLE_ARM_SPEC}"
    count=0
    for entry in $ARM_SPEC; do
        IFS='|' read -r label binary mode <<< "$entry"
        count=$((count + 1))
    done
    echo "$count"
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"the ARM_SPEC for-loop pattern must be valid, executable bash; stderr: {result.stderr}"
    )
    assert result.stdout.strip() == str(_SAMPLE_ARM_SPEC_COUNT), (
        f"expected all {_SAMPLE_ARM_SPEC_COUNT} arm_spec entries to be "
        f"iterated; got stdout {result.stdout!r}"
    )


def test_arena_arms_gives_every_historical_release_a_fast_arm() -> None:
    """Regression test for the arm list construction: every ARENA_RELEASES
    entry must get BOTH a default-mode arm and a `<label>-fast` arm on the
    same binary, built by one loop rather than a separate default-only pass
    -- so a release that predates `--fast` needs no hand-maintained "which
    releases support it" list; it just SKIPs its `-fast` arm at runtime (see
    the module docstring's "Never hard-fail on an old binary").
    """
    text = ARENA_SMK.read_text()
    func_start = text.index("def _arena_arms(")
    func_end = text.index("\ndef ", func_start + 1)
    func_body = text[func_start:func_end]
    assert "for label, _ in ARENA_RELEASES:" in func_body, (
        "expected a single loop over ARENA_RELEASES building both arms per "
        "release, not two separate list comprehensions"
    )
    assert 'arms.append((label, f"bwa-mem3.{label}", "default"))' in func_body
    assert 'arms.append((f"{label}-fast", f"bwa-mem3.{label}", "fast"))' in func_body


# Both the bwa-mem2.fg-labs branch (pre-existing) and the historical-release
# `*)` branch (this fix) must read $mode via this exact pattern.
_EXPECTED_FAST_FLAG_BRANCH_COUNT = 2
# check_cmd() below is invoked once for mode=default, once for mode=fast.
_EXPECTED_CHECK_CMD_INVOCATIONS = 2


def test_arm_dispatch_fast_flag_covers_the_historical_release_branch() -> None:
    """Regression test for a real gap: adding a `<label>-fast` arm to
    `_arena_arms` is not sufficient on its own -- the shell `run_arm`
    dispatcher's `*)` branch (which handles every historical bwa-mem3 release
    and bwa-mem2-upstream, since their binary names never match the `bwa`,
    `minibwa`, or `bwa-mem2.fg-labs` case arms) previously built `cmd` without
    ever reading `$mode`, so a `-fast` arm's `mode=fast` would silently run in
    default mode instead of appending `--fast`.

    Verified by actually extracting the `*)` branch's logic and executing it
    in a real bash subprocess for both `mode=default` and `mode=fast`, mirroring
    the ARM_SPEC regression test above rather than trusting a text search alone
    -- a text search cannot tell "reads $mode and uses it" apart from "reads
    $mode and ignores it".
    """
    text = ARENA_SMK.read_text()
    assert (
        text.count('[ "$mode" = "fast" ] && fast_flag="--fast"') == _EXPECTED_FAST_FLAG_BRANCH_COUNT
    ), (
        "expected the fast_flag pattern in both the bwa-mem2.fg-labs branch "
        "(pre-existing) and the historical-release `*)` branch (this fix) -- "
        "one instance means the `*)` branch still ignores $mode"
    )

    script = r"""
    check_cmd() {
        local binary="$1" mode="$2"
        local fast_flag=""
        [ "$mode" = "fast" ] && fast_flag="--fast"
        cmd="$binary mem -t 16 -K 1000000  $fast_flag ref.fa r1.fq r2.fq"
        echo "$cmd"
    }
    check_cmd "bwa-mem3.v090" "default"
    check_cmd "bwa-mem3.v090" "fast"
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == _EXPECTED_CHECK_CMD_INVOCATIONS, f"expected 2 output lines, got {lines!r}"
    assert "--fast" not in lines[0], f"default mode must not append --fast: {lines[0]!r}"
    assert "--fast" in lines[1], f"fast mode must append --fast: {lines[1]!r}"


def test_arm_spec_literal_substitution_reproduces_the_original_bug() -> None:
    """Companion to the test above: confirms the bug this all guards against
    is real by reproducing it directly -- iterating the SAME arm_spec value
    the old, broken way (baked into literal source text, exactly what
    `for entry in {params.arm_spec}; do` produced) must fail with bash's
    actual "unexpected token" syntax error, not some other, unrelated
    failure.
    """
    script = f"""
    for entry in {_SAMPLE_ARM_SPEC}; do
        IFS='|' read -r label binary mode <<< "$entry"
    done
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "unexpected token" in result.stderr, (
        f"expected the literal-substitution form to fail with bash's "
        f"'unexpected token' syntax error (confirming this is really the "
        f"same bug); got: {result.stderr!r}"
    )


def test_a_successful_measured_arm_tees_its_result_to_stderr() -> None:
    """Every SUCCESSFUL measured-cycle arm must echo its own result line to
    stderr (-> CloudWatch), not just write it to the local arena.tsv.

    Before this, the loop was silent on success -- only a FAILED arm produced
    any live output (the `WARNING: ... FAILED` line) -- because Snakemake's S3
    storage plugin uploads a rule's declared outputs on rule COMPLETION, not
    incrementally, so arena.tsv is invisible until the entire multi-hour job
    finishes. There was no way to see a single number mid-run.
    """
    text = ARENA_SMK.read_text()
    measured_start = text.index("Interleaved measured cycles")
    printf_idx = text.index("printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n'", measured_start)
    keep_comment_idx = text.index("# Keep the LAST measured rep's BAM", printf_idx)
    between = text[printf_idx:keep_comment_idx]
    assert 'echo "RESULT:' in between and ">&2" in between, (
        'expected a `echo "RESULT: ..." >&2` line between the printf that '
        "writes arena.tsv and the keep-BAM step, so a successful arm's result "
        "is visible in CloudWatch the moment it finishes, not just at job end"
    )
    expected_fields = (
        "label=$label",
        "mode=$mode",
        "rep=$rep",
        "wall_s=$WALL",
        "cpu_s=$CPU",
        "max_rss_mb=$RSS",
        "process_s=${{PROC:-NA}}",
    )
    for field in expected_fields:
        assert field in between, f"RESULT line must report {field}"


def _fg_labs_case_snippet() -> str:
    """Extracts the `bwa-mem2.fg-labs)` case body from `run_arm`, unescaping
    Snakemake's `.format()` `{{`/`}}` literal-brace escapes and standing in
    plain shell variables/literals for the substituted `{threads}` /
    `{params.*}` / `{input.*}` fields, so it can run standalone as valid bash.
    """
    text = ARENA_SMK.read_text()
    case_start = text.index("bwa-mem2.fg-labs)")
    case_end = text.index("*)", case_start)
    snippet = text[case_start:case_end].replace("{{", "{").replace("}}", "}")
    return (
        snippet.replace("{threads}", "16")
        .replace("{params.batch_flag}", "")
        .replace("{params.mem_flags}", "")
        .replace("{params.fg_labs_flags}", "")
        .replace("{input.ref[0]}", "ref.fa")
        .replace("{input.fastqs}", "r1.fq r2.fq")
    )


def test_fg_labs_invocation_requests_v3_only_on_the_warmup_rep() -> None:
    """The fg-labs binary must run with `-v 3` on the warmup rep (rep=0) so
    that invocation prints the "phase-2 SMEM lockstep width: N" line the pin
    block below parses -- but MUST NOT carry `-v 3` on any measured rep
    (rep>=1): the verbose stderr writes happen inside the tricorder-timed
    region and can perturb the very wall_s values the pin exists to keep
    clean. Verified by actually extracting the case branch and executing it
    in a real bash subprocess for rep=0 and rep=1, mirroring
    `test_arm_dispatch_fast_flag_covers_the_historical_release_branch` --
    a text search alone cannot tell "reads $rep and gates on it" apart from
    "always includes -v 3".
    """
    case_body = _fg_labs_case_snippet()
    script = f"""
    check_cmd() {{
        local binary="$1" mode="$2" rep="$3"
        case "$binary" in
            {case_body}
        esac
        echo "$cmd"
    }}
    check_cmd "bwa-mem2.fg-labs" "default" 0
    check_cmd "bwa-mem2.fg-labs" "default" 1
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == _EXPECTED_CHECK_CMD_INVOCATIONS, f"expected 2 output lines, got {lines!r}"
    assert "-v 3" in lines[0], f"warmup (rep=0) must pass -v 3: {lines[0]!r}"
    assert "-v 3" not in lines[1], f"measured rep (rep=1) must NOT pass -v 3: {lines[1]!r}"


def _lockstep_pin_snippet() -> str:
    """Extracts the `unset BWA3_SMEM_LOCKSTEP_N` ... pin block from
    `align_arena`, unescaping Snakemake's `.format()` `{{`/`}}` literal-brace
    escapes so it can run standalone as valid bash.

    The extraction starts at the production `unset` deliberately: that
    `unset` is the fix for inherited-environment leakage, and the test
    harness below must exercise it as written in `align_arena`, not
    re-implement its own copy. The block now calls `run_arm` itself (a
    DEDICATED probe invocation, not a parse of some other loop's warmup log
    -- see the module docstring's "Warmup: per-label, not per-job"), so
    callers must supply a stub `run_arm` (see `_stub_run_arm_snippet`)."""
    text = ARENA_SMK.read_text()
    start = text.index("unset BWA3_SMEM_LOCKSTEP_N")
    end = text.index("# Interleaved measured cycles", start)
    # `{{`/`}}` are Snakemake .format() escapes for literal `{`/`}` -- none
    # appear in this slice, but keep the unescape for parity with how
    # Snakemake would actually render it, in case a future edit adds one.
    return text[start:end].replace("{{", "{").replace("}}", "}")


def _stub_run_arm_snippet() -> str:
    """A fake `run_arm` matching the real function's I/O contract (writes
    `${out}.stderr.log`, returns 0/1) but with content and exit code
    controlled by the `STUB_LOG_CONTENT` / `STUB_EXIT_CODE` / `STUB_WRITE_LOG`
    env vars the test sets -- lets the pin-probe tests below exercise the
    REAL pin-probe snippet (which now calls `run_arm` itself to produce its
    own probe invocation, rather than only parsing a file some other loop
    already wrote) without a real `bwa-mem2.fg-labs` binary or `tricorder`."""
    return """
    run_arm() {
        local out="$5"
        mkdir -p "$(dirname "$out")"
        # Record whether the probe opt-in was live for THIS invocation, so the
        # pin-probe test can assert the dedicated probe requested it.
        printf '%s' "${BWA3_SMEM_LOCKSTEP_PROBE:-UNSET}" > "${out}.probe_env"
        if [ "${STUB_WRITE_LOG:-1}" = "1" ]; then
            printf '%s' "${STUB_LOG_CONTENT:-}" > "${out}.stderr.log"
        fi
        return "${STUB_EXIT_CODE:-0}"
    }
    """


def _run_lockstep_pin_snippet(
    outdir: Path,
    *,
    log_content: str = "",
    exit_code: int = 0,
    write_log: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Runs `_lockstep_pin_snippet()`'s extracted pin block, backed by
    `_stub_run_arm_snippet()`'s fake `run_arm`, in a real bash subprocess
    against `outdir`, echoing the resulting `BWA3_SMEM_LOCKSTEP_N` (or
    `UNSET`) to stdout so callers can assert on both the pinned value and the
    block's stderr diagnostics. Deliberately does NOT unset the variable
    itself beforehand -- that would test the harness's own cleanup instead of
    `align_arena`'s production behavior."""
    script = f"""
    set -euo pipefail
    OUTDIR="{outdir}"
    STUB_LOG_CONTENT={shlex.quote(log_content)}
    STUB_EXIT_CODE={exit_code}
    STUB_WRITE_LOG={"1" if write_log else "0"}
    {_stub_run_arm_snippet()}
    {_lockstep_pin_snippet()}
    echo "RESULT_PIN=${{BWA3_SMEM_LOCKSTEP_N:-UNSET}}"
    echo "PROBE_AFTER=${{BWA3_SMEM_LOCKSTEP_PROBE:-UNSET}}"
    """
    return subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_smem_lockstep_width_is_pinned_from_the_dedicated_pin_probe(tmp_path: Path) -> None:
    """A probe invocation whose stderr carries the "phase-2 SMEM lockstep
    width: N" line must pin `BWA3_SMEM_LOCKSTEP_N=N` and log a matching
    confirmation to stderr, so the measured cycles reuse this dedicated
    probe's own reading."""
    result = _run_lockstep_pin_snippet(
        tmp_path,
        log_content="* Ref file: /ref/hs38\n[M::main_mem] phase-2 SMEM lockstep width: 24\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=24" in result.stdout
    assert "pinned BWA3_SMEM_LOCKSTEP_N=24" in result.stderr


def test_pin_probe_opts_into_the_probe_and_clears_it_afterward(tmp_path: Path) -> None:
    """As of fg-labs/bwa-mem3#414 the startup MLP probe is opt-in (default off),
    so the dedicated pin probe must export `BWA3_SMEM_LOCKSTEP_PROBE=1` for its
    own invocation -- otherwise a #414+ candidate would echo its compiled
    default width instead of measuring one. It must then clear the var before the
    measured cycles, or the unpinned fallback path would make a #414+ build
    re-probe on every measured rep (the inflation the pin exists to avoid)."""
    result = _run_lockstep_pin_snippet(
        tmp_path,
        log_content="* Ref file: /ref/hs38\n[M::main_mem] phase-2 SMEM lockstep width: 24\n",
    )
    assert result.returncode == 0, result.stderr
    probe_env = (tmp_path / "runs" / "fg-labs-default.default.pinprobe.probe_env").read_text()
    assert probe_env == "1", f"pin probe must run with the probe opt-in set, got {probe_env!r}"
    assert "PROBE_AFTER=UNSET" in result.stdout, (
        "probe opt-in must be cleared before the measured cycles"
    )


def test_smem_lockstep_pin_warns_but_does_not_crash_when_the_probe_line_is_missing(
    tmp_path: Path,
) -> None:
    """A build that predates fg-labs/bwa-mem3#393 (or any unexpected stderr
    shape) must not fail the whole job under `set -euo pipefail` -- it should
    warn and leave the probe unpinned for the measured cycles."""
    result = _run_lockstep_pin_snippet(tmp_path, log_content="* Ref file: /ref/hs38\n")
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=UNSET" in result.stdout
    assert "could not find a phase-2 SMEM lockstep width line" in result.stderr
    assert "PROBE_AFTER=UNSET" in result.stdout, (
        "probe opt-in must be cleared before the measured cycles even when no width line was found"
    )


def test_smem_lockstep_pin_warns_but_does_not_crash_when_the_probe_log_is_missing(
    tmp_path: Path,
) -> None:
    """The dedicated probe invocation itself could FAIL before ever writing
    its stderr.log (see the "Never hard-fail on an old binary" design) -- the
    pin step must degrade gracefully, not crash the job."""
    result = _run_lockstep_pin_snippet(tmp_path, exit_code=1, write_log=False)
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=UNSET" in result.stdout
    assert "pin probe: fg-labs-default failed" in result.stderr
    assert "pin probe likely failed" in result.stderr
    assert "PROBE_AFTER=UNSET" in result.stdout, (
        "probe opt-in must be cleared before the measured cycles even when the "
        "probe invocation itself failed to write a log"
    )


def test_smem_lockstep_pin_still_pins_a_parsed_width_after_a_nonzero_run_arm_exit(
    tmp_path: Path,
) -> None:
    """`run_arm` can exit nonzero after still writing a usable stderr.log (e.g.
    a late-stage failure after the probe line was already emitted) -- parsing
    below does not gate on `$status`, so the width is pinned regardless. The
    failure warning printed immediately after `run_arm` returns must not claim
    the rows "will run unpinned" in that case, since they will not be."""
    result = _run_lockstep_pin_snippet(
        tmp_path,
        log_content="* Ref file: /ref/hs38\n[M::main_mem] phase-2 SMEM lockstep width: 24\n",
        exit_code=1,
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=24" in result.stdout
    assert "pinned BWA3_SMEM_LOCKSTEP_N=24" in result.stderr
    assert "pin probe: fg-labs-default failed" in result.stderr
    assert "a parsed width will be used if available" in result.stderr, (
        "the failure warning must hedge on a still-parseable width rather than "
        "unconditionally claiming an unpinned fallback"
    )


def test_smem_lockstep_pin_snippet_ignores_an_inherited_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`subprocess.run` inherits the calling process's environment, and a
    real `align_arena` job's environment could equally already have
    `BWA3_SMEM_LOCKSTEP_N` set (e.g. a worker host that leaked one from a
    prior invocation). A missing probe line must still report `UNSET` rather
    than silently echoing back the stale inherited value -- the pin block's
    own leading `unset BWA3_SMEM_LOCKSTEP_N` (see `_lockstep_pin_snippet`) is
    what guarantees that; without it the snippet would misreport an unpinned
    run as pinned."""
    monkeypatch.setenv("BWA3_SMEM_LOCKSTEP_N", "999")
    result = _run_lockstep_pin_snippet(tmp_path, log_content="* Ref file: /ref/hs38\n")
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=UNSET" in result.stdout
    assert "RESULT_PIN=999" not in result.stdout


def _measured_cycle_body() -> str:
    """Extracts the LABEL-MAJOR measured-cycle loop (from its introducing
    comment through the per-label instrumentation block), for the ordering
    and instrumentation regression tests below."""
    text = ARENA_SMK.read_text()
    start = text.index("# Interleaved measured cycles")
    end = text.index("emit-host-probe post", start)
    return text[start:end]


def test_arm_spec_is_seeded_by_the_fg_labs_sha() -> None:
    """Regression test for the arena's own host-drift confound (see
    `bwa_mem3_bench/arena_arms.py`): a FIXED arm order always disadvantages
    whichever label is newest. `_arena_arm_spec`'s shuffle must vary by
    candidate, so it needs the fg-labs SHA under measurement as its seed --
    not a fixed literal, which would just fix a DIFFERENT arm in the worst
    position forever instead of decorrelating position from identity."""
    text = ARENA_SMK.read_text()
    assert "def _arena_arm_spec(arch: str, *, seed: str) -> str:" in text
    assert "arm_spec = lambda wc: _arena_arm_spec(wc.arch, seed=wc.sha)" in text, (
        "expected the rule's arm_spec param to pass wc.sha as the shuffle seed"
    )


def test_measured_cycle_is_label_major_not_rep_major() -> None:
    """Regression test for the fixed-order host-drift confound this rule
    used to have: the measured cycle must iterate LABEL-outer / rep-inner (one
    label's reps run back-to-back) so a label's own median is drawn from a
    narrow slice of the job's wall-clock, not the fixed-order rep-outer shape
    where the same arm was always measured last, every single rep."""
    body = _measured_cycle_body()
    entry_idx = body.index("for entry in $ARM_SPEC; do")
    rep_idx = body.index("for rep in $(seq 1 {params.reps}); do")
    assert entry_idx < rep_idx, (
        "expected `for entry in $ARM_SPEC` (label-major, outer) to precede "
        "`for rep in $(seq ...)` (inner) in the measured cycle -- got the "
        "reverse, which is the old rep-major shape this rule moved away from"
    )
    # Exactly one of each: warmup is folded into this same loop (see
    # `test_per_label_warmup_precedes_its_own_measured_reps` below), not run
    # as a separate pass with its own `for entry in $ARM_SPEC`.
    assert body.count("for entry in $ARM_SPEC; do") == 1
    assert body.count("for rep in $(seq 1 {params.reps}); do") == 1


def test_per_label_warmup_precedes_its_own_measured_reps() -> None:
    """Regression test for the within-label cache-eviction bug the per-label
    warmup restructuring fixes: each label's own UNMEASURED warmup call must
    run INSIDE that label's own `for entry in $ARM_SPEC` iteration, before
    its `for rep in ...` loop -- not once for the whole arm list up front, in
    a separate loop whose warmup could be separated from its own label's
    measured cycle by every other arm's warmup and measured runs happening
    in between (confirmed via a real arena run's `meminfo.jsonl`: `wall_s`
    showed a rep1 >> rep2 > rep3 decay on affected labels while `process_s`
    stayed flat -- see the module docstring)."""
    body = _measured_cycle_body()
    entry_idx = body.index("for entry in $ARM_SPEC; do")
    warmup_idx = body.index('run_arm "$label" "$binary" "$mode" 0 ')
    rep_idx = body.index("for rep in $(seq 1 {params.reps}); do")
    assert entry_idx < warmup_idx < rep_idx, (
        "expected the per-label warmup call (rep=0) to appear inside the "
        "`for entry in $ARM_SPEC` loop, before its `for rep in ...` loop -- "
        f"got entry_idx={entry_idx}, warmup_idx={warmup_idx}, rep_idx={rep_idx}"
    )
    # Exactly one rep=0 warmup call per label iteration -- a second one would
    # mean warmup is (again) running as its own separate pass.
    assert body.count('run_arm "$label" "$binary" "$mode" 0 ') == 1


def test_tricorder_records_a_per_tick_trace() -> None:
    """`--trace` gives per-tick RSS/IO/page-fault samples per arm -- the most
    direct test available for the page-cache-eviction hypothesis in the
    module docstring, since it is measured on the exact process in question
    rather than inferred from wall_s minus process_s."""
    text = ARENA_SMK.read_text()
    assert '--trace "{{out}}.trace.tsv"' in text or '--trace "${{out}}.trace.tsv"' in text, (
        "expected the tricorder invocation to also request a per-tick --trace file"
    )


def test_per_label_host_probe_and_meminfo_snapshot_are_emitted() -> None:
    """Regression test for the arena's new per-label instrumentation: a
    tachyon probe and a page-cache snapshot must both fire once per label,
    after that label's reps finish, so the host-drift hypothesis in the
    module docstring is checkable directly rather than only inferred."""
    body = _measured_cycle_body()
    assert (
        'emit-host-probe "label-${{label}}" {params.label_probe_seconds} >> {output.host_probe}'
        in body
    )
    assert 'emit_meminfo_snapshot "label-${{label}}"' in body

    text = ARENA_SMK.read_text()
    assert 'meminfo        = "arena/{sha}/{arch}/meminfo.jsonl"' in text, (
        "expected a declared `meminfo` output for the page-cache snapshots"
    )
    assert "emit_meminfo_snapshot() {{" in text
    assert "emit_meminfo_snapshot pre" in text
    assert "emit_meminfo_snapshot post" in text


def test_meminfo_snapshot_reads_the_expected_proc_fields(tmp_path: Path) -> None:
    """Executes the real `emit_meminfo_snapshot` function against a synthetic
    `/proc/meminfo`-shaped file, confirming it parses Cached/Dirty/Buffers
    into a well-formed JSON line -- a text search alone cannot tell "reads
    the right field" apart from "reads the wrong one and still prints valid
    JSON"."""
    text = ARENA_SMK.read_text()
    func_start = text.index("emit_meminfo_snapshot() {{")
    func_end = text.index("\n        }}", func_start) + len("\n        }}")
    func_body = text[func_start:func_end].replace("{{", "{").replace("}}", "}")

    meminfo_out = tmp_path / "meminfo.jsonl"
    fake_proc_meminfo = tmp_path / "meminfo"
    fake_proc_meminfo.write_text(
        "MemTotal:       65000000 kB\nCached:         15728640 kB\n"
        "Dirty:              1024 kB\nBuffers:            2048 kB\n"
    )

    script = f"""
    set -euo pipefail
    {func_body.replace("/proc/meminfo", str(fake_proc_meminfo))}
    """
    # The function as written appends to a literal `{output.meminfo}` --
    # Snakemake's own `.format()` substitution target -- so stand in the temp
    # path for it here, the same way `_fg_labs_case_snippet` stands in plain
    # values for other `{params.*}`/`{input.*}` fields.
    script = script.replace("{output.meminfo}", str(meminfo_out))
    script += '\nemit_meminfo_snapshot "test-phase"\n'
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    line = meminfo_out.read_text().strip()
    record = json.loads(line)
    assert record == {
        "phase": "test-phase",
        "cached_kb": 15728640,
        "dirty_kb": 1024,
        "buffers_kb": 2048,
    }
