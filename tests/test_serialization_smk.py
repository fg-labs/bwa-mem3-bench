"""Workflow wiring, config rejection and opt-in end-to-end runs of `serialization.smk`.

The end-to-end tests need `BWA_MEM3_BENCH_SERIALIZATION_BIN` (a bwa-mem3 with `--bam`), plus
`samtools`, `mawk` and `fgumi` on PATH, and are skipped otherwise. The timing tests replace
`taskset` with a shim that drops its CPU list, and `tricorder` with a stand-in when it is absent.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from bwa_mem3_bench import REPO_ROOT

SNAKEFILE = Path(REPO_ROOT) / "workflow" / "Snakefile"
SHA = "abc1234"
PAIRS = 32


def _snakemake(
    directory: Path,
    *args: str,
    config: tuple[str, ...] = (),
    snakefile: Path = SNAKEFILE,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, S607 -- fixed args, no shell
        [
            "snakemake",
            "-s",
            str(snakefile),
            "--directory",
            str(directory),
            *args,
            "--config",
            *config,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env=env,
    )


def _dry_run(
    directory: Path, target: str, *config: str, snakefile: Path = SNAKEFILE
) -> subprocess.CompletedProcess[str]:
    """Dry-run `target`, first touching every input the DAG reports missing."""
    for _ in range(20):  # `all` reports its missing inputs a few rule families at a time
        result = _snakemake(
            directory,
            "-n",
            "-p",
            config=(f"fg_labs_sha={SHA}", *config, "--", target),
            snakefile=snakefile,
        )
        text = result.stdout + result.stderr
        if "affected files:" not in text:
            return result
        for line in text.split("affected files:", 1)[1].splitlines()[1:]:
            path = line.strip()
            if not path or " " in path or ":" in path:
                break
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).touch()
    return result


def test_workflow_lists_the_targets(tmp_path: Path) -> None:
    result = _snakemake(tmp_path, "--list")
    assert result.returncode == 0, result.stderr[-2000:]
    names = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    assert {
        "serialization",
        "serialization_fidelity",
        "serialization_timing",
        "serialization_smoke",
    } <= names


def test_smoke_dry_run_schedules_two_fidelity_and_three_timing_jobs(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, "serialization_smoke")
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    lines = result.stdout.splitlines()
    assert sum(line.startswith("rule serialization_fidelity_run:") for line in lines) == 2  # noqa: PLR2004
    assert sum(line.startswith("rule serialization_timing_run:") for line in lines) == 3  # noqa: PLR2004
    outputs = [line for line in lines if line.strip().startswith("output:")]
    assert all(f"serialization/{SHA}/m8a/smoke-1M/" in line for line in outputs)
    assert not any(".bam" in line for line in outputs)


def test_full_target_needs_samples(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, "serialization")
    assert result.returncode != 0
    assert "serialization_samples is empty" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            ["serialization_modes=default;touch /tmp/x"],
            "serialization_modes: 'default;touch /tmp/x' is not one of",
        ),
        (["serialization_budget=True"], "serialization_budget must be a positive integer"),
        (["serialization_budget=2"], "must be at least serialization_helpers + 1"),
        (["serialization_budget=16"], "must be below arena.threads (16)"),
        (["serialization_fidelity_threads=17"], "must be at most arena.threads (16)"),
        (["serialization_archs=c6a"], "serialization_archs: 'c6a' is not one of"),
        (["serialization_aligner=bwa;ls"], "serialization_aligner is not a plain executable path"),
        (["serialization_samples=wgs-5M-fast"], "'--fast' sets or abbreviates --fast"),
        (["serialization_samples=wgs-5M-compat"], "--compat=bwa-mem2 cannot run in mode fast"),
        (
            ["serialization_samples=meth-twist-emseq-trim-5M"],
            "must be a paired-end, non-methylation",
        ),
        (["serialization_samples=sbx-1M"], "must be a paired-end, non-methylation sample"),
        (["serialization_samples=nosuch"], "serialization_samples: 'nosuch' is not one of"),
        (["serialization_modes=,"], "serialization_modes must not be empty"),
        (["serialization_indexes=stock,stock"], "lists 'stock' more than once"),
        (["serialization_bogus=1"], "unknown serialization settings ['serialization_bogus']"),
        (["serialization_tools=bwa-mem2"], "leave m8g with no arm to time: upstream bwa-mem2"),
        (["serialization_modes=fast"], "run only in default mode on the stock index"),
        (["fg_labs_sha=abc12"], "7-40 hex chars"),
    ],
)
def test_bad_config_is_rejected(tmp_path: Path, entries: list[str], message: str) -> None:
    result = _dry_run(tmp_path, "serialization", "serialization_samples=smoke-1M", *entries)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr


@pytest.mark.parametrize(
    "entries",
    [
        ["serialization_samples=wgs-5M-fast"],
        ["serialization_samples=nosuch"],
        ["serialization_archs=c6a"],
        ["serialization_tools=bwa-mem2"],
    ],
)
def test_smoke_ignores_full_target_selection(tmp_path: Path, entries: list[str]) -> None:
    """The smoke runs its own sample on m8a; the full target's samples and archs never gate it."""
    result = _dry_run(tmp_path, "serialization_smoke", *entries)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def test_smoke_checks_the_budget_for_every_endpoint(tmp_path: Path) -> None:
    """The smoke runs pinned and unpinned whatever `serialization_endpoints` selects."""
    result = _dry_run(
        tmp_path, "serialization_smoke", "serialization_endpoints=pinned", "serialization_budget=16"
    )
    assert result.returncode != 0
    assert "below arena.threads" in result.stdout + result.stderr


@pytest.mark.parametrize("leaf", ["fidelity/default/result.tsv", "timing/pinned/timings.tsv"])
def test_output_sha_other_than_the_image_sha_is_rejected(tmp_path: Path, leaf: str) -> None:
    result = _dry_run(tmp_path, f"serialization/def5678/m8a/smoke-1M/{leaf}")
    assert result.returncode != 0
    assert (
        "serialization output sha 'def5678' differs from the configured fg_labs_sha 'abc1234'"
        in (result.stdout + result.stderr)
    )


@pytest.mark.parametrize(
    ("target", "entries"),
    [
        ("serialization_smoke", ["serialization_budget=15"]),
        ("serialization_smoke", ["serialization_budget=3"]),
        ("serialization_smoke", ["fg_labs_sha=abc1234-lto"]),
        (
            "serialization_smoke",
            ["serialization_samples=wgs-5M-compat", "serialization_modes=default"],
        ),
        (
            "serialization_timing",
            ["serialization_samples=smoke-1M", "serialization_endpoints=pinned"]
            + ["serialization_budget=16"],
        ),
    ],
)
def test_boundary_config_is_accepted(tmp_path: Path, target: str, entries: list[str]) -> None:
    result = _dry_run(tmp_path, target, *entries)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def test_unknown_sample_wildcard_is_rejected(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, f"serialization/{SHA}/m8a/nosuch/fidelity/default/result.tsv")
    assert result.returncode != 0
    assert "serialization: unknown sample 'nosuch'" in result.stdout + result.stderr


def _repo_copy(tmp_path: Path, sample: str = "", **arena: str | int) -> Path:
    """Copy the workflow and config, add a sample and override `arena` keys; the Snakefile."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(Path(REPO_ROOT) / "workflow", root / "workflow")
    shutil.copytree(Path(REPO_ROOT) / "config", root / "config")
    for name in ("bwa_mem3_bench", "docs", "docker"):
        (root / name).symlink_to(Path(REPO_ROOT) / name)
    samples = root / "config" / "samples.yaml"
    samples.write_text(samples.read_text().replace("\nsamples:\n", f"\nsamples:{sample}\n", 1))
    defaults = root / "config" / "defaults.yaml"
    lines = defaults.read_text().splitlines(keepends=True)
    start = lines.index("arena:\n")
    for key, value in arena.items():
        index = next(i for i in range(start, len(lines)) if lines[i].startswith(f"  {key}:"))
        lines[index] = f"  {key}: {value}\n"
    defaults.write_text("".join(lines))
    return root / "workflow" / "Snakefile"


def _sample(name: str, flags: str) -> str:
    return (
        f"\n  {name}:\n    baseline_tool: bwa-mem2-upstream\n    reference: hg38\n"
        f"    source: data/smoke/1M/\n    {flags}\n"
    )


@pytest.mark.parametrize(
    ("flags", "config", "message"),
    [
        ("fg_labs_flags: [-5St4]", (), "controls: '-5St4' sets -t"),
        ("mem_flags: [-Mt4]", (), "controls: '-Mt4' sets -t"),
        ("mem_flags: [-M]", (), "mem_flag '-M' has no minibwa equivalent"),
        ("mem_flags: [-M]", ("serialization_tools=bwa-mem3",), None),
        ("fg_labs_flags: [-xont2d]", (), None),
    ],
)
def test_sample_flags_are_parsed_getopt_style(
    tmp_path: Path, flags: str, config: tuple[str, ...], message: str | None
) -> None:
    """Flags hidden in a bundle are rejected; option values and minibwa-only problems are not."""
    snakefile = _repo_copy(tmp_path, sample=_sample("flagged-1M", flags))
    result = _dry_run(
        tmp_path / "work",
        "serialization_fidelity",
        "serialization_samples=flagged-1M",
        *config,
        snakefile=snakefile,
    )
    if message is None:
        assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    else:
        assert result.returncode != 0
        assert message in result.stdout + result.stderr


# Arena settings that leave the default serialization settings unrunnable.
_UNRUNNABLE_ARENAS = {
    "no-densification": {"dense_sa_shift": 3},
    "no-m8g": {"archs": "[m8a]"},
    "no-m8a": {"archs": "[m8g]"},
    "few-threads": {"threads": 8},
}


@pytest.mark.parametrize("target", ["all", "smoke"])
@pytest.mark.parametrize("arena", list(_UNRUNNABLE_ARENAS))
def test_existing_targets_unaffected_by_serialization_preconditions(
    tmp_path: Path, arena: str, target: str
) -> None:
    """Serialization preconditions on other config fire only when a serialization target runs."""
    snakefile = _repo_copy(tmp_path, **_UNRUNNABLE_ARENAS[arena])
    result = _dry_run(tmp_path / "work", target, snakefile=snakefile)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]


@pytest.mark.parametrize(
    ("arena", "target", "message"),
    [
        (
            "no-densification",
            "serialization_smoke",
            "there is no dense index: pass serialization_indexes=stock",
        ),
        ("no-m8g", "serialization_timing", "serialization_archs: 'm8g' is not one of ['m8a']"),
        ("no-m8a", "serialization_smoke", "the smoke arch m8a is not in arena.archs ['m8g']"),
        (
            "few-threads",
            "serialization_smoke",
            "serialization_fidelity_threads must be at most arena.threads (8)",
        ),
    ],
)
def test_serialization_target_still_rejects_unrunnable_arena(
    tmp_path: Path, arena: str, target: str, message: str
) -> None:
    snakefile = _repo_copy(tmp_path, **_UNRUNNABLE_ARENAS[arena])
    rejected = _dry_run(
        tmp_path / "work", target, "serialization_samples=smoke-1M", snakefile=snakefile
    )
    assert rejected.returncode != 0
    assert message in rejected.stdout + rejected.stderr


@pytest.mark.parametrize(
    ("arena", "target", "entries"),
    [
        ("no-m8g", "serialization_smoke", []),
        ("no-m8a", "serialization_timing", ["serialization_archs=m8g"]),
    ],
)
def test_serialization_target_checks_only_its_own_archs(
    tmp_path: Path, arena: str, target: str, entries: list[str]
) -> None:
    """The smoke needs only m8a; a full target needs only the archs it selects."""
    snakefile = _repo_copy(tmp_path, **_UNRUNNABLE_ARENAS[arena])
    accepted = _dry_run(
        tmp_path / "work", target, "serialization_samples=smoke-1M", *entries, snakefile=snakefile
    )
    assert accepted.returncode == 0, (accepted.stdout + accepted.stderr)[-3000:]


def test_dense_rejection_is_lifted_by_a_stock_only_run(tmp_path: Path) -> None:
    snakefile = _repo_copy(tmp_path, dense_sa_shift=3)
    accepted = _dry_run(
        tmp_path / "work", "serialization_smoke", "serialization_indexes=stock", snakefile=snakefile
    )
    assert accepted.returncode == 0, (accepted.stdout + accepted.stderr)[-3000:]


# -- Opt-in end-to-end fidelity on a tiny fixture -----------------------------

BIN = os.environ.get("BWA_MEM3_BENCH_SERIALIZATION_BIN", "")
needs_tools = pytest.mark.skipif(
    not (BIN and Path(BIN).is_file() and all(map(shutil.which, ("samtools", "mawk", "fgumi")))),
    reason="needs BWA_MEM3_BENCH_SERIALIZATION_BIN, and samtools, mawk and fgumi on PATH",
)


@pytest.fixture(scope="module")
def shared_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A local stand-in for the shared bucket: the indexed hg38 slot and the smoke reads."""
    root = tmp_path_factory.mktemp("shared")
    rng = random.Random(170)
    sequence = "".join(rng.choices("ACGT", k=100_000))
    fasta = root / "references" / "hg38" / "Homo_sapiens_assembly38.fasta"
    fasta.parent.mkdir(parents=True)
    fasta.write_text(f">chr1\n{sequence}\n")
    for argv in (
        [BIN, "index", "--emit-unpacked-ref", str(fasta)],
        ["samtools", "faidx", str(fasta)],
        ["samtools", "dict", "-o", str(fasta.with_suffix(".dict")), str(fasta)],
    ):
        subprocess.run(argv, check=True, capture_output=True)  # noqa: S603
    # The dense slot holds the same index; the timing tests tell the two apart by path.
    shutil.copytree(fasta.parent, root / "references" / "hg38-u1")
    complement = str.maketrans("ACGT", "TGCA")
    reads = root / "data" / "smoke" / "1M"
    reads.mkdir(parents=True)
    with gzip.open(reads / "r1.fq.gz", "wt") as r1, gzip.open(reads / "r2.fq.gz", "wt") as r2:
        for index in range(PAIRS):
            start = 1000 + index * 2000
            first = sequence[start : start + 100]
            second = sequence[start + 250 : start + 350].translate(complement)[::-1]
            r1.write(f"@pair{index}/1\n{first}\n+\n{'I' * 100}\n")
            r2.write(f"@pair{index}/2\n{second}\n+\n{'I' * 100}\n")
    return root


TASKSET_SHIM = """#!/bin/bash
# Test double for taskset: drops `-c <cpus>` and runs the command.
shift 2
exec "$@"
"""

TRICORDER_STAND_IN = """#!/bin/bash
# Test double for tricorder: runs the command and writes a two-line timing.tsv.
out=""
while [ "$1" != -- ]; do
    if [ "$1" = --out ]; then out=$2; fi
    shift
done
shift
start=$SECONDS
"$@"
rc=$?
printf 's\\tcpu_time\\tmax_rss\\n%s\\t-\\t-\\n' "$((SECONDS - start))" > "$out"
exit $rc
"""


def _script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


def _run_rule(
    tmp_path: Path, shared_root: Path, target: str, aligner: str, *config: str, bash: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run one serialization output locally, with the host shims on PATH; the output's directory.

    `bash`, when set, is the `bash` Snakemake finds first on PATH and runs the rule's shell with.
    """
    tools = tmp_path / "bin"
    tools.mkdir()
    if bash:
        (tools / "bash").symlink_to(bash)
    (tools / "emit-host-meta").symlink_to(Path(REPO_ROOT) / "docker" / "emit-host-meta.sh")
    _script(tools / "taskset", TASKSET_SHIM)
    if not shutil.which("tricorder"):
        _script(tools / "tricorder", TRICORDER_STAND_IN)
    settings = (
        f"fg_labs_sha={SHA}",
        f"shared_root={shared_root}",
        f"serialization_aligner={aligner}",
    )
    result = _snakemake(
        tmp_path / "work",
        "--cores",
        "2",
        config=(*settings, "serialization_fidelity_threads=2", *config, "--", target),
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
    )
    return result, (tmp_path / "work" / target).parent


def _run_fidelity(
    tmp_path: Path, shared_root: Path, mode: str, aligner: str, bash: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    target = f"serialization/{SHA}/m8a/smoke-1M/fidelity/{mode}/result.tsv"
    return _run_rule(tmp_path, shared_root, target, aligner, bash=bash)


def _bash_major(bash: str) -> int:
    if not Path(bash).is_file():
        return 0
    version = subprocess.run(  # noqa: S603 -- fixed args, no shell
        [bash, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
    )
    return int(version.stdout.strip() or 0)


BASH_3 = "/bin/bash"


@needs_tools
@pytest.mark.parametrize("mode", ["default", "fast"])
def test_fidelity_end_to_end(tmp_path: Path, shared_root: Path, mode: str) -> None:
    result, base = _run_fidelity(tmp_path, shared_root, mode, BIN)
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    rows = [line.split("\t") for line in (base / "result.tsv").read_text().splitlines()]
    assert rows[0][:4] == ["compression", "expected_primary", "sam_primary", "native_primary"]
    assert [row[:5] for row in rows[1:]] == [[c, "64", "64", "64", "0"] for c in ("0", "1")]
    versions = json.loads((base / "versions.json").read_text())
    version = subprocess.run([BIN, "version"], capture_output=True, text=True, check=False)  # noqa: S603
    assert versions["bwa_mem3_version"] == (version.stdout + version.stderr).splitlines()[0]
    assert (versions["batch_k"], versions["helpers"], versions["flags"]) == (160000000, 2, [])
    assert len(versions["aligner_sha256"]) == 64  # noqa: PLR2004
    assert int(versions["aligner_sha256"], 16) >= 0
    assert versions["samtools_version"].startswith("samtools ")
    assert "IDENTICAL" in (base / "compare_bam1.txt").read_text()
    assert not list((tmp_path / "work").rglob("*.bam"))


@needs_tools
@pytest.mark.skipif(_bash_major(BASH_3) != 3, reason=f"needs Bash 3 at {BASH_3} (macOS)")  # noqa: PLR2004
def test_fidelity_runs_under_bash_3(tmp_path: Path, shared_root: Path) -> None:
    """The rule's shell avoids Bash 4 features, so macOS's stock Bash 3.2 runs it."""
    result, base = _run_fidelity(tmp_path, shared_root, "default", BIN, bash=BASH_3)
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    rows = [line.split("\t") for line in (base / "result.tsv").read_text().splitlines()]
    assert [row[:5] for row in rows[1:]] == [[c, "64", "64", "64", "0"] for c in ("0", "1")]


TRUNCATING_ALIGNER = """#!/bin/bash
# Test double: drops the last read pair from the input of every route, then runs the real aligner.
set -euo pipefail
args=("$@")
n=${#args[@]}
if [ "${args[0]}" = mem ]; then
    scratch=$(mktemp -d)
    for i in $((n - 2)) $((n - 1)); do
        gzip -cd "${args[$i]}" | head -n $((4 * (@PAIRS@ - 1))) > "$scratch/$i.fq"
        args[$i]="$scratch/$i.fq"
    done
fi
exec "@BIN@" "${args[@]}"
"""


@needs_tools
def test_fidelity_fails_when_every_route_drops_a_pair(tmp_path: Path, shared_root: Path) -> None:
    """Routes that lose the same pair agree with each other; only the primary count catches it."""
    aligner = tmp_path / "truncating-aligner"
    aligner.write_text(TRUNCATING_ALIGNER.replace("@BIN@", BIN).replace("@PAIRS@", str(PAIRS)))
    aligner.chmod(0o755)
    result, base = _run_fidelity(tmp_path, shared_root, "default", str(aligner))
    assert result.returncode != 0
    assert "sam-primary-count(62!=64)" in result.stderr
    assert not (base / "result.tsv").exists()
    assert (base / "logs" / "sam.log").is_file()


QUALITY_ALTERING_ALIGNER = """#!/bin/bash
# Test double: a native BAM (`-o <file>`) whose first record has one base quality changed.
set -euo pipefail
args=("$@")
out=""
for i in "${!args[@]}"; do
    if [ "${args[$i]}" = -o ]; then out=${args[$((i + 1))]}; args[$((i + 1))]="$out.orig"; fi
done
"@BIN@" "${args[@]}"
if [ -n "$out" ]; then
    samtools view -h "$out.orig" \\
        | mawk 'BEGIN { OFS = "\\t" } !/^@/ && !n { $11 = "#" substr($11, 2); n = 1 } { print }' \\
        | samtools view -b -o "$out" -
fi
"""


@needs_tools
def test_fidelity_fails_when_native_records_differ(tmp_path: Path, shared_root: Path) -> None:
    """Equal primary counts, one changed quality: fgumi reports a difference and the rule fails."""
    script = QUALITY_ALTERING_ALIGNER.replace("@BIN@", BIN)
    aligner = _script(tmp_path / "quality-altering-aligner", script)
    result, base = _run_fidelity(tmp_path, shared_root, "default", aligner)
    assert result.returncode != 0
    assert "checks failed: fgumi-bam0(exit 1) fgumi-bam1(exit 1)\n" in result.stderr
    assert "RESULT: BAM files DIFFER" in result.stderr
    assert not (base / "result.tsv").exists()


# -- Opt-in end-to-end timing loop on the same fixture -------------------------

ARG_LOGGING_ALIGNER = """#!/bin/bash
# Test double: logs each invocation's arguments, then runs the real aligner.
printf '%s\\n' "$*" >> "@LOG@"
exec "@BIN@" "$@"
"""

NATIVE_FAILING_ALIGNER = """#!/bin/bash
# Test double: fails every native-BAM run, runs everything else.
case " $* " in *" --bam=1 "*) echo "native writer unavailable" >&2; exit 3 ;; esac
exec "@BIN@" "$@"
"""


def _run_timing(
    tmp_path: Path, shared_root: Path, endpoint: str, aligner: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    target = f"serialization/{SHA}/m8a/smoke-1M/timing-smoke/{endpoint}/timings.tsv"
    selection = ("serialization_tools=bwa-mem3", "serialization_modes=default")
    return _run_rule(tmp_path, shared_root, target, aligner, *selection)


def _timing_rows(base: Path) -> list[dict[str, str]]:
    header, *lines = (base / "timings.tsv").read_text().splitlines()
    return [dict(zip(header.split("\t"), line.split("\t"), strict=True)) for line in lines]


@needs_tools
def test_timing_pinned_runs_are_ok_and_dense_runs_read_the_dense_index(
    tmp_path: Path, shared_root: Path
) -> None:
    log = tmp_path / "aligner-args.log"
    script = ARG_LOGGING_ALIGNER.replace("@LOG@", str(log)).replace("@BIN@", BIN)
    result, base = _run_timing(tmp_path, shared_root, "pinned", _script(tmp_path / "a", script))
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    rows = _timing_rows(base)
    assert len(rows) == 8  # noqa: PLR2004 -- 2 blocks x (warmup + 1 round) x (native, sam)
    assert {(r["status"], r["exit_code"], r["primary_count"]) for r in rows} == {("ok", "0", "64")}
    assert {r["block"] for r in rows} == {"bwa-mem2-stock", "bwa-mem2-dense"}
    assert all(r["wall_s"] != "NA" for r in rows)
    runs = [line for line in log.read_text().splitlines() if line.startswith("mem ")]
    dense = [line for line in runs if "/references/hg38-u1/" in line]
    assert len(runs) == 8 and len(dense) == 4  # noqa: PLR2004
    settings = json.loads((base / "settings.json").read_text())
    assert (settings["dense_sa_shift"], settings["prewarm_passes"], settings["budget"]) == (1, 2, 8)
    assert settings["samtools_version"].startswith("samtools ")


@needs_tools
def test_timing_practical_runs_are_unchecked(tmp_path: Path, shared_root: Path) -> None:
    result, base = _run_timing(tmp_path, shared_root, "practical", BIN)
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    statuses = {(r["status"], r["exit_code"], r["primary_count"]) for r in _timing_rows(base)}
    assert statuses == {("unchecked", "0", "NA")}


@needs_tools
def test_timing_job_fails_when_every_run_drops_a_pair(tmp_path: Path, shared_root: Path) -> None:
    script = TRUNCATING_ALIGNER.replace("@BIN@", BIN).replace("@PAIRS@", str(PAIRS))
    result, _ = _run_timing(tmp_path, shared_root, "pinned", _script(tmp_path / "t", script))
    assert result.returncode != 0
    assert "failed (exit 0, primary 62 of 64)" in result.stderr
    assert "status=ok exit=" not in result.stderr
    assert "serialization_timing FAILED: no measured run completed" in result.stderr


@needs_tools
def test_timing_job_names_a_route_with_no_completed_run(tmp_path: Path, shared_root: Path) -> None:
    """A failing native writer is reported, with NA metrics, but the SAM rows keep the job green."""
    script = NATIVE_FAILING_ALIGNER.replace("@BIN@", BIN)
    result, base = _run_timing(tmp_path, shared_root, "pinned", _script(tmp_path / "n", script))
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    rows = _timing_rows(base)
    native = [r for r in rows if r["route"] == "native"]
    assert {(r["status"], r["exit_code"], r["wall_s"], r["cpu_s"]) for r in native} == {
        ("failed", "3", "NA", "NA")
    }
    assert {r["status"] for r in rows if r["route"] == "sam"} == {"ok"}
    assert "no completed measured run for bwa-mem3-native-default-stock (native)" in result.stderr
    assert "native writer unavailable" in result.stderr
