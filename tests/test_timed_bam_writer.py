"""Every timed alignment writes its BAM through a pipe, not to a file.

htslib's ``bgzf_close`` fdatasync()s an output that is a regular file, and bwa-mem3's
default single-threaded BGZF writer leaves the whole sync to close. With ``-o file`` the
timed region therefore ended with several seconds of disk flush after ``PROCESS()``
(~6 s for ~3.5 GB on a 500 MB/s gp3 volume, measured in the arena). Writing to stdout
and through ``cat`` removes the sync, as the arena already does (``arena.smk``). The
SAM-emitting upstream arms (bwa-mem2 / bwameth, minibwa, bwa) pipe ``samtools view -u``
the same way, so the speedup ratios compare like with like.

Read as text, as ``test_sweep_host_probe.py`` does: the rule files are not Python.
"""

from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[1] / "workflow" / "rules"


def _timed_command(smk: str, rule_name: str) -> str:
    """The ``tricorder ... bash -c`` invocation of one rule, up to the record-count check."""
    text = (RULES / smk).read_text()
    start = text.index(f"rule {rule_name}:")
    nxt = text.find("\nrule ", start + 1)
    body = text[start:] if nxt == -1 else text[start:nxt]
    begin = body.index("tricorder --out")
    return body[begin : body.index("samtools view -c", begin)]


@pytest.mark.parametrize(
    ("smk", "rule_name", "dest"),
    [
        ("align.smk", "align_fg_labs", '| cat > "{output.bam}.raw"'),
        ("scaling.smk", "align_thread_scaling", "| cat > '$OUTDIR/runs/o.bam'"),
    ],
)
def test_timed_bam_goes_to_stdout_through_cat(smk: str, rule_name: str, dest: str) -> None:
    """``--bam=0 -o -`` piped into ``cat``, under pipefail, and never ``-o <file>``."""
    cmd = _timed_command(smk, rule_name)
    assert "--bam=0 -o - " in cmd
    assert dest in cmd
    assert "set -o pipefail;" in cmd, "an aligner failure must fail the pipe, not just cat's exit"
    assert '--bam=0 -o "' not in cmd and "--bam=0 -o '" not in cmd


@pytest.mark.parametrize(
    ("smk", "rule_name"),
    [
        ("align.smk", "align_baseline"),
        ("align_minibwa.smk", "align_minibwa"),
        ("align_bwa.smk", "align_bwa"),
    ],
)
def test_samtools_timed_bam_goes_to_stdout_through_cat(smk: str, rule_name: str) -> None:
    """The SAM-emitting arms' ``samtools view -u`` writes stdout into ``cat``, as the arena does.

    These arms are the denominators of ``wall_speedup`` / ``minibwa_speedup``; leaving
    them on ``-o <file>`` would keep a close-time sync the bwa-mem3 arm no longer pays.
    Every timed branch (align_baseline has two) must use the pipe.
    """
    cmd = _timed_command(smk, rule_name)
    branches = cmd.count("tricorder --out")
    assert branches >= 1
    assert cmd.count('samtools view -@4 -u -o - - | cat > "{output.bam}.raw"') == branches
    assert cmd.count("set -o pipefail;") == branches
    assert "-o {output.bam}.raw" not in cmd
    assert '-o "{output.bam}.raw"' not in cmd


def _every_timed_command() -> list[tuple[str, str]]:
    """Every ``tricorder --out`` invocation under workflow/rules, as (file, command).

    The command runs to the first line without a trailing backslash continuation.
    """
    found = []
    for smk in sorted(RULES.glob("*.smk")):
        lines = smk.read_text().splitlines()
        for i, line in enumerate(lines):
            if "tricorder --out" not in line:
                continue
            end = i
            while lines[end].rstrip().endswith("\\"):
                end += 1
            found.append((smk.name, "\n".join(lines[i : end + 1])))
    return found


def test_no_timed_command_writes_to_a_file_directly() -> None:
    """A new timed rule cannot slip back to ``-o <file>``: every site ends in ``| cat >``."""
    commands = _every_timed_command()
    assert len(commands) >= 6  # noqa: PLR2004 -- the six known sites; more is fine
    for name, cmd in commands:
        assert "| cat > " in cmd, f"{name}: timed command does not write through cat:\n{cmd}"
        assert "-o {output" not in cmd and '-o "{output' not in cmd, f"{name}:\n{cmd}"
