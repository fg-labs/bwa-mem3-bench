"""Workflow wiring, config rejection and an opt-in end-to-end run of `serialization.smk`.

The end-to-end tests need `BWA_MEM3_BENCH_SERIALIZATION_BIN` (a bwa-mem3 with `--bam`), plus
`samtools` and `fgumi` on PATH, and are skipped otherwise.
"""

from __future__ import annotations

import gzip
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


def _dry_run(directory: Path, target: str, *config: str) -> subprocess.CompletedProcess[str]:
    """Dry-run `target`, first touching every input the DAG reports missing."""
    for _ in range(5):
        result = _snakemake(
            directory, "-n", "-p", config=(f"fg_labs_sha={SHA}", *config, "--", target)
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
    ("entry", "message"),
    [
        (
            "serialization_modes=default;touch /tmp/x",
            "serialization_modes: 'default;touch /tmp/x' is not one of",
        ),
        ("serialization_budget=True", "serialization_budget must be a positive integer"),
        ("serialization_budget=2", "must be at least serialization_helpers + 1"),
        ("serialization_budget=16", "must be below the arena host's 16"),
        ("serialization_archs=c6a", "serialization_archs: 'c6a' is not one of"),
        ("serialization_aligner=bwa;ls", "serialization_aligner is not a plain executable path"),
        (
            "serialization_samples=wgs-5M-fast",
            "flag '--fast' sets or abbreviates an option",
        ),
        ("serialization_tools=bwa-mem2", "leave m8g with no arm to time"),
        ("fg_labs_sha=abc12", "7-40 hex chars"),
    ],
)
def test_bad_config_is_rejected(tmp_path: Path, entry: str, message: str) -> None:
    result = _dry_run(tmp_path, "serialization_smoke", entry)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr


def test_bundled_flag_in_a_sample_is_rejected(tmp_path: Path) -> None:
    """A sample whose `mem_flags` bundle `-t` (`-Mt4`) is rejected when the workflow loads."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(Path(REPO_ROOT) / "workflow", root / "workflow")
    shutil.copytree(Path(REPO_ROOT) / "config", root / "config")
    for name in ("bwa_mem3_bench", "docs", "docker"):
        (root / name).symlink_to(Path(REPO_ROOT) / name)
    samples = root / "config" / "samples.yaml"
    sample = "\n  bundled-1M:\n    baseline_tool: bwa-mem2-upstream\n    reference: hg38\n"
    sample += "    source: data/smoke/1M/\n    mem_flags: [-Mt4]\n"
    samples.write_text(samples.read_text().replace("\nsamples:\n", "\nsamples:" + sample, 1))
    result = _snakemake(
        tmp_path / "work",
        "--list",
        config=("serialization_samples=bundled-1M",),
        snakefile=root / "workflow" / "Snakefile",
    )
    assert result.returncode != 0
    assert "flag '-Mt4' sets -t, -K, -o or -f" in result.stdout + result.stderr


# -- Opt-in end-to-end fidelity on a tiny fixture -----------------------------

BIN = os.environ.get("BWA_MEM3_BENCH_SERIALIZATION_BIN", "")
needs_tools = pytest.mark.skipif(
    not (BIN and Path(BIN).is_file() and shutil.which("samtools") and shutil.which("fgumi")),
    reason="needs BWA_MEM3_BENCH_SERIALIZATION_BIN, and samtools and fgumi on PATH",
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


def _run_fidelity(
    tmp_path: Path, shared_root: Path, mode: str, aligner: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "emit-host-meta").symlink_to(Path(REPO_ROOT) / "docker" / "emit-host-meta.sh")
    base = f"serialization/{SHA}/m8a/smoke-1M/fidelity/{mode}"
    config = (
        f"fg_labs_sha={SHA}",
        f"shared_root={shared_root}",
        "serialization_fidelity_threads=2",
    )
    result = _snakemake(
        tmp_path / "work",
        "--cores",
        "2",
        config=(*config, f"serialization_aligner={aligner}", "--", f"{base}/result.tsv"),
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
    )
    return result, tmp_path / "work" / base


@needs_tools
@pytest.mark.parametrize("mode", ["default", "fast"])
def test_fidelity_end_to_end(tmp_path: Path, shared_root: Path, mode: str) -> None:
    result, base = _run_fidelity(tmp_path, shared_root, mode, BIN)
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    rows = [line.split("\t") for line in (base / "result.tsv").read_text().splitlines()]
    assert rows[0][:4] == ["compression", "expected_primary", "sam_primary", "native_primary"]
    assert [row[:5] for row in rows[1:]] == [[c, "64", "64", "64", "0"] for c in ("0", "1")]
    assert '"aligner_sha256"' in (base / "versions.json").read_text()
    assert not list((tmp_path / "work").rglob("*.bam"))


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
