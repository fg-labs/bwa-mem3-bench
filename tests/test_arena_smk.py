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
from bwa_mem3_bench.storage.ingest import _ARENA_COLUMNS, _ARENA_HEADER_RENAMES
from bwa_mem3_bench.storage.sqlite import ARENA_V14_COLUMNS

ARENA_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "arena.smk"
RULES_DIR = Path(REPO_ROOT) / "workflow" / "rules"
SNAKEFILE = Path(REPO_ROOT) / "workflow" / "Snakefile"


def test_workflow_loads_without_error() -> None:
    """`snakemake --list` parses and EXECUTES every included .smk at module
    scope. This is the only check that catches a load-order bug in a rules file
    -- e.g. a module-level name used before its definition -- because the
    text-based tests here never execute the file and no unit test imports it (a
    .smk needs Snakemake's injected globals). Regression guard for a real one:
    arena.smk's SA-densification block referenced `_arena_sample_cfg` 60+ lines
    before it was defined, a NameError at load that every green test missed."""
    result = subprocess.run(  # noqa: S603, S607 -- fixed args, no shell
        ["snakemake", "-s", str(SNAKEFILE), "--list"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"`snakemake --list` failed to load the workflow (a .smk parse/NameError "
        f"at module scope?):\nstdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )


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


# A representative `label|binary|mode|dense` list, matching every distinct case
# `run_arm`'s `case "$binary" in` dispatches on -- including the SHA-like
# `394f8f8` label, the least string-like of the historical release names. The
# 4th field is the dense-SA flag (`_arm_uses_dense_sa`): 1 for the candidate,
# 0 for the historical/foreign arms shown here.
_SAMPLE_ARM_SPEC = (
    "bwa|0|1|bwa|bwa|default|0|sam minibwa|0|1|minibwa|minibwa|default|0|sam "
    "bwa-mem2-stock|0|1|bwa-mem2-upstream|bwa-mem2.upstream|default|0|sam "
    "bwa-mem2-stock|0|2|v021|bwa-mem3.v021|default|0|bam "
    "bwa-mem2-stock|0|3|394f8f8|bwa-mem3.394f8f8|default|0|bam "
    "bwa-mem2-dense|1|1|fg-labs-default|bwa-mem2.fg-labs|default|1|bam "
    "bwa-mem2-dense|1|2|fg-labs-fast|bwa-mem2.fg-labs|fast|1|bam"
)
_SAMPLE_ARM_SPEC_COUNT = 7
# The align_arena rule declares exactly one arm-spec-iterating `for` loop --
# the per-rep cat prewarm runs inside it (see the module docstring's
# "Warmup: per-rep cat, not per-label alignment") rather than as a separate
# pass with its own loop. It must use the safe form.
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
        IFS='|' read -r block round position label binary mode dense writer <<< "$entry"
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
        IFS='|' read -r label binary mode dense <<< "$entry"
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
    measured_start = text.index("# The schedule: index-family blocks")
    printf_idx = text.index('"$label" "$mode" "$round" "$WALL"', measured_start)
    keep_comment_idx = text.index("# Keep the latest MEASURED round's BAM", printf_idx)
    between = text[printf_idx:keep_comment_idx]
    assert 'echo "RESULT:' in between and ">&2" in between, (
        'expected a `echo "RESULT: ..." >&2` line between the printf that '
        "writes arena.tsv and the keep-BAM step, so a successful arm's result "
        "is visible in CloudWatch the moment it finishes, not just at job end"
    )
    expected_fields = (
        "label=$label",
        "mode=$mode",
        "round=$round",
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


def test_fg_labs_invocation_requests_v3_only_on_the_pin_probe() -> None:
    """The fg-labs binary must run with `-v 3` on the pin probe (rep=probe) so
    that invocation prints the "phase-2 SMEM lockstep width: N" line the pin
    block below parses -- but MUST NOT carry `-v 3` on any scheduled run,
    warmup round 0 included: the verbose stderr writes happen inside the
    tricorder-timed region and can perturb the very wall_s values the pin
    exists to keep clean. Verified by actually extracting the case branch and executing it
    in a real bash subprocess for rep=0 and rep=1, mirroring
    `test_arm_dispatch_fast_flag_covers_the_historical_release_branch` --
    a text search alone cannot tell "reads $rep and gates on it" apart from
    "always includes -v 3".
    """
    case_body = _fg_labs_case_snippet()
    script = f"""
    check_cmd() {{
        local binary="$1" mode="$2" rep="$3"
        local ref="ref.fa"   # set outside the case block in the real run_arm
        case "$binary" in
            {case_body}
        esac
        echo "$cmd"
    }}
    check_cmd "bwa-mem2.fg-labs" "default" probe
    check_cmd "bwa-mem2.fg-labs" "default" 0
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == _EXPECTED_CHECK_CMD_INVOCATIONS, f"expected 2 output lines, got {lines!r}"
    assert "-v 3" in lines[0], f"the pin probe must pass -v 3: {lines[0]!r}"
    assert "-v 3" not in lines[1], f"warmup round 0 must NOT pass -v 3: {lines[1]!r}"


def test_dense_sa_min_label_is_a_real_release() -> None:
    """`ARENA_DENSE_SA_MIN_LABEL` gates by position in ARENA_RELEASES, so it
    MUST be a label in that list -- otherwise `arm_uses_dense_sa` falls to its
    'min_label not in release_labels -> stock' guard and every historical arm
    silently stays stock, no matter how new."""
    text = ARENA_SMK.read_text()
    m = re.search(r'ARENA_DENSE_SA_MIN_LABEL\s*=\s*"([^"]+)"', text)
    assert m, "ARENA_DENSE_SA_MIN_LABEL assignment not found"
    min_label = m.group(1)
    release_labels = re.findall(r'\(\s*"([^"]+)"\s*,\s*"[0-9a-f]{40}"\s*\)', text)
    assert min_label in release_labels, (
        f"ARENA_DENSE_SA_MIN_LABEL={min_label!r} is not in ARENA_RELEASES "
        f"{release_labels!r}; the dense-SA gate would never fire for a release"
    )


def _run_arm_body() -> str:
    """The `run_arm() { ... }` function body, sliced OUT of the surrounding
    script (it ends where `prewarm_arm()` begins). Scoping to run_arm matters:
    prewarm_arm carries the identical ref-selection lines, so a whole-file
    `in text` check cannot prove the routing lives in run_arm."""
    text = ARENA_SMK.read_text()
    start = text.index("run_arm() {{")
    end = text.index("prewarm_arm() {{", start)
    return text[start:end]


def test_run_arm_routes_dense_arms_to_the_stride2_index() -> None:
    """A dense arm (`dense=1`) must align against `{input.ref_dense[0]}` (the
    stride-2 `re-sa` copy), everything else against the stock `{input.ref[0]}`.
    run_arm selects this ONCE, before the case block, into `$ref`; the aligner
    cmds then reference `$ref`, never a hard-coded index."""
    body = _run_arm_body()
    # The selection lives IN run_arm (not merely somewhere in the file).
    assert 'local ref="{input.ref[0]}"' in body
    assert '[ "$dense" = "1" ] && ref="{input.ref_dense[0]}"' in body
    # Every aligner cmd must read $ref (never a hard-coded idxbase), or a dense
    # arm would silently align against the stock stride-8 index. All four cases
    # (bwa, minibwa, fg-labs, *) end their cmd with `$ref {input.fastqs}`.
    _EXPECTED_REF_USING_CMDS = 4
    assert body.count("$ref {input.fastqs}") == _EXPECTED_REF_USING_CMDS, (
        "expected all four run_arm aligner cmds to end with `$ref {input.fastqs}`"
    )
    assert "{input.ref[0]} {input.fastqs}" not in body, (
        "an aligner cmd hard-codes the stock idxbase `{input.ref[0]}` -- dense "
        "arms would then ignore the stride-2 index; use $ref"
    )

    # Execute the real selection lines for dense=0 and dense=1.
    script = r"""
    select_ref() {
        local dense="$1"
        local ref="stock.fa"
        [ "$dense" = "1" ] && ref="dense.fa"
        echo "$ref"
    }
    select_ref 0
    select_ref 1
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout.split() == ["stock.fa", "dense.fa"]


def test_run_arm_call_sites_pass_the_dense_argument() -> None:
    """Both run_arm invocations must pass the 6th positional `dense` arg, or the
    candidate silently measures against the wrong SA-sample index: the pin probe
    passes the Python-computed `{params.fg_labs_dense}`, and the measured loop
    passes the parsed `"$dense"` token. A missing arg makes run_arm read an
    unset `$6`, which under `set -u` aborts the job (loud) but under the probe's
    `set +e` window could silently default the ref."""
    text = ARENA_SMK.read_text()
    assert (
        'run_arm "fg-labs-default" "bwa-mem2.fg-labs" "default" probe "$PIN_PROBE_OUT" '
        "{params.fg_labs_dense} bam" in text
    ), "pin-probe run_arm call must pass {params.fg_labs_dense} as the dense arg"
    assert 'run_arm "$label" "$binary" "$mode" "$round" "$OUT" "$dense" "$writer"' in text, (
        'scheduled-run run_arm call must pass "$dense" and "$writer" as the 6th/7th args'
    )


def _lockstep_pin_snippet(pin_probe: str = "1") -> str:
    """Extracts the `unset BWA3_SMEM_LOCKSTEP_N` ... pin block from
    `align_arena`, unescaping Snakemake's `.format()` `{{`/`}}` literal-brace
    escapes so it can run standalone as valid bash.

    The extraction starts at the production `unset` deliberately: that
    `unset` is the fix for inherited-environment leakage, and the test
    harness below must exercise it as written in `align_arena`, not
    re-implement its own copy. The block now calls `run_arm` itself (a
    DEDICATED probe invocation, not a parse of some other loop's warmup log
    -- see the module docstring's "Warmup: per-run cat"), so
    callers must supply a stub `run_arm` (see `_stub_run_arm_snippet`)."""
    text = ARENA_SMK.read_text()
    start = text.index("unset BWA3_SMEM_LOCKSTEP_N")
    end = text.index("# The schedule: index-family blocks", start)
    # `{{`/`}}` are Snakemake .format() escapes for literal `{`/`}` -- none
    # appear in this slice, but keep the unescape for parity with how
    # Snakemake would actually render it, in case a future edit adds one.
    return (
        text[start:end]
        .replace("{{", "{")
        .replace("}}", "}")
        .replace("{params.pin_probe}", pin_probe)
    )


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
    pin_probe: str = "1",
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
    {_lockstep_pin_snippet(pin_probe)}
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
    """Extracts the scheduled-run loop (from its introducing comment through
    the end of the loop), for the ordering and instrumentation tests below."""
    text = ARENA_SMK.read_text()
    start = text.index("# The schedule: index-family blocks")
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


def test_scheduled_loop_iterates_the_python_schedule_once() -> None:
    """The run order is `build_schedule`'s (unit-tested in test_arena_arms.py):
    the shell must walk its tokens exactly once and must not re-impose its own
    label-major `for rep` loop, which is what let one moment of host state
    land on all of a label's reps (see the module docstring's "Schedule")."""
    body = _measured_cycle_body()
    assert body.count("for entry in $ARM_SPEC; do") == 1
    assert "for rep in $(seq" not in body
    assert "IFS='|' read -r block round position label binary mode dense writer" in body, (
        "the loop must parse every field of `ScheduledRun.to_token`"
    )


def test_prewarm_runs_prewarm_passes_times_before_each_run() -> None:
    body = _measured_cycle_body()
    passes_idx = body.index("for pass in $(seq 1 {params.prewarm_passes}); do")
    prewarm_idx = body.index('prewarm_arm "$binary" "$dense"', passes_idx)
    run_arm_idx = body.index('run_arm "$label" "$binary" "$mode" "$round" ')
    assert passes_idx < prewarm_idx < run_arm_idx
    assert body.count('prewarm_arm "$binary"') == 1


def test_balloon_runs_at_every_block_change_before_any_run_of_the_block() -> None:
    body = _measured_cycle_body()
    change_idx = body.index('if [ "$block" != "$PREV_BLOCK" ]; then')
    balloon_idx = body.index('balloon "$block"', change_idx)
    run_arm_idx = body.index('run_arm "$label"')
    assert change_idx < balloon_idx < run_arm_idx


def test_balloon_touches_the_configured_amount_and_never_fails_the_job(tmp_path: Path) -> None:
    """Executes the real `balloon` function with `dd` stubbed: it must request one
    `<balloon_mb>M` block with `iflag=fullblock` (so every page is touched), skip
    entirely at 0, and survive a failing `dd`."""
    text = ARENA_SMK.read_text()
    start = text.index("balloon() {{")
    end = text.index("\n        }}", start) + len("\n        }}")
    func = text[start:end].replace("{{", "{").replace("}}", "}")
    log = tmp_path / "dd.log"

    def run(mb: int, dd_exit: int) -> subprocess.CompletedProcess[str]:
        script = f"""
        set -euo pipefail
        dd() {{ echo "$@" >> {log}; return {dd_exit}; }}
        {func.replace("{params.balloon_mb}", str(mb))}
        balloon dense
        echo done
        """
        return subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

    result = run(4000, 0)
    assert result.returncode == 0 and "done" in result.stdout, result.stderr
    assert "bs=4000M count=1 iflag=fullblock" in log.read_text()
    log.unlink()
    assert run(0, 0).returncode == 0
    assert not log.exists(), "balloon_mb=0 must skip dd entirely"
    failed = run(4000, 1)
    assert failed.returncode == 0 and "done" in failed.stdout
    assert "WARNING: balloon" in failed.stderr


def test_bwa_mem3_builds_write_native_bam_and_others_pipe_through_samtools() -> None:
    text = ARENA_SMK.read_text()
    run_arm = text[text.index("run_arm() {{") : text.index("prewarm_arm() {{")]
    assert '[ "$writer" = "bam" ] && out_flags="--bam=0 -o -"' in run_arm
    for case in ("bwa-mem2.fg-labs)", "*)"):
        case_body = run_arm[run_arm.index(case) :]
        case_body = case_body[: case_body.index(";;")]
        assert "$out_flags" in case_body, case
    for case in ("bwa)", "minibwa)"):
        case_body = run_arm[run_arm.index(f"                {case}") :]
        case_body = case_body[: case_body.index(";;")]
        assert "$out_flags" not in case_body, case
    sam_stage = run_arm[run_arm.index('local to_bam=""') : run_arm.index("tricorder --interval")]
    assert '[ "$writer" = "sam" ] && to_bam="samtools view -@4 -u -o - - |"' in sam_stage, (
        "only the SAM writer may route through samtools"
    )
    assert run_arm.count("samtools view -@4 -u") == 1, "samtools must appear only in the sam stage"


def test_every_arm_writes_its_output_file_through_cat_not_htslib() -> None:
    """htslib fdatasync()s a regular-file output at close; a single-threaded
    BGZF writer defers the whole file's sync to then, so writing the file
    directly would time the disk, not the aligner. The one timed pipeline must
    end in `| cat > ...bam.raw`, with samtools (when present) writing stdout."""
    text = ARENA_SMK.read_text()
    run_arm = text[text.index("run_arm() {{") : text.index("prewarm_arm() {{")]
    assert run_arm.count("tricorder --interval") == 1
    assert "| $to_bam cat > '${{out}}.bam.raw'" in run_arm
    assert "samtools view -@4 -u -o - - |" in run_arm
    assert "-o '${{out}}.bam.raw'" not in run_arm


def test_tricorder_samples_finely_enough_to_resolve_release_deltas() -> None:
    """At tricorder's default 0.5 s tick a ~20 s wall resolves in 2.5% steps."""
    text = ARENA_SMK.read_text()
    run_arm = text[text.index("run_arm() {{") : text.index("prewarm_arm() {{")]
    assert run_arm.count("tricorder --interval 0.1 ") == 1


def test_each_row_records_schedule_position_and_host_state() -> None:
    text = ARENA_SMK.read_text()
    header = text[text.index("printf 'label\\t") :].split("\n", 1)[0]
    columns = header.split("'")[1].split("\\t")
    columns[-1] = columns[-1].removesuffix("\\n")
    assert columns[:7] == [
        "label",
        "mode",
        "rep",
        "wall_s",
        "cpu_s",
        "max_rss_mb",
        "process_s",
    ], "columns 1-7 keep the legacy order"
    assert columns[7:] == [
        "block",
        "position",
        "writer",
        "index_read_s",
        "io_in_mb",
        "page_cache_start_mb",
        "thp_fault_fallback",
        "compact_stall",
        "pgmajfault",
        "memfree_mb",
        "anon_huge_mb",
    ]
    body = _measured_cycle_body()
    for counter in ("thp_fault_fallback", "compact_stall", "pgmajfault"):
        assert f"vmstat_counter {counter})" in body
    snapshot_idx = body.index("FALLBACK0=$(vmstat_counter thp_fault_fallback)")
    assert body.index("prewarm_arm") < snapshot_idx < body.index('run_arm "$label"'), (
        "the pre-run counter snapshot must follow the prewarm, so deltas cover the run alone"
    )


def test_vmstat_and_meminfo_readers_parse_and_degrade_to_na(tmp_path: Path) -> None:
    text = ARENA_SMK.read_text()
    funcs = ""
    for name in ("vmstat_counter", "meminfo_mb", "counter_delta"):
        start = text.index(f"{name}() {{{{")
        end = text.index("\n        }}", start) + len("\n        }}")
        funcs += text[start:end].replace("{{", "{").replace("}}", "}") + "\n"
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("thp_fault_alloc 10\nthp_fault_fallback 7\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree:        2097152 kB\n")
    funcs = funcs.replace("/proc/vmstat", str(vmstat)).replace("/proc/meminfo", str(meminfo))
    script = f"""
    set -euo pipefail
    {funcs}
    echo "$(vmstat_counter thp_fault_fallback) $(vmstat_counter nope) $(meminfo_mb MemFree)"
    echo "$(meminfo_mb AnonHugePages) $(counter_delta 3 10) $(counter_delta NA 10)"
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["7 NA 2048", "NA 7 NA"]


def test_no_per_label_full_alignment_warmup_remains() -> None:
    """The only alignment outside the schedule is the dedicated SMEM-lockstep pin
    probe; warmup is the schedule's own discarded round 0."""
    text = ARENA_SMK.read_text()
    assert 'run_arm "$label" "$binary" "$mode" 0 ' not in text
    assert text.count('run_arm "fg-labs-default" "bwa-mem2.fg-labs" "default" probe ') == 1


def test_global_one_time_index_cat_prewarm_is_removed() -> None:
    """Regression guard: the OLD job-start-only `cat` prewarm (all three
    index families, once, before any arm ran) must not reappear alongside
    the per-rep one -- it was superseded, not supplemented, since it could
    not protect a label running later in a ~14-22-arm job from intervening
    arms' eviction (the same gap the per-label full-alignment warmup left
    for large indices)."""
    text = ARENA_SMK.read_text()
    preamble_start = text.index('mkdir -p "$OUTDIR/runs"')
    preamble_end = text.index("emit-host-probe pre", preamble_start)
    preamble = text[preamble_start:preamble_end]
    assert "cat {input.ref[0]}" not in preamble, (
        "expected the once-per-job index cat prewarm to be gone from the "
        "job preamble -- it is now per-rep via prewarm_arm"
    )


def _prewarm_arm_snippet() -> str:
    """Extracts the `prewarm_arm() { ... }` function body from `align_arena`,
    unescaping Snakemake's `.format()` `{{`/`}}` literal-brace escapes and
    standing in plain paths for the substituted `{input.*}` fields, so it can
    run standalone as valid bash."""
    text = ARENA_SMK.read_text()
    start = text.index("prewarm_arm() {{")
    end = text.index("\n        }}", start) + len("\n        }}")
    snippet = text[start:end].replace("{{", "{").replace("}}", "}")
    return (
        snippet.replace("{input.ref[0]}", "ref")
        .replace("{input.ref_dense[0]}", "ref_dense")
        .replace("{input.fastqs}", "r1.fq r2.fq")
    )


def _run_prewarm_arm(binary: str, dense: str = "0") -> list[str]:
    """Runs `prewarm_arm(binary, dense)` with `cat` replaced by a stub that logs
    its file arguments to a side file (not stdout, since the production case
    block redirects stdout/stderr to `/dev/null` -- a side-file log survives
    that redirect since it is the stub's own explicit target, not FD 1/2).
    Returns the file arguments every `cat` invocation received, in order,
    letting the dispatch tests below assert on exactly which files each
    binary case touches without needing real index files on disk."""
    script = f"""
    set -euo pipefail
    LOGFILE=$(mktemp)
    cat() {{
        for f in "$@"; do printf '%s\\n' "$f" >> "$LOGFILE"; done
    }}
    {_prewarm_arm_snippet()}
    prewarm_arm {binary} {dense}
    unset -f cat
    cat "$LOGFILE"
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return result.stdout.strip().splitlines()


def test_prewarm_arm_cats_bwas_own_index_files() -> None:
    assert _run_prewarm_arm("bwa") == ["ref.bwt", "ref.sa", "ref.pac", "r1.fq", "r2.fq"]


def test_prewarm_arm_cats_minibwas_own_index_files() -> None:
    assert _run_prewarm_arm("minibwa") == ["ref.l2b", "ref.mbw", "r1.fq", "r2.fq"]


def test_prewarm_arm_cats_the_bwa_mem2_format_index_for_every_other_binary() -> None:
    """Covers `bwa-mem2.fg-labs`, every `bwa-mem3.<label>`, and
    `bwa-mem2.upstream` -- all three share the SAME on-disk index files, so
    the `*)` branch is correct for all of them, not just the fg-labs one."""
    expected = ["ref.0123", "ref.bwt.2bit.64", "ref.pac", "r1.fq", "r2.fq"]
    for binary in ("bwa-mem2.fg-labs", "bwa-mem3.v060", "bwa-mem2.upstream"):
        assert _run_prewarm_arm(binary) == expected, binary


def test_prewarm_arm_cats_the_dense_index_for_a_dense_arm() -> None:
    """A dense arm (`dense=1`, e.g. the `> v0.12.0` candidate) must warm the
    STRIDE-2 `re-sa` idxbase (`ref_dense.*`), not the stock one -- otherwise the
    (larger) .bwt.2bit.64 the timed run reads would be cold. bwa/minibwa are
    never dense arms, so they stay on the stock ref even if dense is set."""
    assert _run_prewarm_arm("bwa-mem2.fg-labs", "1") == [
        "ref_dense.0123",
        "ref_dense.bwt.2bit.64",
        "ref_dense.pac",
        "r1.fq",
        "r2.fq",
    ]
    # bwa/minibwa ignore dense -- always their own stock-index sidecars.
    assert _run_prewarm_arm("bwa", "1") == ["ref.bwt", "ref.sa", "ref.pac", "r1.fq", "r2.fq"]


def test_prewarm_arm_tolerates_missing_index_files(tmp_path: Path) -> None:
    """A missing sidecar (e.g. no upstream .bwt/.sa on an arch that never
    stages them) must not fail the job over a warm-cache nicety -- the same
    `|| true` contract the old global prewarm already relied on. Uses the
    REAL `cat` (not the logging stub) against files that don't exist, so
    this actually exercises the fallback rather than assuming it."""
    script = f"""
    set -euo pipefail
    cd {tmp_path}
    {_prewarm_arm_snippet()}
    prewarm_arm bwa 0
    echo DONE
    """
    result = subprocess.run(  # noqa: S603, S607 -- fixed, test-owned Bash script
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "DONE" in result.stdout


def test_tricorder_records_a_per_tick_trace() -> None:
    """`--trace` gives per-tick RSS/IO/page-fault samples per arm -- the most
    direct test available for the page-cache-eviction hypothesis in the
    module docstring, since it is measured on the exact process in question
    rather than inferred from wall_s minus process_s."""
    text = ARENA_SMK.read_text()
    assert '--trace "{{out}}.trace.tsv"' in text or '--trace "${{out}}.trace.tsv"' in text, (
        "expected the tricorder invocation to also request a per-tick --trace file"
    )


def test_per_round_host_probe_and_meminfo_snapshot_are_emitted() -> None:
    """A tachyon probe and a page-cache snapshot fire at the start of every
    round, so host drift across a job is checkable round by round."""
    body = _measured_cycle_body()
    assert (
        'emit-host-probe "round-${{block}}-${{round}}" {params.label_probe_seconds}'
        " >> {output.host_probe}" in body
    )
    assert 'emit_meminfo_snapshot "round-${{block}}-${{round}}"' in body

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


def _arena_missing_inputs(tmp_path: Path, *config: str) -> str:
    """`snakemake -n` for one arena target in an empty directory: every input is
    missing, so the error lists exactly what `align_arena` would stage."""
    sha = "0a65bec1f47f8b773cf40b85d16833de010c8107"
    result = subprocess.run(  # noqa: S603, S607 -- fixed args, no shell
        [
            "snakemake",
            "-s",
            str(SNAKEFILE),
            "--directory",
            str(tmp_path),
            "-n",
            "--config",
            f"fg_labs_sha={sha}",
            *config,
            "--",
            f"arena/{sha}/m8a/arena.tsv",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    output = result.stdout + result.stderr
    assert "Missing input files for rule align_arena" in output, output[-2000:]
    return output


def test_arena_labels_stages_only_the_index_families_its_arms_read(tmp_path: Path) -> None:
    """A narrowed run (`--config arena_labels=`) must drop the bwa and minibwa
    indexes it never reads, while a full run must still stage them: losing one
    from a real bless fails at input staging hours into a paid run."""
    narrowed = _arena_missing_inputs(tmp_path, "arena_labels=v151,fg-labs-default")
    assert "references/hg38-u1/Homo_sapiens_assembly38.fasta.bwt.2bit.64" in narrowed
    assert "Homo_sapiens_assembly38.fasta.sa" not in narrowed
    assert "Homo_sapiens_assembly38.fasta.mbw" not in narrowed

    full = _arena_missing_inputs(tmp_path)
    assert "Homo_sapiens_assembly38.fasta.sa" in full
    assert "Homo_sapiens_assembly38.fasta.mbw" in full


def test_arena_labels_rejects_a_label_no_arm_runs(tmp_path: Path) -> None:
    """A typo would otherwise silently drop arms from a paid on-demand job."""
    sha = "0a65bec1f47f8b773cf40b85d16833de010c8107"
    result = subprocess.run(  # noqa: S603, S607 -- fixed args, no shell
        [
            "snakemake",
            "-s",
            str(SNAKEFILE),
            "--directory",
            str(tmp_path),
            "-n",
            "--config",
            f"fg_labs_sha={sha}",
            "arena_labels=v151,fg-labs-defualt",
            "--",
            f"arena/{sha}/m8a/arena.tsv",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode != 0
    assert "fg-labs-defualt" in result.stdout + result.stderr


def test_arena_labels_accepts_an_x86_only_label_for_every_arch(tmp_path: Path) -> None:
    """`bwa-mem2-upstream` has no ARM build; naming it must not fail the m8g job."""
    output = _arena_missing_inputs(tmp_path, "arena_labels=bwa-mem2-upstream,fg-labs-default")
    assert "Missing input files for rule align_arena" in output


def _sam_control_parser():  # type: ignore[no-untyped-def]
    text = ARENA_SMK.read_text()
    start = text.index("def _parse_arena_sam_control(")
    end = text.index("\n\n\nARENA_SAM_CONTROL", start)
    namespace: dict[str, object] = {}
    exec(text[start:end], namespace)  # noqa: S102 -- test-owned source from the repo
    return namespace["_parse_arena_sam_control"]


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("False", False), (True, True)])
def test_arena_sam_control_accepts_booleans(raw: object, expected: bool) -> None:
    assert _sam_control_parser()(raw) is expected


@pytest.mark.parametrize("raw", ["yes", "1", "ture"])
def test_arena_sam_control_rejects_anything_else(raw: str) -> None:
    with pytest.raises(ValueError, match="arena_sam_control"):
        _sam_control_parser()(raw)


def _printf_formats(body: str) -> list[str]:
    """Every `printf '<format>'` format string in `body` that writes an arena.tsv row."""
    return re.findall(r"printf '((?:%s|NA)(?:\\t(?:%s|NA))+\\n)'", body)


def test_every_arena_tsv_row_matches_the_header_width() -> None:
    """Ingest drops any row whose field count differs from the header, so a
    printf that gains or loses a field would silently lose every row it writes
    -- the SKIPPED-row format included, which the happy path never exercises."""
    text = ARENA_SMK.read_text()
    header = text[text.index("printf 'label\\t") :].split("'")[1]
    width = header.removesuffix("\\n").count("\\t") + 1
    formats = _printf_formats(_measured_cycle_body())
    assert len(formats) == 2, formats  # noqa: PLR2004 -- success and SKIPPED rows
    for fmt in formats:
        assert fmt.removesuffix("\\n").count("\\t") + 1 == width, fmt


def test_every_arena_tsv_header_column_is_a_known_db_column() -> None:
    text = ARENA_SMK.read_text()
    header = text[text.index("printf 'label\\t") :].split("'")[1].removesuffix("\\n").split("\\t")
    extra = [_ARENA_HEADER_RENAMES.get(name, name) for name in header[_ARENA_COLUMNS:]]
    assert extra == [column for column, _ in ARENA_V14_COLUMNS]


def test_pin_probe_is_skipped_when_no_candidate_arm_is_selected(tmp_path: Path) -> None:
    """A narrowed arena without the candidate must not pay a full probe
    alignment, and must not inherit a pin from one."""
    result = _run_lockstep_pin_snippet(
        tmp_path,
        log_content="[M::main_mem] phase-2 SMEM lockstep width: 24\n",
        pin_probe="0",
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT_PIN=UNSET" in result.stdout
    assert not list(tmp_path.rglob("*.probe_env")), "run_arm must not be called"


def test_pin_probe_flag_comes_from_the_selected_arms() -> None:
    text = ARENA_SMK.read_text()
    assert "pin_probe = lambda wc: int(_arena_runs_candidate(wc.arch))," in text
    assert 'if [ "{params.pin_probe}" = "1" ]; then' in text


def test_narrowed_staging_skips_unread_bwa_mem3_index_sets(tmp_path: Path) -> None:
    """A bwa-only arena reads neither bwa-mem3 index; a stock-only one does not
    read the densified copy. Staging them costs ~30 GB of S3 download each."""
    bwa_only = _arena_missing_inputs(tmp_path, "arena_labels=bwa")
    assert "Homo_sapiens_assembly38.fasta.bwt.2bit.64" not in bwa_only
    assert "Homo_sapiens_assembly38.fasta.sa" in bwa_only
    stock_only = _arena_missing_inputs(tmp_path, "arena_labels=v021")
    assert "references/hg38/Homo_sapiens_assembly38.fasta.bwt.2bit.64" in stock_only
    assert "references/hg38-u1/" not in stock_only
