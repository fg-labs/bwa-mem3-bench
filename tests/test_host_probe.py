"""Guards for the tachyon host-contention probe.

The probe answers "how contended was the machine while we measured", which is the
one thing `instance_id` cannot say. On the v0.9.0 bless the perf gate failed
`hic-1M-compat`/m7i at +13.0%; re-running the PREVIOUS release's unchanged binary
the same day measured it 18.7% slower than its own recorded number. `cpu_time /
wall` held at 14.1-14.7 of 16 vCPUs on every rep, so no vCPU was lost — identical
work simply burned ~20% more CPU-seconds waiting on memory a co-tenant was
consuming. Nothing on record could show that at query time.

Two failure modes these tests exist to prevent:

1. **A probe that aborts the work it was measuring.** The reading is diagnostic;
   the ladder is not. `emit-host-probe` runs inside a `set -e` shell body, so any
   non-zero exit would destroy a ~45-minute Batch job over a diagnostic.

2. **A reading with no provenance.** A score is only comparable to another taken
   with the same probe build. The runtime image has no Rust toolchain, so `rustc`
   must be captured at image build time and copied in — a coupling between the
   Dockerfile and this script that nothing else would catch.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT

# Exit code the script uses for a malformed argument list.
USAGE_EXIT = 2

SCRIPT_REPO_PATH = "docker/emit-host-probe.sh"
INSTALLED_SCRIPT = "/usr/local/bin/emit-host-probe"
SCRIPT = Path(REPO_ROOT) / "docker" / "emit-host-probe.sh"
DOCKERFILE = Path(REPO_ROOT) / "docker" / "Dockerfile"
BASE_DOCKERFILE = Path(REPO_ROOT) / "docker" / "Dockerfile.base"
SCALING_SMK = Path(REPO_ROOT) / "workflow" / "rules" / "scaling.smk"

# Every field the ingest reads. Named here so a rename in the script that ingest
# does not follow fails loudly instead of silently writing NULLs.
#
# `working_set_bytes_per_thread` was missing from this tuple while ingest read it,
# which is precisely how the degraded record came to omit it: the test that claims
# to enforce this list did not cover the one field that differed. Keep it in sync
# with `_ingest_host_probes`.
PROBE_FIELDS = (
    "phase",
    "status",
    "million_accesses_per_sec",
    "ns_per_access",
    "threads",
    "working_set_bytes_per_thread",
    "seconds",
    "probe_version",
    "rustc",
)

# A PATH with no tachyon on it, but with the interpreters the script needs. The
# script is invoked by absolute path so its shebang still resolves.
_NO_TACHYON_PATH = {"PATH": "/usr/bin:/bin"}


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _code_match(pattern: str, text: str) -> re.Match[str] | None:
    """Match `pattern` on a line of `text` that is not a `#` comment.

    The single place the comment-blind pattern is written. `_in_code` and
    `_code_offset` are both thin wrappers over it so they cannot drift into
    disagreeing about what counts as code — which they would, silently, since one
    returns a bool and the other a position and no test compares the two.
    """
    return re.search(rf"^[^#\n]*{pattern}", text, re.MULTILINE)


def _in_code(pattern: str, text: str) -> bool:
    """True if `pattern` matches a line of `text` that is not a `#` comment.

    Every assertion in this module that checks a Dockerfile instruction or a
    snakemake shell command goes through here, because a bare substring check is
    satisfied by a COMMENT — and these files necessarily discuss in prose the very
    commands they run. Proved, not assumed: commenting out both `emit-host-probe`
    invocations in `scaling.smk` left all four of this module's rule assertions
    green until they were anchored this way.

    CLAUDE.md records the same trap for `build.py`, and `tests/test_compat_arm.py`
    solves it for a different shape with `_code_only()` (which strips comments and
    docstrings from a whole rule body). Anchoring per-pattern is the narrower form:
    it needs no rule-body extraction, and the leading `\\s*` still tolerates the
    indentation both Dockerfile `RUN` continuations and snakemake shell bodies use.
    """
    return _code_match(pattern, text) is not None


def test_in_code_ignores_commented_out_lines() -> None:
    """The guard's own guard.

    Every source assertion in this module trusts `_in_code`, so if it silently
    started matching comments they would all go quietly weak — which is exactly
    the state they were in before it existed.
    """
    assert _in_code(r"emit-host-probe pre", "        emit-host-probe pre 10 > out")
    assert not _in_code(r"emit-host-probe pre", "        # emit-host-probe pre 10 > out")
    assert not _in_code(r"emit-host-probe pre", "# emit-host-probe pre")
    # A trailing comment on a real command line must still count as code.
    assert _in_code(r"chmod \+x", "RUN chmod +x /usr/local/bin/x  # make it runnable")


def _code_offset(pattern: str, text: str) -> int:
    """Source offset of the first non-comment match of `pattern`.

    The ordering assertions below need a position, not a boolean, and they need
    the same comment-blindness `_in_code` provides: `scaling.smk` discusses the
    probe ordering in prose directly above the commands that implement it, so a
    naive `text.index()` would measure the comment's position instead.
    """
    match = _code_match(pattern, text)
    assert match is not None, f"no non-comment match for {pattern!r}"
    return match.start()


def test_dockerfile_installs_the_script() -> None:
    """The ladder shells out to `emit-host-probe`; it must be on PATH, executable.

    Asserts the actual instructions, not the bare word — which a mere mention in a
    comment would satisfy while the rule still died with "command not found".
    """
    text = DOCKERFILE.read_text()
    assert re.search(
        rf"^\s*COPY\s+{re.escape(SCRIPT_REPO_PATH)}\s+{re.escape(INSTALLED_SCRIPT)}\s*$",
        text,
        re.MULTILINE,
    ), f"Dockerfile does not COPY {SCRIPT_REPO_PATH} to {INSTALLED_SCRIPT}"
    assert _in_code(rf"chmod\s+\+x\s+{re.escape(INSTALLED_SCRIPT)}\b", text), (
        f"Dockerfile does not chmod +x {INSTALLED_SCRIPT}; the ladder would get "
        "'permission denied' instead of a reading."
    )


def _provenance_path() -> str:
    """The default provenance path the script reads, taken from the script itself."""
    match = re.search(r"^_DEFAULT_PROVENANCE=(\S+)$", SCRIPT.read_text(), re.MULTILINE)
    assert match, "emit-host-probe no longer defines a _DEFAULT_PROVENANCE path"
    return match.group(1)


def test_the_runtime_image_receives_the_provenance_the_script_reads() -> None:
    """The builder must write it, and the runtime must copy it to that same path.

    Three files have to agree and nothing else checks them: the builder writes
    /out/share/..., the runtime COPYs /out/share/ to /usr/local/share/, and this
    script reads the result. A reading with a null `rustc` is still usable, so
    breaking this coupling degrades SILENTLY into unattributable scores — the
    exact failure the probe exists to end.
    """
    text = DOCKERFILE.read_text()
    # The write happens in the BASE image (docker/Dockerfile.base) -- tachyon is
    # cargo-installed there, and only that stage has a Rust toolchain to ask for
    # its version. `docker/Dockerfile`'s builder stage IS that base plus more, so
    # the runtime `COPY --from=builder /out/share/` below still reaches it. The
    # coupling now spans three files instead of two, which is precisely why it is
    # worth asserting.
    base_text = BASE_DOCKERFILE.read_text()
    installed = _provenance_path()
    assert installed.startswith("/usr/local/share/"), (
        f"expected the provenance under /usr/local/share/, got {installed}"
    )
    built = installed.replace("/usr/local/share/", "/out/share/", 1)
    assert _in_code(re.escape(built), base_text), (
        f"the base image never writes {built}, so {installed} will not exist in "
        "the runtime image and every recorded score will have a null `rustc`."
    )
    assert re.search(
        r"^\s*COPY\s+--from=builder\s+/out/share/\s+/usr/local/share/\s*$", text, re.MULTILINE
    ), (
        "the runtime stage does not COPY /out/share/ from the builder; the "
        "provenance file would be built and then thrown away."
    )
    assert _in_code(re.escape("rustc +stable --version"), base_text), (
        "the base image must record `rustc +stable --version`: the runtime image has "
        "no Rust toolchain, so this cannot be recovered later, and `cargo +stable "
        "install` floats the compiler between rebuilds."
    )


def test_the_ladder_probes_its_own_host_before_and_after() -> None:
    """Two readings, in the rule's own shell body, bracketing the timed work.

    In the rule's shell for the same reason `emit-host-meta` is: only the working
    shell is guaranteed to be on the working machine. Two of them because one
    pre-flight sample cannot tell a quiet host from a host whose neighbour arrived
    after we started.
    """
    text = SCALING_SMK.read_text()
    assert _in_code(r"host-probe\.jsonl", text), "scaling.smk must declare the probe output"
    assert _in_code(r"emit-host-probe pre", text), "the ladder must take a `pre` reading"
    assert _in_code(r"emit-host-probe post", text), "the ladder must take a `post` reading"
    assert _in_code(r"emit-host-meta", text), (
        "the ladder must capture host identity too; a contention score with no "
        "instance_id cannot be joined to anything."
    )
    # Presence is not the property — BRACKETING is. Both readings could sit above
    # the loop and every assertion above would still pass, while the pair measured
    # the same instant twice and could no longer tell a quiet host from one whose
    # neighbour arrived mid-ladder. `for spec in` opens the timed rung loop.
    pre = _code_offset(r"emit-host-probe pre", text)
    timed_work = _code_offset(r"for spec in", text)
    post = _code_offset(r"emit-host-probe post", text)
    assert pre < timed_work, "the `pre` reading must be taken BEFORE the rung loop"
    assert timed_work < post, "the `post` reading must be taken AFTER the rung loop"


def test_the_first_reading_truncates_and_the_second_appends() -> None:
    """A retried job must produce one pair of readings, not three.

    A spot interruption can leave a partial output behind, so appending both
    readings would stack a second `pre` onto the survivor. The DB upsert would
    absorb it (it is keyed on phase), which is exactly why this would go unnoticed
    while the recorded file quietly disagreed with itself.
    """
    text = SCALING_SMK.read_text()
    assert _in_code(r"emit-host-probe pre \S+ > \{output\.host_probe\}", text), (
        "the `pre` reading must TRUNCATE (`>`) so a retry starts a fresh file"
    )
    assert _in_code(r"emit-host-probe post \S+ >> \{output\.host_probe\}", text), (
        "the `post` reading must APPEND (`>>`) or it would overwrite the `pre` one"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_probe_cannot_abort_the_ladder_when_tachyon_is_missing() -> None:
    """No tachyon must yield exit 0 and one valid record, not a failed job."""
    out = _run("pre", "1", env=_NO_TACHYON_PATH)
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["phase"] == "pre"
    assert payload["status"] == "unavailable"
    # Null, never 0: a zero would read as "an infinitely contended host" and would
    # be indistinguishable from a real reading in an average.
    assert payload["million_accesses_per_sec"] is None
    assert payload["ns_per_access"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_probe_cannot_abort_the_ladder_in_a_hostile_environment() -> None:
    """Stripping PATH removes tachyon and python3 at once — still exit 0."""
    out = _run("post", "1", env={"PATH": "/nonexistent"})
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["phase"] == "post"
    assert payload["status"] == "unavailable"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_degraded_record_carries_every_field_the_ingest_reads() -> None:
    """A short record would make ingest's `.get()` calls silently mean nothing.

    The degraded and healthy records must be the same SHAPE, so a consumer never
    has to branch on which one it got.
    """
    payload = json.loads(_run("pre", "1", env=_NO_TACHYON_PATH).stdout)
    for field in PROBE_FIELDS:
        assert field in payload, f"degraded record is missing {field!r}"
    # Present AND null. A degraded reading that reported 0 would average in as an
    # infinitely contended host rather than as "not measured".
    for field in ("million_accesses_per_sec", "ns_per_access", "threads", "seconds"):
        assert payload[field] is None, f"{field} should be null when the probe cannot run"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_record_is_newline_terminated_so_readings_can_be_appended() -> None:
    """The rule appends both readings to one .jsonl with `>>`.

    Without the trailing newline the second record concatenates onto the first and
    BOTH are lost — and the ladder still succeeds, so nothing would flag it.
    """
    out = _run("pre", "1", env=_NO_TACHYON_PATH)
    assert out.stdout.endswith("\n"), "record must end with a newline"
    combined = out.stdout + _run("post", "1", env=_NO_TACHYON_PATH).stdout
    lines = combined.splitlines()
    assert [json.loads(line)["phase"] for line in lines] == ["pre", "post"]


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
@pytest.mark.parametrize(
    "args",
    [
        [],  # phase is required
        ["pre", "10", "extra"],
        ["Pre"],  # phase is part of the ingest key; case matters
        ["pre!"],
        ["pre", "0"],  # a zero-second probe measures nothing
        ["pre", "0.0"],
        ["pre", "-5"],
        ["pre", "abc"],
    ],
)
def test_probe_rejects_a_malformed_argument_list(args: list[str]) -> None:
    """Bad ARGUMENTS are a workflow bug and must fail loudly, unlike a bad env.

    The degrade-to-unavailable contract exists for environments the worker cannot
    control. Swallowing a wrong call as well would turn a broken rule into a file
    full of nulls that looks like a quiet host.
    """
    out = _run(*args)
    assert out.returncode == USAGE_EXIT, f"expected a usage failure, got {out.returncode}"
    assert out.stdout == "", "a usage error must not write a reading"
    assert "usage:" in out.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_a_fractional_second_budget_is_accepted() -> None:
    """`host_probe_seconds` is a float in config, so the script must take one."""
    out = _run("pre", "0.5", env=_NO_TACHYON_PATH)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["phase"] == "pre"


# --------------------------------------------------------------------------- #
# The healthy path, against a stub tachyon. The real binary only exists inside
# the image, so these use a stub emitting tachyon 0.1.0's actual JSON shape —
# which is what the assertions here pin: if tachyon renames a field, ingest reads
# NULL for it and no other test notices.
# --------------------------------------------------------------------------- #

STUB_RATE = 28.4
STUB_NS = 126.05
STUB_THREADS = 64
STUB_WORKING_SET = 67108864
STUB_RUSTC = "rustc 1.97.1 (8bab26f4f 2026-07-14)"


@pytest.fixture
def stub_tachyon(tmp_path: Path) -> dict[str, str]:
    """A PATH carrying a stub `tachyon` that echoes a real 0.1.0 --json payload."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = json.dumps(
        {
            "probe": "memory-chase",
            "version": "0.1.0",
            "million_accesses_per_sec": STUB_RATE,
            "ns_per_access": STUB_NS,
            "accesses": 286_000_000,
            "elapsed_s": 10.004,
            "threads": STUB_THREADS,
            "working_set_bytes_per_thread": STUB_WORKING_SET,
        }
    )
    stub = bin_dir / "tachyon"
    stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n")
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_healthy_reading_carries_the_measurement_and_the_probe_release(
    stub_tachyon: dict[str, str],
) -> None:
    """Field names are the contract between tachyon, this script, and ingest."""
    out = _run("pre", "1", env=stub_tachyon)
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["status"] == "ok"
    assert payload["million_accesses_per_sec"] == STUB_RATE
    assert payload["ns_per_access"] == STUB_NS
    assert payload["threads"] == STUB_THREADS
    assert payload["working_set_bytes_per_thread"] == STUB_WORKING_SET
    # tachyon's REPORTED release, not the version the image meant to install:
    # the payload is the authority on what actually took the measurement.
    assert payload["probe_version"] == "0.1.0"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_healthy_and_degraded_records_have_identical_key_sets(
    stub_tachyon: dict[str, str],
) -> None:
    """The shape contract, asserted as a shape rather than as a list to maintain.

    `PROBE_FIELDS` is a hand-kept list and it already drifted once: it omitted
    `working_set_bytes_per_thread` while ingest read that field, so the degraded
    record was free to omit it too and no test objected. Comparing the two records
    to each other cannot drift, because neither side is written down here.
    """
    healthy = json.loads(_run("pre", "1", env=stub_tachyon).stdout)
    degraded = json.loads(_run("pre", "1", env=_NO_TACHYON_PATH).stdout)
    assert healthy.keys() == degraded.keys()
    # And the list above must not fall behind either record.
    assert set(PROBE_FIELDS) <= healthy.keys()


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_rustc_provenance_is_merged_when_the_image_recorded_it(
    stub_tachyon: dict[str, str], tmp_path: Path
) -> None:
    """The whole point of the builder-stage capture: it reaches the score."""
    provenance = tmp_path / "tachyon-provenance.json"
    provenance.write_text(json.dumps({"tachyon_version": "0.1.0", "rustc": STUB_RUSTC}))
    out = _run(
        "post",
        "1",
        env={**stub_tachyon, "BWA_MEM3_BENCH_TACHYON_PROVENANCE": str(provenance)},
    )
    assert json.loads(out.stdout)["rustc"] == STUB_RUSTC


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
@pytest.mark.parametrize("body", ["", "not json", '{"no_rustc_key": 1}'])
def test_an_unusable_provenance_file_still_yields_a_reading(
    stub_tachyon: dict[str, str], tmp_path: Path, body: str
) -> None:
    """An image built before the capture existed must still produce a score.

    Unattributable is worse than attributable, but far better than nothing — and
    making provenance mandatory would mean an old image records no host data at
    all, which is the state that made the v0.9.0 investigation manual.
    """
    provenance = tmp_path / "tachyon-provenance.json"
    provenance.write_text(body)
    out = _run(
        "pre", "1", env={**stub_tachyon, "BWA_MEM3_BENCH_TACHYON_PROVENANCE": str(provenance)}
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["status"] == "ok"
    assert payload["million_accesses_per_sec"] == STUB_RATE
    assert payload["rustc"] is None


def test_every_script_running_test_is_skipped_on_windows() -> None:
    """The `win32` marks are per-test, so this is what stops the next one missing it.

    Per-test rather than a module-level `pytestmark` because roughly half this
    module only READS `emit-host-probe.sh` and the Dockerfile as text — those run
    fine on Windows and blanket-skipping them would be a real coverage loss. It
    matches `tests/test_host_metadata.py`, which draws the same line.

    But the per-test form is demonstrably easy to forget: all four stub-based
    tests were added without it, which is what CodeRabbit caught on this PR. So
    the convention is enforced here rather than merely documented — the same
    drift-detection pattern `test_locally_written_names_match_bench_defaults` and
    `test_supp_keys_mirror_the_report_struct` use elsewhere in this suite.

    A test "runs the script" if it calls `_run` (which execs the bash script) or
    takes the `stub_tachyon` fixture (which writes a `#!/bin/sh` stub and chmods
    it — both no-ops on Windows).

    Parsed with `ast`, not a regex over the source. A line-oriented pattern drops
    the mark that sits ABOVE a multi-line `@pytest.mark.parametrize`, which reads
    as "unmarked" for tests that are in fact marked — the same false negative that
    made this convention look already-satisfied when it was not.
    """
    # `ast.walk`, not `tree.body`: a test nested in a class would otherwise be
    # skipped silently, which is the failure this whole test exists to prevent.
    unmarked: list[str] = []
    for node in ast.walk(ast.parse(Path(__file__).read_text())):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        decorators = "".join(ast.unparse(d) for d in node.decorator_list)
        takes_stub = any(arg.arg == "stub_tachyon" for arg in node.args.args)
        calls_run = any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_run"
            for sub in ast.walk(node)
        )
        if (takes_stub or calls_run) and "win32" not in decorators:
            unmarked.append(node.name)
    assert not unmarked, (
        f"these tests exec the bash script but lack the win32 skip: {unmarked}. "
        "Add @pytest.mark.skipif(sys.platform == 'win32', reason='bash script')."
    )
